#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小黑盒独立评论发送工具
用法:
    python send_comment.py --link-id 123456 --text "评论内容"
    python send_comment.py --link-id ac6a7241c4c2 --text "评论内容"   # 自动转换
    python send_comment.py --link-id https://...?link_id=ac6a7241c4c2 --text "内容"  # 支持完整链接
    python send_comment.py --link-id 123456 --text "回复内容" --reply-id 789
    python send_comment.py --link-id 123456 --text "内容" --dry-run

注意：回复评论时，若未指定 --root-id，将自动将 --root-id 设为 --reply-id（适用于回复第一层评论）。
若回复楼中楼，请手动提供正确的 --root-id。
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent))

from heybox_client import HeyboxCommentClient
from custom_signer import CustomSigner
from config_loader import load_config
from auth_manager import HTTPAuthManager


def setup_utf8_console() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def resolve_cookie(req_cfg: dict, auth_cfg: dict, *, use_config_cookie: bool = False) -> str:
    if use_config_cookie:
        cookie = str(req_cfg.get("cookie", "")).strip()
        if not cookie:
            raise ValueError("request.cookie is empty in config")
        return cookie

    manager = HTTPAuthManager(auth_cfg["state_file"])
    state_cookie = manager.load_cookie()
    if state_cookie:
        return state_cookie

    fallback = str(req_cfg.get("cookie", "")).strip()
    if fallback:
        return fallback

    raise ValueError("No cookie found. Run --login/--login-qr/--save-cookie first or set request.cookie in config.")


def extract_link_id_from_input(value: str) -> str:
    """如果输入是完整 URL，提取 link_id 参数；否则原样返回"""
    if value.startswith(('http://', 'https://')):
        parsed = urlparse(value)
        params = parse_qs(parsed.query)
        if 'link_id' in params:
            return params['link_id'][0]
    return value


def resolve_real_link_id(client: HeyboxCommentClient, link_id_str: str, debug: bool = False) -> int:
    """
    将用户输入的 link_id（可能为编码或数字）转换为真实的数字 ID
    """
    # 如果是纯数字，直接转为 int
    if link_id_str.isdigit():
        return int(link_id_str)
    
    # 否则调用 fetch_post_content 获取帖子信息，从中提取数字 ID
    try:
        post_data = client.fetch_post_content(link_id=link_id_str)
    except Exception as e:
        raise ValueError(f"获取帖子信息失败: {e}")
    
    if not post_data.ok:
        raise ValueError(f"获取帖子信息失败: {post_data.status} - {post_data.msg}")
    
    raw = post_data.raw
    result = raw.get('result', {})
    link = result.get('link', {})
    real_id = link.get('linkid')
    if real_id is None:
        real_id = link.get('link_id')
    if real_id is None:
        raise ValueError("无法从帖子信息中提取数字ID")
    
    real_id = int(real_id)
    if debug:
        print(f"[INFO] 解析得到数字ID: {real_id}", file=sys.stderr)
    return real_id


def send_comment(
    client: HeyboxCommentClient,
    link_id: int,
    text: str,
    is_cy: int = 0,
    reply_id: int = -1,
    root_id: int = -1,
    max_retries: int = 3,
    retry_wait: int = 5,
    debug: bool = False,
) -> dict[str, Any]:
    if reply_id != -1 and root_id == -1:
        root_id = reply_id

    last_result = None

    for attempt in range(1, max_retries + 1):
        try:
            result = client.create_comment(
                link_id=link_id,
                text=text,
                is_cy=is_cy,
                reply_id=reply_id,
                root_id=root_id,
            )
            last_result = result
            if result.ok:
                return {
                    "ok": True,
                    "status": result.status,
                    "msg": result.msg,
                    "comment_id": result.comment_id,
                    "floor": result.floor,
                    "attempt": attempt,
                    "http_status": result.http_status_code,
                }
            if result.status in {"login", "show_captcha"}:
                return {
                    "ok": False,
                    "status": result.status,
                    "msg": result.msg,
                    "comment_id": None,
                    "floor": None,
                    "attempt": attempt,
                    "http_status": result.http_status_code,
                }
            if debug:
                print(f"[RETRY] attempt {attempt}/{max_retries} failed: {result.status} - {result.msg}", file=sys.stderr)
            time.sleep(retry_wait)
        except Exception as e:
            if debug:
                print(f"[RETRY] attempt {attempt}/{max_retries} exception: {e}", file=sys.stderr)
            time.sleep(retry_wait)
            last_result = None

    if last_result:
        return {
            "ok": False,
            "status": last_result.status,
            "msg": last_result.msg,
            "comment_id": None,
            "floor": None,
            "attempt": max_retries,
            "http_status": last_result.http_status_code,
        }
    else:
        return {
            "ok": False,
            "status": "exception",
            "msg": "Max retries exceeded due to exceptions",
            "comment_id": None,
            "floor": None,
            "attempt": max_retries,
            "http_status": None,
        }


def main() -> int:
    setup_utf8_console()
    parser = argparse.ArgumentParser(description="小黑盒独立评论发送工具")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径")
    parser.add_argument("--link-id", type=str, required=True, help="帖子 link_id（支持十进制数字、十六进制编码或完整分享链接）")
    parser.add_argument("--text", required=True, help="评论内容")
    parser.add_argument("--reply-id", type=int, default=-1, help="回复的目标评论 ID，-1 表示回复主楼")
    parser.add_argument("--root-id", type=int, default=-1, help="根评论 ID（仅当回复非第一层评论时需要）")
    parser.add_argument("--is-cy", type=int, default=0, help="is_cy 参数，默认为 0")
    parser.add_argument("--max-retries", type=int, default=3, help="最大重试次数")
    parser.add_argument("--retry-wait", type=int, default=5, help="重试等待秒数")
    parser.add_argument("--use-config-cookie", action="store_true", help="强制使用 request.cookie 而非状态文件")
    parser.add_argument("--dry-run", action="store_true", help="只打印参数，不实际发送")
    parser.add_argument("--debug", action="store_true", help="输出调试信息（到 stderr）")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"[ERROR] 加载配置失败: {e}", file=sys.stderr)
        return 1

    req_cfg = config["request"]
    auth_cfg = config["auth"]

    try:
        cookie = resolve_cookie(req_cfg, auth_cfg, use_config_cookie=args.use_config_cookie)
    except Exception as e:
        print(f"[ERROR] 获取 cookie 失败: {e}", file=sys.stderr)
        return 1

    client = HeyboxCommentClient(
        base_url=req_cfg["base_url"],
        req_path=req_cfg["req_path"],
        default_query=req_cfg["default_query"],
        headers=req_cfg["headers"],
        cookie=cookie,
        signer=CustomSigner(),
        timeout_seconds=int(req_cfg.get("timeout_seconds", 15)),
    )

    link_id_raw = extract_link_id_from_input(args.link_id)

    if args.dry_run:
        print("[DRY-RUN] 以下参数将被发送（注意：link_id 未转换，实际发送时会自动转换）:")
        print(f"  原始 link_id = {link_id_raw}")
        print(f"  text         = {args.text}")
        print(f"  reply_id     = {args.reply_id}")
        print(f"  root_id      = {args.root_id}")
        print(f"  is_cy        = {args.is_cy}")
        print("[DRY-RUN] 未实际发送")
        return 0

    try:
        real_link_id = resolve_real_link_id(client, link_id_raw, debug=args.debug)
    except Exception as e:
        print(f"[ERROR] 解析帖子ID失败: {e}", file=sys.stderr)
        return 1

    result = send_comment(
        client=client,
        link_id=real_link_id,
        text=args.text,
        is_cy=args.is_cy,
        reply_id=args.reply_id,
        root_id=args.root_id,
        max_retries=args.max_retries,
        retry_wait=args.retry_wait,
        debug=args.debug,
    )

    output = {
        "link_id": real_link_id,
        "original_link_id": link_id_raw,
        "ok": result["ok"],
        "status": result["status"],
        "msg": result["msg"],
        "comment_id": result["comment_id"],
        "floor": result["floor"],
        "attempt": result["attempt"],
        "http_status": result.get("http_status"),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
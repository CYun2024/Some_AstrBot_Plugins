"""
小黑盒 @消息获取工具
功能: 获取指定时间段内的所有 @消息，返回对应的帖子 ID 列表

依赖: 复用 heibox-comment-bot-master 的 config, auth, signer 基础设施

用法:
    python at_fetcher.py --start-time "2026-06-20 10:00:00" --end-time "2026-06-20 15:00:00"
    python at_fetcher.py --start-ts 1718868000 --end-ts 1718886000
    python at_fetcher.py --recent-hours 2
    python at_fetcher.py --recent-hours 2 --heybox-id 12345678

返回格式 (JSON):
    {
      "count": 3,
      "link_ids": [183663331, 183663332, 183663333],
      "details": [...]
    }
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from heybox_client import HeyboxCommentClient
from custom_signer import CustomSigner
from config_loader import load_config
from auth_manager import HTTPAuthManager


# 小黑盒 API 常量
API_BASE_URL = "https://api.xiaoheihe.cn"
MESSAGE_NUM_LIMIT = 20


def _parse_timestamp(value: str | int | float) -> int:
    """将各种时间输入解析为 UTC 时间戳（秒）"""
    if isinstance(value, (int, float)):
        return int(value)

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return int(dt.timestamp())
        except ValueError:
            continue

    try:
        return int(float(value))
    except ValueError:
        pass

    raise ValueError(f"无法解析时间: {value}")


def _timestamp_to_beijing_str(ts: int) -> str:
    """UTC 时间戳转北京时间字符串"""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=8)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _extract_heybox_id(cookie: str) -> str | None:
    """从 cookie 字符串中提取 heybox_id / user_heybox_id"""
    if not cookie:
        return None
    # 小黑盒 cookie 中字段名可能是 heybox_id 或 user_heybox_id
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("heybox_id=") or part.startswith("user_heybox_id="):
            return part.split("=", 1)[1].strip()
    return None


def _resolve_cookie(req_cfg: dict, auth_cfg: dict) -> str:
    """
    复用其他三个文件的 cookie 获取逻辑：
    1. 先尝试从 auth_manager 的 state_file 加载
    2. 失败则使用 config 中的 request.cookie
    """
    manager = HTTPAuthManager(auth_cfg["state_file"])
    cookie = manager.load_cookie()
    if cookie:
        return cookie

    fallback = str(req_cfg.get("cookie", "")).strip()
    if fallback:
        return fallback

    raise ValueError(
        "No cookie found. Run --login/--login-qr/--save-cookie first or set request.cookie in config."
    )


def fetch_at_messages(
    client: HeyboxCommentClient,
    heybox_id: str,
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    """
    获取指定时间范围内的所有 @消息。

    使用 client 的 session/headers/signer，但构造 @消息专用请求。
    小黑盒消息 API 按时间倒序返回（新的在前）。
    """
    session = client.session
    headers = dict(client.headers)
    default_query = dict(client.default_query)
    signer = client.signer

    all_messages: list[dict[str, Any]] = []
    offset = 0

    while True:
        path = "/bbs/app/user/message"
        keys = signer.get_keys(path)

        params = dict(default_query)
        params.update({
            "hkey": keys.hkey,
            "nonce": keys.nonce,
            "_time": str(keys.Rtime),
            "message_type": "16",
            "app": "heybox",
            "offset": str(offset),
            "limit": str(MESSAGE_NUM_LIMIT),
            "no_more": "false",
        })

        url = f"{API_BASE_URL}{path}"
        resp = session.get(url, params=params, headers=headers, timeout=client.timeout_seconds)
        resp.raise_for_status()
        data = resp.json()

        status = str(data.get("status", ""))
        if status != "ok":
            raise RuntimeError(f"API error: status={status} msg={data.get('msg', '')}")

        result = data.get("result", {})
        messages = result.get("messages", [])

        if not messages:
            break

        page_has_valid = False
        all_too_old = True

        for msg in messages:
            ts_raw = msg.get("timestamp", "0")
            try:
                msg_ts = int(float(ts_raw))
            except (ValueError, TypeError):
                msg_ts = 0

            # 兼容毫秒时间戳
            if msg_ts > 2000000000000:
                msg_ts = msg_ts // 1000

            if start_ts <= msg_ts <= end_ts:
                page_has_valid = True
                all_too_old = False

                link = msg.get("link", {})
                message_type = msg.get("message_type", 0)

                if message_type == 16:  # @帖子
                    link_id = link.get("linkid", 0) if link else msg.get("linkid", 0)
                    is_post = True
                    text = link.get("description", "") if link else msg.get("comment_a_text", "")
                else:  # @评论
                    link_id = msg.get("linkid", 0)
                    is_post = False
                    text = msg.get("comment_a_text", "")

                user_a = msg.get("user_a", {}) or msg.get("user", {})

                parsed = {
                    "link_id": int(link_id) if isinstance(link_id, (int, float, str)) and str(link_id).isdigit() else 0,
                    "message_id": msg.get("message_id", 0),
                    "timestamp": msg_ts,
                    "time_str": _timestamp_to_beijing_str(msg_ts),
                    "username": user_a.get("username", "") if isinstance(user_a, dict) else "",
                    "user_id": str(user_a.get("userid", "")) if isinstance(user_a, dict) else "",
                    "text": str(text) if text else "",
                    "comment_id": msg.get("comment_a_id", 0),
                    "root_comment_id": msg.get("root_comment_id", 0),
                    "is_post": is_post,
                    "raw_message_type": message_type,
                }
                all_messages.append(parsed)

            elif msg_ts > end_ts:
                all_too_old = False  # 还有更新的消息，继续翻页

            elif msg_ts < start_ts:
                pass  # 太旧了，继续检查本页其他消息

        # 终止条件
        if len(messages) < MESSAGE_NUM_LIMIT:
            break

        if all_too_old and not page_has_valid and offset >= MESSAGE_NUM_LIMIT * 2:
            break

        offset += MESSAGE_NUM_LIMIT

        # 安全上限：最多 50 页
        if offset >= MESSAGE_NUM_LIMIT * 50:
            break

    all_messages.sort(key=lambda x: x["timestamp"])
    return all_messages


def get_at_messages_in_range(
    config_path: str,
    start_ts: int,
    end_ts: int,
    heybox_id: str | None = None,
) -> dict[str, Any]:
    """
    主入口：获取指定时间范围内的 @消息
    """
    config = load_config(config_path)
    req_cfg = config["request"]
    auth_cfg = config["auth"]

    # 解析 cookie（与其他三个文件保持一致）
    cookie = _resolve_cookie(req_cfg, auth_cfg)

    if not cookie:
        return {
            "count": 0,
            "link_ids": [],
            "details": [],
            "error": "没有可用的 cookie",
            "time_range": {
                "start_ts": start_ts,
                "end_ts": end_ts,
                "start_str": _timestamp_to_beijing_str(start_ts),
                "end_str": _timestamp_to_beijing_str(end_ts),
            },
        }

    # 获取 heybox_id
    if not heybox_id:
        heybox_id = _extract_heybox_id(cookie)
    if not heybox_id:
        # 尝试从 config 中获取
        heybox_id = str(req_cfg.get("heybox_id", "")).strip()
    if not heybox_id:
        raise ValueError(
            "无法从 cookie 中提取 heybox_id/user_heybox_id。请使用 --heybox-id 参数手动指定，"
            "或在 config 中设置 request.heybox_id。"
        )

    # 创建客户端（与其他三个文件保持一致）
    client = HeyboxCommentClient(
        base_url=req_cfg["base_url"],
        req_path=req_cfg["req_path"],
        default_query=req_cfg["default_query"],
        headers=req_cfg["headers"],
        cookie=cookie,
        signer=CustomSigner(),
        timeout_seconds=15,
    )

    messages = fetch_at_messages(client, heybox_id, start_ts, end_ts)

    # 去重 link_id
    seen: set[int] = set()
    unique: list[dict[str, Any]] = []
    for msg in messages:
        lid = msg["link_id"]
        if lid and lid not in seen:
            seen.add(lid)
            unique.append(msg)

    return {
        "count": len(unique),
        "link_ids": [m["link_id"] for m in unique],
        "details": unique,
        "time_range": {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "start_str": _timestamp_to_beijing_str(start_ts),
            "end_str": _timestamp_to_beijing_str(end_ts),
        },
        "heybox_id": heybox_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="获取小黑盒 @消息，返回帖子 ID 列表")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径")
    parser.add_argument("--start-time", help="开始时间，如 '2026-06-20 10:00:00' (北京时间)")
    parser.add_argument("--end-time", help="结束时间，如 '2026-06-20 15:00:00' (北京时间)")
    parser.add_argument("--start-ts", type=int, help="开始时间戳 (UTC秒)")
    parser.add_argument("--end-ts", type=int, help="结束时间戳 (UTC秒)")
    parser.add_argument("--recent-hours", type=float, help="获取最近 N 小时的 @")
    parser.add_argument("--heybox-id", help="手动指定小黑盒用户 ID（若 cookie 中无法提取）")
    parser.add_argument("--raw", action="store_true", help="输出完整 details")
    args = parser.parse_args()

    # 确定时间范围
    now = int(time.time())

    if args.recent_hours is not None:
        end_ts = now
        start_ts = int(now - args.recent_hours * 3600)
    elif args.start_ts is not None and args.end_ts is not None:
        start_ts = args.start_ts
        end_ts = args.end_ts
    elif args.start_time is not None and args.end_time is not None:
        start_ts = _parse_timestamp(args.start_time)
        end_ts = _parse_timestamp(args.end_time)
    else:
        parser.print_help()
        print("\n[ERROR] 必须指定时间范围: --recent-hours, 或 --start-ts/--end-ts, 或 --start-time/--end-time")
        return 1

    if start_ts > end_ts:
        print("[ERROR] 开始时间不能晚于结束时间")
        return 1

    print(f"[INFO] 获取 @消息: {_timestamp_to_beijing_str(start_ts)} ~ {_timestamp_to_beijing_str(end_ts)}")

    try:
        result = get_at_messages_in_range(
            args.config,
            start_ts,
            end_ts,
            heybox_id=args.heybox_id,
        )
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1

    if args.raw:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        output = {
            "count": result["count"],
            "link_ids": result["link_ids"],
            "time_range": result["time_range"],
            "details": [
                {
                    "link_id": d["link_id"],
                    "time": d["time_str"],
                    "user": d["username"],
                    "text_preview": d["text"][:50] + "..." if len(d["text"]) > 50 else d["text"],
                }
                for d in result["details"]
            ],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())

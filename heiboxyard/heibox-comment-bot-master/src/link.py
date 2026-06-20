"""
小黑盒帖子信息查询工具（整合版）
一次请求获取帖主名字 + 帖子发布时间

用法:
    python link_info.py --link-id 183663331
    python link_info.py --link-id 183663331 --debug

返回字段:
    - link_id: 帖子ID
    - userid: 帖主数字ID
    - username: 帖主名字
    - avatar: 帖主头像
    - create_at: 帖子发布时间（UTC时间戳）
    - create_at_str: 帖子发布时间（北京时间字符串）
    - modify_at: 最后修改/评论时间（UTC时间戳）
    - modify_at_str: 最后修改/评论时间（北京时间字符串）
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from heybox_client import HeyboxCommentClient
from custom_signer import CustomSigner
from config_loader import load_config
from auth_manager import HTTPAuthManager


def timestamp_to_beijing_str(ts: int | None) -> str | None:
    """UTC时间戳转北京时间字符串"""
    if ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=8)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return None


def extract_link_info(raw_data: dict, link_id: int) -> dict:
    """
    从 API 原始数据中提取帖子核心信息
    """
    result = raw_data.get("result", {})
    if not isinstance(result, dict):
        return {"link_id": link_id, "error": "result 不是字典"}

    link = result.get("link", {})
    if not isinstance(link, dict):
        return {"link_id": link_id, "error": "result.link 不是字典"}

    info = {
        "link_id": link_id,
        "userid": None,
        "username": None,
        "avatar": None,
        "create_at": None,        # 帖子发布时间（UTC时间戳）
        "create_at_str": None,    # 帖子发布时间（北京时间）
        "modify_at": None,        # 最后修改/评论时间
        "modify_at_str": None,    # 最后修改/评论时间（北京时间）
        "title": None,
        "content": None,
    }

    # ========== 帖主信息（result.link.user）==========
    link_user = link.get("user", {})
    if isinstance(link_user, dict):
        userid = link_user.get("userid")
        if isinstance(userid, int):
            info["userid"] = userid
        elif isinstance(userid, str) and userid.isdigit():
            info["userid"] = int(userid)

        username = link_user.get("username")
        if isinstance(username, str) and username.strip():
            info["username"] = username.strip()

        avatar = link_user.get("avatar") or link_user.get("headimg")
        if isinstance(avatar, str) and avatar.strip():
            info["avatar"] = avatar.strip()

    # 备用：result.link 直接字段
    if info["userid"] is None:
        userid = link.get("userid")
        if isinstance(userid, int):
            info["userid"] = userid
        elif isinstance(userid, str) and userid.isdigit():
            info["userid"] = int(userid)

    # ========== 帖子时间信息 ==========
    # 帖子发布时间
    create_at = link.get("create_at")
    if isinstance(create_at, int):
        info["create_at"] = create_at
        info["create_at_str"] = timestamp_to_beijing_str(create_at)
    elif isinstance(create_at, str) and create_at.isdigit():
        info["create_at"] = int(create_at)
        info["create_at_str"] = timestamp_to_beijing_str(int(create_at))

    # 最后修改/评论时间
    modify_at = link.get("modify_at")
    if isinstance(modify_at, int):
        info["modify_at"] = modify_at
        info["modify_at_str"] = timestamp_to_beijing_str(modify_at)
    elif isinstance(modify_at, str) and modify_at.isdigit():
        info["modify_at"] = int(modify_at)
        info["modify_at_str"] = timestamp_to_beijing_str(int(modify_at))

    # ========== 帖子内容预览 ==========
    title = link.get("title")
    if isinstance(title, str) and title.strip():
        info["title"] = title.strip()

    content = ""
    for key in ("content", "text", "description", "desc"):
        val = link.get(key)
        if isinstance(val, str) and val.strip():
            content = val.strip()
            break

    if content:
        info["content"] = content[:] 

    return info


def get_link_info(config_path: str, link_id: int, debug: bool = False) -> dict:
    """
    获取帖子信息（帖主 + 发布时间）
    只发一次 API 请求
    """
    config = load_config(config_path)
    req_cfg = config["request"]
    auth_cfg = config["auth"]

    manager = HTTPAuthManager(auth_cfg["state_file"])
    cookie = manager.load_cookie() or str(req_cfg.get("cookie", "")).strip()

    if not cookie:
        return {"link_id": link_id, "error": "没有可用的 cookie"}

    # 只创建一次客户端，只发一次请求
    client = HeyboxCommentClient(
        base_url=req_cfg["base_url"],
        req_path=req_cfg["req_path"],
        default_query=req_cfg["default_query"],
        headers=req_cfg["headers"],
        cookie=cookie,
        signer=CustomSigner(),
        timeout_seconds=15,
    )

    tree_cfg = req_cfg.get("link_tree", {})
    post = client.fetch_post_content(
        link_id=link_id,
        tree_path=str(tree_cfg.get("req_path", "/bbs/app/link/tree")),
        tree_url=str(tree_cfg.get("url", "https://api.xiaoheihe.cn/bbs/app/link/tree")),
        is_first=1, page=1, index=1, limit=20, owner_only=0,
    )

    if not post.ok:
        return {"link_id": link_id, "error": f"{post.status} {post.msg}"}

    info = extract_link_info(post.raw, link_id)

    # 调试模式：保存原始数据
    if debug:
        debug_file = f"debug_link_{link_id}.json"
        Path(debug_file).write_text(
            json.dumps(post.raw, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[DEBUG] 原始数据已保存: {debug_file}")

    return info


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="获取小黑盒帖子信息（帖主+发布时间）")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径")
    parser.add_argument("--link-id", type=int, required=True, help="帖子 link_id")
    parser.add_argument("--debug", action="store_true", help="保存原始数据到文件")
    args = parser.parse_args()

    result = get_link_info(args.config, args.link_id, debug=args.debug)
    print(json.dumps(result, ensure_ascii=False, indent=2))

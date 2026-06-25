"""
小黑盒帖子信息查询工具（整合版）
一次请求获取帖子信息 + 点赞数前三的评论

用法:
    python src/link.py --link-id 183663331
    python src/link.py --link-id 183663331 --top-n 5
    python src/link.py --link-id 183663331 --debug

返回JSON格式:
    {
      "link_id": 183663331,
      "userid": 26816787,
      "username": "月桂蛋糕",
      "avatar": "https://...",
      "create_at": 1781712006,
      "create_at_str": "2025-06-18 08:00:06",
      "modify_at": 1781769303,
      "modify_at_str": "2025-06-18 23:55:03",
      "title": "祝kk生日快乐！！",
      "content": "帖子内容...",
      "topics": [...],
      "post_stats": {
        "favour_count": 2,
        "link_award_num": 79,
        "comment_num": 36,
        "battery_count": 30,
        "forward_num": 0
      },
      "top_comments": [
        {
          "rank": 1,
          "comment_id": 891058613,
          "username": "Kelvin-0",
          "user_id": "59543894",
          "avatar": "https://...",
          "text": "评论内容...",
          "up": 18,
          "has_image": false,
          "images": []
        }
      ]
    }
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any

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


def extract_post_stats(link: dict) -> dict[str, Any]:
    """提取帖子统计数据"""
    battery = link.get("battery", {})
    if not isinstance(battery, dict):
        battery = {}
    return {
        "favour_count": link.get("favour_count", 0),
        "link_award_num": link.get("link_award_num", 0),
        "comment_num": link.get("comment_num", 0),
        "battery_count": battery.get("count", 0),
        "forward_num": link.get("forward_num", 0),
        "click": link.get("click", 0),
    }


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
        "create_at": None,
        "create_at_str": None,
        "modify_at": None,
        "modify_at_str": None,
        "title": None,
        "content": None,
        "topics": [],
        "post_stats": {},
    }

    # 帖主信息
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

    # 备用字段
    if info["userid"] is None:
        userid = link.get("userid")
        if isinstance(userid, int):
            info["userid"] = userid
        elif isinstance(userid, str) and userid.isdigit():
            info["userid"] = int(userid)

    # 帖子时间
    create_at = link.get("create_at")
    if isinstance(create_at, int):
        info["create_at"] = create_at
        info["create_at_str"] = timestamp_to_beijing_str(create_at)
    elif isinstance(create_at, str) and create_at.isdigit():
        info["create_at"] = int(create_at)
        info["create_at_str"] = timestamp_to_beijing_str(int(create_at))

    modify_at = link.get("modify_at")
    if isinstance(modify_at, int):
        info["modify_at"] = modify_at
        info["modify_at_str"] = timestamp_to_beijing_str(modify_at)
    elif isinstance(modify_at, str) and modify_at.isdigit():
        info["modify_at"] = int(modify_at)
        info["modify_at_str"] = timestamp_to_beijing_str(int(modify_at))

    # 帖子内容
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
        info["content"] = content

    # 话题标签
    topics = link.get("topics")
    if isinstance(topics, list) and topics:
        info["topics"] = topics

    # 帖子统计
    info["post_stats"] = extract_post_stats(link)

    return info


def extract_comment(node: dict) -> dict | None:
    """提取单条评论信息"""
    cid_raw = node.get("commentid")
    if not isinstance(cid_raw, int):
        return None

    user_obj = node.get("user") if isinstance(node.get("user"), dict) else {}

    # 提取图片
    imgs_raw = node.get("imgs", [])
    images = []
    if isinstance(imgs_raw, list):
        for img in imgs_raw:
            if isinstance(img, dict):
                url = img.get("url") or img.get("thumb")
                if url and isinstance(url, str):
                    images.append(url)

    return {
        "comment_id": cid_raw,
        "username": user_obj.get("username"),
        "user_id": str(user_obj.get("userid", "")),
        "avatar": user_obj.get("avatar") or user_obj.get("avartar"),
        "text": str(node.get("text", "")).strip(),
        "up": node.get("up", 0) or 0,
        "has_image": len(images) > 0,
        "images": images,
    }


def extract_top_comments(raw_data: dict, top_n: int = 3) -> list[dict]:
    """
    从原始响应中提取点赞数前N的评论
    只取主评论（floor_num > 0），跳过楼中楼
    """
    comments: list[dict] = []
    seen_ids: set[int] = set()

    result = raw_data.get("result", {})
    if not isinstance(result, dict):
        return comments

    groups = result.get("comments", [])
    if not isinstance(groups, list):
        return comments

    for group in groups:
        if not isinstance(group, dict):
            continue
        arr = group.get("comment", [])
        if not isinstance(arr, list):
            continue
        for node in arr:
            if not isinstance(node, dict):
                continue
            # 只取主评论
            if node.get("floor_num", 0) <= 0:
                continue
            detail = extract_comment(node)
            if detail and detail["comment_id"] not in seen_ids:
                comments.append(detail)
                seen_ids.add(detail["comment_id"])

    # 按点赞数降序，取前N
    comments.sort(key=lambda x: x.get("up", 0), reverse=True)
    top = comments[:top_n]
    for i, c in enumerate(top, 1):
        c["rank"] = i
    return top


def get_link_info(config_path: str, link_id: int, top_n: int = 3, debug: bool = False) -> dict:
    """
    获取帖子信息 + 点赞数前N的评论
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

    # 提取帖子信息
    info = extract_link_info(post.raw, link_id)

    # 提取点赞数前N的评论（从同一份原始数据）
    info["top_comments"] = extract_top_comments(post.raw, top_n=top_n)

    # 调试模式：保存原始数据
    if debug:
        debug_file = f"debug_link_{link_id}.json"
        Path(debug_file).write_text(
            json.dumps(post.raw, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[DEBUG] 原始数据已保存: {debug_file}", file=sys.stderr)

    return info


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="获取小黑盒帖子信息 + 点赞数前N评论")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径")
    parser.add_argument("--link-id", type=int, required=True, help="帖子 link_id")
    parser.add_argument("--top-n", type=int, default=3, help="取点赞数前N条评论(默认3)")
    parser.add_argument("--debug", action="store_true", help="保存原始数据到文件")
    args = parser.parse_args()

    result = get_link_info(args.config, args.link_id, top_n=args.top_n, debug=args.debug)
    print(json.dumps(result, ensure_ascii=False, indent=2))

"""
@消息拉取模块
负责：定时拉取 @ 消息，提取帖子 ID 并走完整处理流程
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from astrbot.api import logger

from .utils import ts_to_bj_str


class AtMessageFetcher:
    """@消息定时拉取器"""

    # 默认拉取时间点（北京时间）
    DEFAULT_FETCH_HOURS = [4, 10, 16, 22]

    def __init__(self, post_manager, fetch_hours: list[int] = None,
                 recent_hours: int = 6, enabled: bool = True):
        """
        Args:
            post_manager: PostManager 实例
            fetch_hours: 拉取时间点列表（北京时间小时），默认 [4, 10, 16, 22]
            recent_hours: 每次拉取最近 N 小时的 @消息
            enabled: 是否启用
        """
        self.post_manager = post_manager
        self.fetch_hours = fetch_hours or self.DEFAULT_FETCH_HOURS
        self.recent_hours = recent_hours
        self.enabled = enabled
        self._task: Optional[asyncio.Task] = None

    def start(self):
        """启动定时拉取任务"""
        if not self.enabled:
            logger.info("@消息拉取已禁用")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info(f"@消息定时拉取已启动: {self.fetch_hours} 点, 每次拉取最近 {self.recent_hours} 小时")

    def stop(self):
        """停止定时拉取任务"""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("@消息定时拉取已停止")

    async def _loop(self):
        """主循环：等待到下一个拉取时间点"""
        while True:
            try:
                wait_seconds = self._calc_next_wait()
                next_hour = self._get_next_hour()
                logger.info(f"@消息拉取: 等待 {wait_seconds/3600:.1f} 小时到 {next_hour}:00 北京时间")
                await asyncio.sleep(wait_seconds)

                logger.info(f"执行 @消息定时拉取 ({next_hour}:00)")
                await self.fetch_and_process()

            except asyncio.CancelledError:
                logger.info("@消息拉取任务已取消")
                break
            except Exception as e:
                logger.error(f"@消息拉取循环异常: {e}")
                await asyncio.sleep(60)

    def _calc_next_wait(self) -> float:
        """计算到下一个拉取时间点的等待秒数"""
        now_bj = datetime.now(timezone(timedelta(hours=8)))
        current_hour = now_bj.hour

        # 找到下一个拉取时间点
        next_hour = None
        for h in sorted(self.fetch_hours):
            if h > current_hour:
                next_hour = h
                break

        if next_hour is None:
            # 今天的都已经过了，取明天的第一个
            next_hour = min(self.fetch_hours)
            target = now_bj.replace(hour=next_hour, minute=0, second=0, microsecond=0) + timedelta(days=1)
        else:
            target = now_bj.replace(hour=next_hour, minute=0, second=0, microsecond=0)

        # 如果目标时间已经过了（比如正好在目标时间点），跳到下一个周期
        if target <= now_bj:
            target = target + timedelta(days=1)

        return (target - now_bj).total_seconds()

    def _get_next_hour(self) -> int:
        """获取下一个拉取时间点的小时数"""
        now_bj = datetime.now(timezone(timedelta(hours=8)))
        current_hour = now_bj.hour

        for h in sorted(self.fetch_hours):
            if h > current_hour:
                return h
        return min(self.fetch_hours)

    async def fetch_and_process(self) -> int:
        """
        拉取 @消息并处理帖子
        返回处理的帖子数量
        """
        try:
            link_ids = await self.post_manager.fetch_at_messages(recent_hours=self.recent_hours)
            if not link_ids:
                logger.info("本次 @消息拉取为空")
                return 0

            logger.info(f"@消息拉取到 {len(link_ids)} 个帖子，开始处理...")
            processed = await self.post_manager.process_posts(link_ids, source="at")
            return processed

        except Exception as e:
            logger.error(f"@消息拉取处理失败: {e}")
            return 0

    async def manual_fetch(self, start_time: str = None, end_time: str = None,
                           recent_hours: int = None) -> int:
        """
        手动触发 @消息拉取
        Args:
            start_time: 开始时间 "YYYY-MM-DD HH:MM:SS"
            end_time: 结束时间 "YYYY-MM-DD HH:MM:SS"
            recent_hours: 最近 N 小时
        """
        try:
            if recent_hours is not None:
                link_ids = await self.post_manager.fetch_at_messages(recent_hours=recent_hours)
            elif start_time and end_time:
                link_ids = await self.post_manager.fetch_at_messages(start_time=start_time, end_time=end_time)
            else:
                link_ids = await self.post_manager.fetch_at_messages(recent_hours=self.recent_hours)

            if not link_ids:
                logger.info("手动 @消息拉取为空")
                return 0

            logger.info(f"手动 @消息拉取到 {len(link_ids)} 个帖子，开始处理...")
            processed = await self.post_manager.process_posts(link_ids, source="at")
            return processed

        except Exception as e:
            logger.error(f"手动 @消息拉取失败: {e}")
            return 0
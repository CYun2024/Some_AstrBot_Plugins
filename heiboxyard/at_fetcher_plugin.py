"""
@消息拉取模块
负责：定时拉取 @ 消息，提取帖子 ID 并走完整处理流程
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from astrbot.api import logger

from .utils import get_current_window_no, get_window_by_no, ts_to_bj_str


class AtMessageFetcher:
    """@消息定时拉取器"""

    # 默认拉取时间点（北京时间）
    DEFAULT_FETCH_HOURS = [4, 10, 16, 22]

    def __init__(self, post_manager, fetch_hours: list[int] = None,
                 recent_hours: int = 6, enabled: bool = True):
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

        next_hour = None
        for h in sorted(self.fetch_hours):
            if h > current_hour:
                next_hour = h
                break

        if next_hour is None:
            next_hour = min(self.fetch_hours)
            target = now_bj.replace(hour=next_hour, minute=0, second=0, microsecond=0) + timedelta(days=1)
        else:
            target = now_bj.replace(hour=next_hour, minute=0, second=0, microsecond=0)

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

    def _get_target_window_no(self, fetch_hour: int) -> str:
        """
        获取@消息应该归入的目标窗口编号

        特殊处理：在22:00执行拉取时，拉取的是 recent_hours 小时内的消息
        例如22:00拉取最近6小时的消息（16:00-22:00），这些消息应该归入当前窗口
        但22:00是窗口分界线，get_current_window_no() 在22:00后会返回下一个窗口

        解决方案：
        - 如果当前时间是22:00整（或接近22:00），且拉取小时包含22
          则使用上一个窗口的编号
        - 否则使用当前窗口编号
        """
        now_bj = datetime.now(timezone(timedelta(hours=8)))
        current_hour = now_bj.hour
        current_minute = now_bj.minute

        # 获取当前窗口编号（可能已经是下一个窗口）
        current_window_no = get_current_window_no()

        # 如果在22:00-22:05之间执行，且拉取小时是22
        # 说明这是22:00的定时拉取，应该归入上一个窗口
        if fetch_hour == 22 and current_hour == 22 and current_minute < 10:
            # 上一个窗口 = 当前日期（因为22:00后窗口编号是明天）
            # 例如 2026-06-25 22:00，当前窗口编号是 20260626
            # 上一个窗口编号是 20260625
            try:
                window_start, _ = get_window_by_no(current_window_no)
                # 上一个窗口的结束时间 = 当前窗口的开始时间
                # 上一个窗口编号 = 当前窗口开始时间对应的日期
                prev_window_end = window_start
                prev_dt = datetime.fromtimestamp(prev_window_end, tz=timezone(timedelta(hours=8)))
                prev_window_no = prev_dt.strftime("%Y%m%d")
                logger.info(f"22:00 定时拉取特殊处理: 使用上一个窗口编号 {prev_window_no} (当前窗口={current_window_no})")
                return prev_window_no
            except Exception as e:
                logger.warning(f"计算上一个窗口编号失败: {e}，回退到当前窗口 {current_window_no}")
                return current_window_no

        return current_window_no

    async def fetch_and_process(self) -> int:
        """
        拉取 @消息并处理帖子
        所有@消息归入当前窗口编号（22:00特殊处理）
        返回处理的帖子数量
        """
        try:
            # 确定当前执行的小时（用于22:00特殊处理）
            now_bj = datetime.now(timezone(timedelta(hours=8)))
            fetch_hour = now_bj.hour

            # 确定目标窗口编号（处理22:00分界线问题）
            target_window_no = self._get_target_window_no(fetch_hour)
            logger.info(f"@消息拉取: 当前时间={now_bj.strftime('%H:%M')}, 目标窗口编号={target_window_no}")

            link_ids = await self.post_manager.fetch_at_messages(recent_hours=self.recent_hours)
            if not link_ids:
                logger.info("本次 @消息拉取为空")
                return 0

            logger.info(f"@消息拉取到 {len(link_ids)} 个帖子，开始处理...")
            processed = await self.post_manager.process_posts(
                link_ids, source="at", target_window_no=target_window_no
            )
            return processed

        except Exception as e:
            logger.error(f"@消息拉取处理失败: {e}")
            return 0

    async def manual_fetch(self, start_time: str = None, end_time: str = None,
                           recent_hours: int = None) -> int:
        """
        手动触发 @消息拉取
        所有@消息归入当前窗口编号
        """
        try:
            # 确定当前窗口编号
            target_window_no = get_current_window_no()
            logger.info(f"手动 @消息拉取: 当前窗口编号={target_window_no}")

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
            processed = await self.post_manager.process_posts(
                link_ids, source="at", target_window_no=target_window_no
            )
            return processed

        except Exception as e:
            logger.error(f"手动 @消息拉取失败: {e}")
            return 0
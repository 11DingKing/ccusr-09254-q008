"""队列使用的时钟：生产环境走系统 UTC，测试可冻结/推进以模拟老化与租约到期。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    def __init__(self) -> None:
        self._frozen: datetime | None = None
        self._offset: timedelta = timedelta(0)

    def now(self) -> datetime:
        if self._frozen is not None:
            return self._frozen
        return datetime.now(timezone.utc) + self._offset

    def freeze(self, moment: datetime | None = None) -> datetime:
        if moment is None:
            moment = self.now()
        if moment.tzinfo is None:
            raise ValueError("时间必须包含时区")
        self._frozen = moment.astimezone(timezone.utc)
        return self._frozen

    def advance(self, delta: timedelta) -> datetime:
        target = self.now() + delta
        self._frozen = target
        return target

    def reset(self) -> None:
        self._frozen = None
        self._offset = timedelta(0)


clock = Clock()

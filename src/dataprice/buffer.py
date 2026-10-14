"""批量缓冲。策略线程只往里放，另一个线程取走再写数据库。"""

from __future__ import annotations

import threading
from typing import Generic, TypeVar

T = TypeVar("T")

DEFAULT_BAR_WATERMARK = 200_000
DEFAULT_EVENT_WATERMARK = 10_000


class BufferFull(RuntimeError):
    """缓冲满了。调用方应停下来等刷盘，而不是在策略线程里写数据库。"""


class BatchBuffer(Generic[T]):
    def __init__(self, high_watermark: int) -> None:
        if high_watermark < 1:
            raise ValueError("high_watermark 必须 >= 1")
        self.high_watermark = high_watermark
        self._items: list[T] = []
        self._lock = threading.Lock()

    def add(self, item: T) -> None:
        with self._lock:
            if len(self._items) >= self.high_watermark:
                raise BufferFull(
                    f"缓冲已到 {self.high_watermark} 条，拒绝在调用线程上写数据库"
                )
            self._items.append(item)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def take(self) -> list[T]:
        with self._lock:
            items = self._items
            self._items = []
            return items

    def prepend(self, items: list[T]) -> None:
        """写入失败时把批次放回去，保持原顺序。"""
        if not items:
            return
        with self._lock:
            self._items = list(items) + self._items

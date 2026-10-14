"""下单线程上的出站队列。只存事件，不连数据库。"""

from __future__ import annotations

from dataprice.buffer import DEFAULT_EVENT_WATERMARK, BatchBuffer
from dataprice.contracts import FillEvent, OrderEvent


class Outbox(BatchBuffer[OrderEvent | FillEvent]):
    def __init__(self, high_watermark: int = DEFAULT_EVENT_WATERMARK) -> None:
        super().__init__(high_watermark)

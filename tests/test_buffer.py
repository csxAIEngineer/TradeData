import pytest

from dataprice.buffer import BatchBuffer, BufferFull


def test_take_and_prepend_keep_order():
    buffer = BatchBuffer[int](high_watermark=3)
    buffer.add(1)
    buffer.add(2)
    first = buffer.take()
    buffer.add(3)
    buffer.prepend(first)
    assert buffer.take() == [1, 2, 3]


def test_add_refuses_when_full():
    buffer = BatchBuffer[str](high_watermark=1)
    buffer.add("a")
    with pytest.raises(BufferFull):
        buffer.add("b")
    assert buffer.take() == ["a"]

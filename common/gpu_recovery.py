"""Live セッションが所有する GPU メモリ不足時の復旧フック。"""
from typing import Callable

_handler: Callable[..., None] | None = None


def set_handler(handler: Callable[..., None] | None) -> None:
    global _handler
    _handler = handler


def active() -> bool:
    return _handler is not None


def is_oom(error) -> bool:
    text = str(error).lower()
    return any(mark in text for mark in (
        "cuda_oom", "insufficient_vram", "cuda out of memory",
        "cuda error: out of memory", "cudaerrormemoryallocation",
        "gpu の空き", "cuda failed to allocate", "cudamalloc failed",
        "unable to allocate cuda", "cuda error out of memory",
    ))


def recover(source: str, error, *, deadline: float | None = None) -> bool:
    handler = _handler
    if handler is None or not is_oom(error):
        return False
    handler(f"{source}: {error}", deadline=deadline)
    return True

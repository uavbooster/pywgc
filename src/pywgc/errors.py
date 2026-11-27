"""pywgc 的异常层次。

全部继承自 :class:`RuntimeError`，这样既能让调用方沿用 ``except RuntimeError``
的旧写法，又能在需要精确处理时按类型捕获。
"""

from __future__ import annotations

__all__ = [
    "CaptureError",
    "WindowNotFound",
    "BackendUnavailable",
    "StaleFrameError",
    "BlankFrameError",
]


class CaptureError(RuntimeError):
    """所有抓屏相关错误的基类。"""


class WindowNotFound(CaptureError):
    """找不到目标窗口，或窗口尺寸/区域不可用。"""


class BackendUnavailable(CaptureError):
    """``backend="wgc"`` 或 ``backend="gdi"`` 强制模式下后端无法启动。"""


class StaleFrameError(CaptureError):
    """``on_stale="raise"`` 时取不到新帧。

    触发条件：会话僵死且自愈失败、窗口最小化/无可见表面、会话已结束。
    """


class BlankFrameError(CaptureError):
    """连续多帧全黑。

    典型原因：DRM 保护的表面、窗口内容已丢失、GDI 后端被完全遮挡。
    """

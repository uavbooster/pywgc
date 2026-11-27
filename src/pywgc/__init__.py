"""pywgc —— 低延迟 Windows 窗口抓屏。

``Windows.Graphics.Capture`` 优先、GDI ``BitBlt`` 降级的双后端封装，对外只暴露
一个拉取式的 ``grab()``。

    >>> from pywgc import WindowGrabber
    >>> with WindowGrabber(title="记事本") as g:
    ...     frame = g.grab()
    ...     print(g.backend, frame.shape)

单帧成本对比（同一窗口、800x684 ROI）：

===========  =========================  ==================
后端         单次 ``grab()`` 耗时        CPU / 帧
===========  =========================  ==================
``wgc``      0.4 ~ 0.7 ms               约 3.1 ms
``gdi``      16.4 ~ 17.0 ms             约 12.4 ms
===========  =========================  ==================

行为差异（这才是选型的决定性因素，不是速度）：

* ``wgc`` 抓**窗口表面** —— 窗口被遮挡、失去前台、部分移出屏幕都照常出帧；
* ``gdi`` 抓**屏幕像素** —— 被遮挡时抓到遮挡物，完全遮挡时返回全黑。

因此 ``backend="auto"``（默认）总是先尝试 ``wgc``，失败才降级。
"""

from __future__ import annotations

from . import _win32 as win32
from ._win32 import (
    client_rect,
    enable_dpi_awareness,
    frame_rect,
    is_foreground,
    is_minimized,
    is_visible,
    list_windows,
    move_window,
    pick_main_window,
    show_no_activate,
    window_ops_blocked,
)
from ._win32 import is_alive as is_window_alive
from .capture import AsyncGrabber, WindowGrabber, available_backends, wait_for_window
from .errors import (
    BackendUnavailable,
    BlankFrameError,
    CaptureError,
    StaleFrameError,
    WindowNotFound,
)

__version__ = "0.1.0"

__all__ = [
    # 抓屏
    "WindowGrabber",
    "AsyncGrabber",
    "wait_for_window",
    "available_backends",
    # 窗口工具
    "win32",
    "enable_dpi_awareness",
    "list_windows",
    "pick_main_window",
    "client_rect",
    "frame_rect",
    "is_window_alive",
    "is_visible",
    "is_minimized",
    "is_foreground",
    "show_no_activate",
    "move_window",
    "window_ops_blocked",
    # 异常
    "CaptureError",
    "WindowNotFound",
    "BackendUnavailable",
    "StaleFrameError",
    "BlankFrameError",
    "__version__",
]

# 导入即声明 DPI 感知：否则在 125%/150% 缩放下客户区坐标是逻辑坐标，
# 而 GDI 按物理像素工作，抓到的区域会整体偏移。
enable_dpi_awareness()

"""Win32 底层封装：窗口枚举、几何查询、窗口操作与权限诊断。

本模块只依赖标准库 ``ctypes``，不引入任何第三方包，方便单独导入和测试。
所有函数在非 Windows 平台导入时即失败（见文件末尾的平台检查），
因为这里的每一项能力都直接对应 ``user32`` 的符号。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys

if sys.platform != "win32":
    raise ImportError(
        "pywgc only supports Windows: this module binds user32/gdi32 directly. "
        f"Current platform is {sys.platform!r}."
    )

__all__ = [
    "enable_dpi_awareness",
    "iter_windows",
    "list_windows",
    "pick_main_window",
    "client_rect",
    "frame_rect",
    "is_alive",
    "is_minimized",
    "is_foreground",
    "is_visible",
    "show_no_activate",
    "move_window",
    "window_ops_blocked",
    "ERROR_ACCESS_DENIED",
]

user32 = ctypes.WinDLL("user32", use_last_error=True)

# ---- 常量 ---------------------------------------------------------------
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
ERROR_ACCESS_DENIED = 5

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wt.LONG),
        ("top", wt.LONG),
        ("right", wt.LONG),
        ("bottom", wt.LONG),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


# ---- 函数签名 -----------------------------------------------------------
# 必须显式声明：不设 argtypes/restype 时 HWND 会按 c_int 截断，
# 而 GetForegroundWindow 的返回值在高位为 1 时会变成负数，比较永远不等。
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wt.BOOL
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = wt.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wt.HWND
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.SetWindowPos.argtypes = [
    wt.HWND,
    wt.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wt.UINT,
]
user32.SetWindowPos.restype = wt.BOOL

_DPI_SET = False


def enable_dpi_awareness() -> None:
    """声明本进程为 per-monitor DPI aware（幂等）。

    不做这一步的话，在 125% / 150% 缩放下 ``GetClientRect`` 与
    ``ClientToScreen`` 给出的是**逻辑坐标**，而 GDI 抓屏按**物理像素**工作，
    结果就是抓到的区域整体偏移 —— 表现为"画面错位"，极难联想到 DPI。
    """
    global _DPI_SET
    if _DPI_SET:
        return
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - Win7 没有 shcore，退回老 API
        try:
            user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    _DPI_SET = True


def _window_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def iter_windows(title_prefix: str | None = None, pid: int | None = None):
    """枚举顶层窗口，按需按标题前缀 / 进程号过滤，逐个 yield 字典。

    每个字典的键：``hwnd`` ``pid`` ``title`` ``visible`` ``w`` ``h`` ``x`` ``y``。
    其中 ``x`` / ``y`` / ``w`` / ``h`` 指的是**客户区**（不含标题栏与边框）
    在屏幕上的位置，这也是抓屏 ROI 使用的坐标系。
    """
    out = []

    def _cb(hwnd, _lparam):
        title = _window_text(hwnd)
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if title_prefix is not None and not title.startswith(title_prefix):
            return True
        if pid is not None and wpid.value != pid:
            return True
        cr = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(cr))
        cw, ch = cr.right - cr.left, cr.bottom - cr.top
        pt = POINT(cr.left, cr.top)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        out.append(
            {
                "hwnd": int(hwnd),
                "pid": wpid.value,
                "title": title,
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "w": cw,
                "h": ch,
                "x": pt.x,
                "y": pt.y,
            }
        )
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    yield from out


def list_windows(title_prefix: str | None = None, pid: int | None = None) -> list:
    """``iter_windows`` 的列表版本。"""
    return list(iter_windows(title_prefix, pid))


def pick_main_window(windows, min_side: int = 100):
    """从一堆同名窗口里挑出最可能是"主窗口"的那个；没有候选返回 ``None``。

    为什么不能简单地"可见优先"：应用最小化到托盘时，渲染窗口会被
    ``SW_HIDE``（``visible=False``），反而留下一个
    ``160x28 @ (-32000,-32000)`` 的最小化壳仍是 visible。可见优先就会选中壳，
    拿到面积仅数千、坐标在屏幕外的假窗口。

    打分维度：客户区面积（主导）、可见性（加分但压不过面积）、
    最小化（坐标在 -32000 附近或 ``IsIconic``，重罚）。
    """
    cands = [w for w in windows if w.get("w", 0) > min_side and w.get("h", 0) > min_side]
    if not cands:
        return None

    def score(w):
        s = w["w"] * w["h"]
        if w["visible"]:
            s += 200_000
        if w["x"] < -10_000 or w["y"] < -10_000:
            s -= 500_000  # 最小化壳的经典坐标
        try:
            if user32.IsIconic(w["hwnd"]):
                s -= 500_000
        except Exception:  # noqa: BLE001
            pass
        return s

    return max(cands, key=score)


def client_rect(hwnd: int):
    """客户区在屏幕上的 ``(x, y, w, h)``；取不到返回 ``None``。"""
    cr = RECT()
    try:
        if not user32.GetClientRect(hwnd, ctypes.byref(cr)):
            return None
        pt = POINT(cr.left, cr.top)
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
        w, h = cr.right - cr.left, cr.bottom - cr.top
        return (pt.x, pt.y, w, h)
    except Exception:  # noqa: BLE001
        return None


def frame_rect(hwnd: int):
    """窗口**外框**在屏幕上的 ``(left, top, w, h)``；取不到返回 ``None``。

    注意与 :func:`client_rect` 的区别：要把窗口挪走再挪回原位，必须用外框
    坐标 —— 用客户区坐标恢复会少挪一个标题栏的高度，抓屏 ROI 随之偏掉。
    """
    r = RECT()
    try:
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:  # noqa: BLE001
        return None


def is_alive(hwnd: int) -> bool:
    """窗口句柄是否仍然有效（进程重启后旧 hwnd 会失效）。"""
    try:
        return bool(user32.IsWindow(hwnd))
    except Exception:  # noqa: BLE001
        return False


def is_minimized(hwnd: int) -> bool:
    """窗口是否被最小化。

    最小化时两种后端都拿不到画面：GDI 抓到空图/黑图，WGC 因为没有可见表面
    **一帧都不出**。所以这是两个后端都必须处理的情况。
    """
    try:
        return bool(user32.IsIconic(hwnd))
    except Exception:  # noqa: BLE001
        return False


def is_visible(hwnd: int) -> bool:
    try:
        return bool(user32.IsWindowVisible(hwnd))
    except Exception:  # noqa: BLE001
        return False


def is_foreground(hwnd: int) -> bool:
    try:
        return user32.GetForegroundWindow() == hwnd
    except Exception:  # noqa: BLE001
        return False


def error_text(err: int) -> str:
    """把 Win32 错误码转成可读文本（失败返回空串）。"""
    if not err:
        return ""
    try:
        return ctypes.FormatError(err)
    except Exception:  # noqa: BLE001
        return ""


def uipi_hint(err: int) -> str:
    """``err == 5`` 时的可操作解释。

    UIPI（User Interface Privilege Isolation）只允许**同级或更高**完整性级别的
    进程向窗口发送消息，而 ``SetWindowPos`` / ``ShowWindow`` /
    ``SetForegroundWindow`` 本质上都是在发窗口消息。因此当目标进程以管理员
    权限运行、而本进程没有提权时，这些调用会**静默返回 0**，``GetLastError``
    为 ``5``，不抛任何异常。

    注意 ``SendInput`` **不受**此限制（它注入的是输入队列，不是窗口消息），
    所以"能往窗口注入按键、却挪不动窗口"是完全正常的组合，不必怀疑代码。
    """
    if err == ERROR_ACCESS_DENIED:
        return (
            " (UIPI: the target window belongs to a process with a higher "
            "integrity level. Run this process elevated, or run the target "
            "process without elevation.)"
        )
    return ""


def window_ops_blocked(hwnd: int):
    """本进程能否对该窗口做窗口操作。返回 ``(blocked, reason)``。

    为什么要单独探测：这类调用失败时**不抛异常**，只给出返回值和
    ``GetLastError``。若不主动检查，故障会表现成"某项检查莫名失败但看不出
    原因"。

    探测方式为 ``SWP_NOMOVE | SWP_NOSIZE`` 且坐标/尺寸全零 —— 不会真正改变
    窗口状态，只是让 Windows 走一遍权限检查，因此可以安全地反复调用。
    """
    try:
        ctypes.set_last_error(0)
        user32.SetWindowPos(
            hwnd, None, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE
        )
        err = ctypes.get_last_error()
        if err == ERROR_ACCESS_DENIED:
            return True, "ERROR_ACCESS_DENIED(5): target process has a higher integrity level"
        return False, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def show_no_activate(hwnd: int) -> bool:
    """显示窗口但不激活它（不抢焦点）。"""
    try:
        return bool(user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE))
    except Exception:  # noqa: BLE001
        return False


def move_window(hwnd: int, x: int, y: int):
    """只改窗口位置：不动尺寸、不动 Z 序、不抢焦点。返回 ``(ok, err)``。

    ``SWP_NOSIZE`` 是必需的 —— 尺寸一变，抓屏侧就得重建整个 WGC 会话，
    白白付出一次 D3D 会话初始化的代价。

    失败时返回值里带 ``GetLastError``：``SetWindowPos`` 返回 0 但**不抛异常**，
    只包一层 ``try/except`` 的话会静默失败，排查时完全无从下手。
    """
    try:
        ctypes.set_last_error(0)
        ok = user32.SetWindowPos(
            hwnd, None, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
        )
        return bool(ok), (0 if ok else ctypes.get_last_error())
    except Exception as e:  # noqa: BLE001
        return False, getattr(e, "winerror", 0) or 0

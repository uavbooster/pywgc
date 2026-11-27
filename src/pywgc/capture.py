"""pywgc 核心：双后端窗口抓屏。

两个后端的定位（同一个 ``grab()`` 接口）：

  ``wgc``  Windows.Graphics.Capture，基于 ``windows-capture`` 包。
           直接抓**窗口表面**，不受遮挡、不受前台归属、不受其它窗口影响。
           底层是推送模型（native 线程回调），本模块用"多格帧槽"把它包装成
           拉取式的 ``grab()``。打开一次会话后单次取帧约 0.5 ms。
  ``gdi``  桌面 ``BitBlt``，基于 ``mss`` 包。抓的是**屏幕像素**，
           窗口被遮挡、失去前台、移出屏幕就抓到别人的画面甚至全黑。
           单次取帧约 16 ms。仅作为降级路径与取证用途。

WGC 最危险的失败模式是"会话僵死但一声不响"：native 线程还在，回调不再触发，
``grab()`` 于是永远返回同一张旧图。为此本模块内置四道observability：

  ``stale_ms``     零新帧超过该阈值判定会话僵死 -> 尝试自愈；
                   连续 ``max_heals`` 次失败则降级到 ``gdi``。
  ``cb_errors``    回调里被吞掉的异常计数（异常必须吞，否则会打死 native
                   采集线程；但必须计数，否则"库升级后 100% 丢帧"会静默发生）。
  ``blank_streak`` 连续全黑帧计数。全黑帧绝不写进"上一张有效帧"缓存，
                   否则回退链路会拿黑图当有效数据一直传下去。
  ``age_ms``       当前可交付帧的年龄，一眼看出会话是否还活着。
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from . import _win32 as w32
from .errors import (
    BackendUnavailable,
    BlankFrameError,
    StaleFrameError,
    WindowNotFound,
)

__all__ = ["WindowGrabber", "AsyncGrabber", "available_backends"]

logger = logging.getLogger("pywgc")
logger.addHandler(logging.NullHandler())

# 帧槽格数。回调写一格、grab 读另一格；4 格在 60 fps 下约 66 ms 余量，
# 而 grab 的 copy 只有约 0.3 ms，环回覆写的概率可以忽略。
_NBUF = 4

_BACKEND_ALIASES = {"mss": "gdi", "bitblt": "gdi", "desktop": "gdi", "dxgi": "wgc"}

# 进程级缓存：windows_capture 是否可用 / draw_border=False 是否被本机支持。
# 后者一定要记忆 —— 每次白试的代价是一次失败的 D3D 会话初始化。
_WGC_MOD = None
_BORDER_OK = None
_CV2 = None


def _load_wgc():
    """惰性导入 windows_capture。不可用时返回 ``None`` 并说明原因。"""
    global _WGC_MOD
    if _WGC_MOD is None:
        try:
            import windows_capture as _m

            _WGC_MOD = _m
        except Exception as e:  # noqa: BLE001
            logger.warning("windows_capture 不可用: %s", e)
            _WGC_MOD = False
    return _WGC_MOD or None


def _load_cv2():
    """惰性导入 OpenCV（可选依赖，仅用于加速色彩转换）。"""
    global _CV2
    if _CV2 is None:
        try:
            import cv2 as _m

            _CV2 = _m
        except Exception:  # noqa: BLE001
            _CV2 = False
    return _CV2 or None


def _bgra_to_bgr(src: np.ndarray, dst: np.ndarray | None = None) -> np.ndarray:
    """BGRA -> BGR。

    装了 OpenCV 就走 SIMD（``cvtColor`` 约 0.5 ms/1.6MP），否则用 numpy
    跨步拷贝（约 1.8 ms）。结果都是紧凑的 ``(h, w, 3)`` uint8。
    """
    cv2 = _load_cv2()
    if cv2 is not None:
        return cv2.cvtColor(src, cv2.COLOR_BGRA2BGR, dst=dst)
    out = np.ascontiguousarray(src[..., :3])
    if dst is not None:
        dst[...] = out
        return dst
    return out


def available_backends() -> dict:
    """探测本机可用后端。返回 ``{"wgc": bool, "gdi": bool}``。

    只做导入探测，不建立任何会话（无副作用，可安全地在启动日志里调用）。
    """
    gdi = False
    try:
        import mss  # noqa: F401

        gdi = True
    except Exception:  # noqa: BLE001
        gdi = False
    return {"wgc": _load_wgc() is not None, "gdi": gdi}


class WindowGrabber:
    """抓取单个窗口的客户区，返回 BGR ``uint8`` 数组 ``(h, w, 3)``。

    用法::

        with WindowGrabber(title="MyApp") as g:
            frame = g.grab()
            print(g.backend, frame.shape, g.stats)

    :param title: 窗口标题**前缀**（``startswith`` 匹配）。与 ``hwnd`` 二选一。
    :param hwnd: 直接指定窗口句柄，最可靠；标题会随文档/页面变化。
    :param region: 相对客户区的感兴趣区域 ``(x, y, w, h)``，默认整块客户区。
    :param backend: ``"auto"``（默认，先 WGC 后降级）、``"wgc"``、``"gdi"``。
        ``"mss"`` / ``"bitblt"`` / ``"desktop"`` 均视为 ``"gdi"`` 的别名。
    :param first_frame_timeout: 等待首帧的秒数，超时即判定该后端不可用。
    :param cv_threads: OpenCV 线程数，默认压到 1（见下方注释）。
    :param stale_ms: 零新帧多久判定会话僵死。
    :param max_heals: 连续自愈上限，超过则降级到 ``gdi``。
    :param blank_limit: 连续多少帧全黑才判定"没有画面"。

    关于 ``cv_threads=1``：OpenCV 默认按核数开线程池，对"1.6 MB
    BGRA->BGR"这种小任务纯属调度开销（实测 8 线程 3.6 ms CPU，单线程
    0.55 ms）。这类小图操作单线程更快，所以默认压到 1。
    """

    # 类级默认值：``stats`` 不应因为 ``__init__`` 没跑过就 AttributeError
    # （测试替身常用 ``WindowGrabber.__new__()`` 手工装配）。
    _border_ok = False

    def __init__(
        self,
        title: str | None = None,
        hwnd: int | None = None,
        region=None,
        backend: str = "auto",
        first_frame_timeout: float = 1.5,
        cv_threads: int = 1,
        stale_ms: float = 3000.0,
        max_heals: int = 3,
        blank_limit: int = 3,
    ):
        if title is None and hwnd is None:
            raise ValueError("必须提供 title 或 hwnd 之一")

        self.title = title
        self._hwnd_hint = int(hwnd) if hwnd is not None else None
        self.region = region
        self.cv_threads = cv_threads
        self.first_frame_timeout = first_frame_timeout
        self._backend = "gdi"
        self.win = None
        self._mon = {}
        self._roi = (0, 0, 0, 0)

        # WGC 帧槽：回调写、grab 读，互不阻塞
        self._bufs = []
        self._front = 0  # 最近一帧可读的下标
        self._write = 0  # 回调下一次写的下标（只有回调写它）
        self._n = 0  # 已收到的帧数
        self._closed = False  # WGC 会话是否已结束
        self._control = None  # CaptureControl（用于 stop/wait）
        self._lock = threading.Lock()
        self._cap = None
        self.sct = None  # gdi 后端
        self._mss_mod = None
        self._last_img = None

        # ---- 健康度 ----
        self._last_frame_t = 0.0  # native 最近一次推帧的 monotonic 时间
        self._last_ts = None  # WGC 的 frame.timespan：判断是否真换了帧
        self._stale_ms = float(stale_ms)
        self._heals = 0
        self._max_heals = int(max_heals)
        self._cb_errors = 0
        self._blank_streak = 0
        self._blank_limit = int(blank_limit)
        self._border_ok = False
        self._wgc_error = None  # WGC 起不来的原因（强制模式异常消息里带上）

        want = _BACKEND_ALIASES.get(str(backend).lower(), str(backend).lower())
        if want not in ("auto", "wgc", "gdi"):
            raise ValueError(f"未知 backend: {backend!r}（可选 auto / wgc / gdi）")

        if want in ("auto", "wgc"):
            if self._init_wgc(first_frame_timeout):
                self._backend = "wgc"
                return
            if want == "wgc":
                raise BackendUnavailable(
                    f"WGC backend could not start: {self._wgc_error or 'unknown reason'}"
                )
        self._init_gdi()

    # ------------------------------------------------------------ 窗口解析
    def _describe(self, win: dict) -> dict:
        """把窗口字典裁到抓屏需要的最小字段。"""
        return dict(
            hwnd=win["hwnd"],
            pid=win.get("pid", 0),
            title=win.get("title", ""),
            visible=win.get("visible", True),
            w=win["w"],
            h=win["h"],
            x=win["x"],
            y=win["y"],
        )

    def _locate(self):
        """按 hwnd 或标题定位窗口。返回窗口字典或 ``None``。"""
        if self._hwnd_hint is not None:
            if not w32.is_alive(self._hwnd_hint):
                return None
            rc = w32.client_rect(self._hwnd_hint)
            if rc is None:
                return None
            return dict(
                hwnd=self._hwnd_hint,
                pid=0,
                title="",
                visible=w32.is_visible(self._hwnd_hint),
                w=rc[2],
                h=rc[3],
                x=rc[0],
                y=rc[1],
            )
        cands = w32.list_windows(self.title)
        win = w32.pick_main_window(cands)
        return self._describe(win) if win else None

    def _compute_roi(self, win: dict):
        """算出 ``(roi, mon)``：客户区内的 ROI 与它在屏幕上的绝对矩形。"""
        x, y, w, h = self.region if self.region else (0, 0, win["w"], win["h"])
        # 允许 ROI 超出窗口时自动裁剪，但裁到 0 就不能用
        w, h = min(w, win["w"] - x), min(h, win["h"] - y)
        roi = (int(x), int(y), int(w), int(h))
        mon = {
            "left": win["x"] + x,
            "top": win["y"] + y,
            "width": w,
            "height": h,
        }
        return roi, mon

    def refresh(self) -> dict:
        """重新定位窗口并重算 ROI。返回最新的窗口字典。

        ROI 变化（窗口被拖动/缩放）时会自动重建 WGC 会话 —— 帧槽尺寸是
        固定的，尺寸一变继续用旧槽位就会踩内存。
        """
        win = self._locate()
        if win is None:
            if self._backend == "wgc" and self.win is not None:
                # 会话已绑定 hwnd，EnumWindows 偶尔漏掉一帧不影响取帧
                return self.win
            raise WindowNotFound(f"找不到窗口: title={self.title!r} hwnd={self._hwnd_hint}")
        roi, mon = self._compute_roi(win)
        if roi[2] <= 0 or roi[3] <= 0:
            raise WindowNotFound(
                f"抓屏区域为空: region={self.region!r} 客户区={win['w']}x{win['h']}"
            )
        self.win = win
        self._roi = roi
        self._mon = mon
        if self._backend == "wgc" and self._bufs and self._bufs[0].shape[:2] != (roi[3], roi[2]):
            self._restart_wgc()
        return self.win

    # ------------------------------------------------------------- WGC 后端
    def _init_wgc(self, first_frame_timeout: float) -> bool:
        """起一个 WGC 会话并等到首帧。成功 True，失败 False（调用方降级）。

        每条失败路径都必须留下原因（``_wgc_error``）：``backend="wgc"`` 强制
        模式下要靠它把异常说清楚，否则排查时只会看到"莫名其妙没用上 WGC"。
        """
        mod = _load_wgc()
        if mod is None:
            self._wgc_error = "windows_capture 不可用（见 WARNING 日志）"
            return False

        self._border_ok = False
        cv2 = _load_cv2()
        if cv2 is not None:
            try:
                cv2.setNumThreads(self.cv_threads)
            except Exception:  # noqa: BLE001
                pass

        try:
            win = self._locate()
        except Exception as e:  # noqa: BLE001
            self._wgc_error = f"枚举窗口失败: {e}"
            logger.warning("%s，降级 gdi", self._wgc_error)
            return False
        if win is None:
            self._wgc_error = f"找不到窗口 title={self.title!r} hwnd={self._hwnd_hint}"
            logger.warning("%s，降级 gdi", self._wgc_error)
            return False
        if win.get("w", 0) < 64 or win.get("h", 0) < 64:
            self._wgc_error = f"窗口太小（{win['w']}x{win['h']}）"
            logger.warning("%s，降级 gdi", self._wgc_error)
            return False

        roi, mon = self._compute_roi(win)
        if roi[2] <= 0 or roi[3] <= 0:
            self._wgc_error = f"抓屏区域为空（region={self.region!r}）"
            logger.warning("%s，降级 gdi", self._wgc_error)
            return False

        self.win = win
        self._roi = roi
        self._mon = mon
        self._bufs = [np.zeros((roi[3], roi[2], 3), np.uint8) for _ in range(_NBUF)]
        self._front = self._write = 0
        self._n = 0
        self._closed = False
        self._last_frame_t = 0.0
        self._last_ts = None
        self._blank_streak = 0

        # 绑定 hwnd 而不是窗口名：标题会随内容变化，而且 windows-capture 的
        # window_name 是子串匹配，与本模块的 startswith 不是一套规则。
        ok, cap, err = self._start_capture(mod, win["hwnd"])
        if not ok:
            self._wgc_error = f"会话起不来: {err}"
            logger.warning("WGC %s，降级 gdi", self._wgc_error)
            return False
        self._cap = cap

        t0 = time.time()
        while self._n == 0 and not self._closed:
            if time.time() - t0 > first_frame_timeout:
                self._wgc_error = f"{first_frame_timeout:.1f}s 内没有首帧（窗口最小化/无可见表面？）"
                logger.warning("WGC %s，降级 gdi", self._wgc_error)
                self._stop_wgc()
                return False
            time.sleep(0.01)
        if self._n == 0:
            # 这条以前完全静默：start 成功、on_closed 立刻被调用、一帧都没来。
            # 典型原因是窗口表面拿不到（DRM / 独占全屏 / 远程桌面会话）。
            self._wgc_error = "会话启动后立刻关闭（on_closed），一帧都没来"
            logger.warning("WGC %s，降级 gdi", self._wgc_error)
            self._stop_wgc()
            return False
        self._wgc_error = None
        return True

    def _start_capture(self, mod, hwnd: int):
        """建会话 + 注册回调 + 启动，失败逐级降级。返回 ``(ok, cap, err)``。

        降级链：``draw_border=False`` -> 默认边框 -> 放弃（调用方降级 gdi）。

        **降级判断必须在 start 之后**：``draw_border`` 参数是"延迟失败"的 ——
        构造成功，``start_free_threaded()`` 才抛
        ``Toggling the capture border is not supported``（Windows 10 上必然
        如此）。只在构造期 ``try/except`` 的话，整个 WGC 会静默死掉，表现成
        "莫名其妙一直用 gdi"。
        """
        global _BORDER_OK
        last = None
        borders = (None,) if _BORDER_OK is False else (False, None)
        for border in borders:
            kw = {"window_hwnd": int(hwnd), "cursor_capture": False}
            if border is not None:
                kw["draw_border"] = border
            try:
                cap = mod.WindowsCapture(**kw)
            except Exception as e:  # noqa: BLE001
                last = e
                logger.debug("构造失败（draw_border=%r）: %s", border, e)
                continue
            try:
                cap.event(self.on_frame_arrived)
                cap.event(self.on_closed)
                cap.start_free_threaded()
            except Exception as e:  # noqa: BLE001
                # 没拿到 CaptureControl 就没法 stop()；卡住的 native 会话由
                # cap 被 GC 时的 Drop 回收（只发生一次，代价可接受）。
                last = e
                if border is False:
                    logger.warning(
                        "本机不支持 draw_border=False（Windows 10 限制），改用默认边框: %s", e
                    )
                else:
                    logger.warning("启动失败: %s", e)
                continue
            self._border_ok = _BORDER_OK = border is False
            return True, cap, None
        return False, None, last

    # 回调名必须叫这两个：windows_capture 按 handler.__name__ 派发。
    def on_frame_arrived(self, frame, control):
        """native 回调线程：立刻把 ROI 转成 BGR 写进帧槽的一格。

        ``frame.frame_buffer`` 是零拷贝视图，回调返回后即失效（持有它会阻止
        上游回收 mapped frame），所以必须当场处理完。

        并发要点：``cvtColor`` 在**锁外**做 —— 主线程此刻读的是 ``_front``
        那一格，不是这一格，只有"指针切换"需要加锁。这样转换（约 1 ms）与
        ``grab()`` 的 copy（约 0.3 ms）才真正并行，而不是在同一把锁上串行。
        """
        # 只在还没有 control 时记：_start_capture 试错时可能起过一个失败会话
        # （Windows 10 的 draw_border 就是 start 阶段才抛），这种僵尸会话若
        # 反过来推帧，不能覆盖活动会话的 control —— 否则 _stop_wgc 会停错对象，
        # 活动会话泄漏。_stop_wgc 会把 control 置空，所以重建不受影响。
        if self._control is None:
            self._control = control
        try:
            x, y, w, h = self._roi
            buf = frame.frame_buffer
            if buf.shape[0] < y + h or buf.shape[1] < x + w:
                return
            bufs = self._bufs
            idx = self._write
            if idx >= len(bufs):
                return
            _bgra_to_bgr(buf[y : y + h, x : x + w], dst=bufs[idx])
            with self._lock:
                self._front = idx
                self._write = (idx + 1) % len(bufs)
                self._last_frame_t = time.monotonic()
                if frame.timespan != self._last_ts:
                    self._last_ts = frame.timespan
                    self._heals = 0  # 真出了新帧，自愈计数清零
                self._n += 1
        except Exception:  # noqa: BLE001
            # 回调里抛异常会打死 native 采集线程，所以必须吞掉；但必须计数，
            # 否则"依赖升级后 100% 丢帧"会完全静默地发生。
            self._cb_errors += 1

    def on_closed(self):
        self._closed = True

    def _stop_wgc(self):
        ctl, self._control = self._control, None
        self._cap = None
        self._closed = True
        if ctl is not None:
            try:
                ctl.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                ctl.wait()
            except Exception:  # noqa: BLE001
                pass

    def _restart_wgc(self):
        self._stop_wgc()
        if not self._init_wgc(self.first_frame_timeout):
            self._init_gdi()

    # ------------------------------------------------------------- GDI 后端
    def _init_gdi(self):
        try:
            import mss
        except Exception as e:  # noqa: BLE001
            raise BackendUnavailable(
                "GDI backend needs the 'mss' package: pip install pywgc[gdi]"
            ) from e
        self._mss_mod = mss
        self.sct = mss.MSS()
        self._backend = "gdi"
        self._blank_streak = 0
        self.refresh()

    def _grab_gdi(self, on_stale="last"):
        raw = self.sct.grab(self._mon)
        if raw.width <= 0 or raw.height <= 0:
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                pass
            raw = self.sct.grab(self._mon)
            if raw.width <= 0 or raw.height <= 0:
                if self._last_img is None:
                    raise StaleFrameError("GDI 抓屏返回空图（窗口最小化/不可见？）")
                if on_stale == "raise":
                    raise StaleFrameError("GDI 抓屏返回空图（窗口最小化/不可见？）")
                return self._last_img
        # .bgra 是原始缓冲（零转换）；.rgb 会让 mss 在 Python 里逐像素转一遍，
        # 实测白白多花 4.5 ms。转 BGR 交给 cv2 的 SIMD。
        img = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(raw.height, raw.width, 4)
        out = _bgra_to_bgr(img)
        if self._is_blank(out):
            return self._blank_fallback(out, on_stale, "GDI")
        self._last_img = out
        return out

    # ------------------------------------------------------------ 公共取帧
    def grab(self, timeout: float = 1.0, on_stale: str = "last"):
        """返回 BGR ``uint8`` 数组 ``(h, w, 3)``。

        ``on_stale`` 决定"取不到新帧"时的行为：

          * ``"last"``（默认）—— 返回上一张有效帧。适合交互式控制循环：
            宁可让调用方按旧帧继续，也不要抛异常打断整个流程。
          * ``"raise"`` —— 抛 :class:`StaleFrameError`。**录制 / 数据集采集
            必须用这个**：静默重复帧会以 60 张/秒的速度污染数据集，
            而事后完全分辨不出来。

        :param timeout: 等待首帧的最长秒数（仅 WGC 冷启动时会用到）。
        """
        if self._backend == "wgc":
            return self._grab_wgc(timeout, on_stale)
        return self._grab_gdi(on_stale)

    def _grab_wgc(self, timeout: float, on_stale: str = "last"):
        t0 = time.time()
        while True:
            with self._lock:
                n = self._n
                idx = self._front
                last_t = self._last_frame_t
                closed = self._closed
            if n > 0:
                # 帧龄看门狗：会话僵死时旧实现会永远返回同一张老图，调用方
                # 完全无法察觉 —— 这是 WGC 最危险的故障模式。
                age_ms = (time.monotonic() - last_t) * 1000.0
                if age_ms > self._stale_ms:
                    if self._maybe_heal(age_ms):
                        if self._backend != "wgc":
                            return self._grab_gdi(on_stale)  # 已降级
                        continue  # 重建过会话，重取
                    # 自愈也救不了（典型：窗口被最小化，没有可见表面可采）。
                    # 这里必须尊重 on_stale —— 否则录制端会以为还在录，
                    # 实际上每秒往文件里写 60 张同一画面。
                    if on_stale == "raise":
                        raise StaleFrameError(
                            f"WGC {age_ms:.0f}ms 无新帧且无法自愈（窗口最小化/不可见？）"
                        )
                img = self._bufs[idx].copy()  # 锁外 copy
                if self._is_blank(img):
                    return self._blank_fallback(img, on_stale, "WGC")
                self._last_img = img
                return img
            if closed:
                if self._last_img is not None and on_stale != "raise":
                    return self._last_img
                raise StaleFrameError("WGC 会话已结束（目标窗口被关闭？）")
            if time.time() - t0 > timeout:
                # 首帧一直不来也要自愈：可能上一次会话在 init 后被 DWM 断掉
                if self._maybe_heal((time.time() - t0) * 1000.0):
                    if self._backend != "wgc":
                        return self._grab_gdi(on_stale)
                    continue
                if self._last_img is not None and on_stale != "raise":
                    return self._last_img
                raise StaleFrameError("WGC 取不到帧（窗口可能已最小化/不可见）")
            time.sleep(0.002)

    def _maybe_heal(self, age_ms: float) -> bool:
        """帧龄超阈值时自愈。返回 ``True`` 表示动过会话，调用方应重取一帧。

        三种情况，顺序不能反：

          1. 窗口最小化 —— 没有可见表面，重建也没用，直接放弃自愈；
          2. hwnd 已失效（目标进程重启）—— 重新定位窗口，换 hwnd 重建；
          3. 其余 —— 用原 hwnd 重建；连续失败 ``max_heals`` 次就降级 gdi。
        """
        if self.win is not None and w32.is_minimized(self.win["hwnd"]):
            return False
        if self.win is not None and not w32.is_alive(self.win["hwnd"]):
            old = self.win["hwnd"]
            try:
                self._hwnd_hint = None
                win = self._locate()
            except Exception:  # noqa: BLE001
                win = None
            if win is None:
                return False
            logger.warning("窗口已重建（hwnd 0x%X -> 0x%X），重连 WGC", old, win["hwnd"])
            self.win = win
            self._hwnd_hint = win["hwnd"]
            self._heals += 1
            self._restart_wgc()
            return True
        if self._backend != "wgc":
            return False  # gdi 没有会话可重建
        if self._heals >= self._max_heals:
            logger.error(
                "WGC 连续 %d 次自愈失败，降级 gdi（stats=%s）", self._heals, self.stats
            )
            self._stop_wgc()
            self._init_gdi()
            return True
        self._heals += 1
        logger.warning(
            "%.0fms 无新帧，重建 WGC 会话（第 %d/%d 次）", age_ms, self._heals, self._max_heals
        )
        self._restart_wgc()
        return True

    def _is_blank(self, img) -> bool:
        """单帧全黑判定（1/256 采样，成本约 0.02 ms），并维护连续计数。

        返回的是"**这一帧**是黑的"，不是"已经黑了很久"。调用方看
        ``blank_streak >= blank_limit`` 才知道是不是真没画面 ——
        场景切换会闪黑 1 帧，那一帧可以照常交付（对下游只是一次噪声），
        但**绝不能**写进 ``_last_img``，否则回退链路会把黑图当有效帧一直传。
        """
        sample = img[::16, ::16]
        blank = sample.size > 0 and float(sample.mean()) < 1.0
        self._blank_streak = self._blank_streak + 1 if blank else 0
        return blank

    def _blank_fallback(self, img, on_stale: str, who: str):
        """连续全黑帧的交付策略：返回帧或抛错，但绝不把黑帧当有效帧缓存。

        全黑 = 窗口不可见 / DRM 保护 / GDI 后端被完全遮挡。这类帧交给下游
        视觉处理会被读成"画面全零"，比直接报错危险得多。
        """
        if self._blank_streak < self._blank_limit:
            return img  # 闪黑 1~2 帧：照常交付，但下面不会缓存它
        if on_stale == "raise":
            raise BlankFrameError(f"{who} 持续输出全黑帧（DRM / 窗口丢失 / 被遮挡？）")
        if self._last_img is not None:
            return self._last_img
        raise BlankFrameError(f"{who} 只有全黑帧，没有可回退的有效帧（窗口内容被保护或已丢失？）")

    # ------------------------------------------------------------ 生命周期
    def close(self):
        """释放资源（WGC 有 native 采集线程与 D3D 设备，必须显式关闭）。"""
        if self._backend == "wgc":
            self._stop_wgc()
        sct, self.sct = self.sct, None
        if sct is not None:
            try:
                sct.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def reopen(self) -> str:
        """窗口刚被恢复出来时调用：若当初因窗口不可用降级到 gdi，这里升回 WGC。

        典型场景：构造那一刻窗口是**最小化**的 -> WGC 等不到首帧 -> 降级 gdi。
        之后窗口被恢复，如果还赖在 gdi 上，等于白白失去"被遮挡也能抓"的能力，
        而且 CPU 高出一个量级。
        """
        if self._backend != "gdi" or _WGC_MOD is False:
            return self.backend
        w, h = self._mon.get("width", 0), self._mon.get("height", 0)
        if w < 64 or h < 64:
            return self.backend
        if not self._init_wgc(self.first_frame_timeout):
            return self.backend
        self._backend = "wgc"
        sct, self.sct = self.sct, None
        if sct is not None:
            try:
                sct.close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("窗口已恢复，gdi -> WGC 自动升级")
        return self.backend

    # ------------------------------------------------------------ 只读属性
    @property
    def backend(self) -> str:
        """当前实际使用的后端：``"wgc"`` 或 ``"gdi"``。"""
        return self._backend

    @property
    def hwnd(self) -> int:
        return self.win["hwnd"]

    @property
    def window(self) -> dict:
        """当前绑定的窗口信息（客户区坐标）。"""
        return dict(self.win or {})

    @property
    def roi(self) -> tuple:
        """当前 ROI（相对客户区）``(x, y, w, h)``。"""
        return self._roi

    @property
    def frame_rect(self):
        """窗口外框在屏幕上的 ``(left, top, w, h)``（用于挪走再挪回）。"""
        return w32.frame_rect(self.hwnd) if self.win else None

    @property
    def stats(self) -> dict:
        """健康度快照。落盘排查用：僵死、回调报错、黑帧都在这里现形。"""
        with self._lock:
            n = self._n
            last_t = self._last_frame_t
        return dict(
            backend=self._backend,
            frames=n,
            heals=self._heals,
            max_heals=self._max_heals,
            cb_errors=self._cb_errors,
            blank_streak=self._blank_streak,
            last_ts=self._last_ts,
            # 抓到的帧上是否仍带系统提示边框（Windows 10 去不掉，会压到画面边缘）
            borderless=self._border_ok,
            age_ms=None if n == 0 else round((time.monotonic() - last_t) * 1000.0, 1),
        )

    # ------------------------------------------------------------ 窗口位置
    def move_to(self, x: int, y: int):
        """把窗口挪到屏幕坐标 ``(x, y)``（不改尺寸、不抢焦点）。

        返回 ``(ok, err)``。失败时 ``err`` 是 ``GetLastError`` ——
        ``6`` 之外最常见的值是 ``5``（UIPI：目标进程完整性级别更高，
        见 :func:`pywgc.window_ops_blocked`）。
        """
        if not self.win:
            return False, 0
        ok, err = w32.move_window(self.hwnd, x, y)
        try:
            self.refresh()  # 重算 ROI（gdi 后端依赖它）
        except Exception:  # noqa: BLE001
            pass
        if not ok:
            logger.warning(
                "SetWindowPos(0x%X -> %d,%d) 失败: err=%d %s%s",
                self.hwnd,
                x,
                y,
                err,
                w32.error_text(err),
                w32.uipi_hint(err),
            )
        return ok, err

    def park(self, x: int = -4000, y: int = 0):
        """把窗口移到屏幕外但**不最小化**，返回 ``(ok, err)``。

        为什么需要这个：最小化后窗口没有可见表面，WGC 一帧都不出；而 WGC 抓的
        是窗口表面，只要合成器还在合成就能出帧 —— 移出屏幕正好满足"人看不见
        但仍在合成"。

        前提是目标程序不会因为不可见就暂停渲染。**验证方法**：挪完观察
        ``stats["age_ms"]``，3 秒内仍保持在几十毫秒以内说明出帧正常；若
        ``age_ms`` 持续增长，说明该程序会停渲染，只能留在屏幕上。

        挪回原位请先用 :attr:`frame_rect` 存下外框坐标，再 ``move_to(*rect[:2])``。
        """
        if self.win:
            w32.show_no_activate(self.hwnd)
        return self.move_to(x, y)


class AsyncGrabber:
    """后台线程持续抓屏，主循环永远拿最新一帧（旧帧会被丢弃）。

    WGC 后端本身已经是推送模型（native 线程持续采集，``grab()`` 只是取最新帧
    约 0.5 ms），这层包装在 WGC 下不再是瓶颈，但**保留**：它把调度抖动与主循环
    隔离开，而且在 gdi 降级路径下依旧必需（单帧 16 ms 会直接吃掉整个 60 Hz 预算）。

    用法::

        g = WindowGrabber(title="MyApp")
        ag = AsyncGrabber(g)
        try:
            while running:
                frame = ag.latest()      # 阻塞等一帧
                process(frame)
        finally:
            ag.stop()
            g.close()
    """

    def __init__(self, grabber: WindowGrabber, on_stale: str = "last"):
        import queue

        self._g = grabber
        self._on_stale = on_stale
        self._q = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self.frames = 0
        self.errors = 0
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                img = self._g.grab(on_stale=self._on_stale)
            except Exception:  # noqa: BLE001
                # grab 抛错时立刻重试会空转烧满一核（例如窗口刚被关闭）
                self.errors += 1
                self._stop.wait(0.05)
                continue
            if self._q.full():
                try:
                    self._q.get_nowait()  # 丢掉旧帧，永远保留最新
                except Exception:  # noqa: BLE001
                    pass
            self._q.put(img)
            self.frames += 1

    def latest(self, timeout: float = 0.5):
        """取最新一帧；超时抛 :class:`queue.Empty`。"""
        return self._q.get(timeout=timeout)

    def try_latest(self):
        """非阻塞取最新一帧；暂时没有返回 ``None``。"""
        try:
            return self._q.get_nowait()
        except Exception:  # noqa: BLE001
            return None

    @property
    def backend(self) -> str:
        return self._g.backend

    @property
    def stats(self) -> dict:
        """底层抓屏的健康度，附上本层的取帧/异常计数。"""
        s = dict(self._g.stats)
        s["async_frames"] = self.frames
        s["async_errors"] = self.errors
        return s

    @property
    def hwnd(self) -> int:
        return self._g.hwnd

    @property
    def window(self) -> dict:
        return self._g.window

    def stop(self):
        """停止后台线程（幂等）。不会关闭底层 grabber。"""
        self._stop.set()
        if self._t.is_alive():
            self._t.join(timeout=2.0)


def wait_for_window(title: str | None = None, hwnd: int | None = None, timeout: float = 15.0, poll: float = 0.5, **kwargs) -> WindowGrabber:
    """等到窗口出现再返回 :class:`WindowGrabber`。

    比直接构造少一类"一启动就崩"的坑：目标程序启动慢、或窗口在切换过程中
    短暂消失时，构造会抛 :class:`~pywgc.errors.WindowNotFound`，这里自动重试。
    """
    deadline = time.time() + timeout
    last = None
    while True:
        try:
            return WindowGrabber(title=title, hwnd=hwnd, **kwargs)
        except Exception as e:  # noqa: BLE001
            last = e
            if time.time() >= deadline:
                raise WindowNotFound(
                    f"等了 {timeout:.0f} 秒仍无法抓取窗口 title={title!r} hwnd={hwnd}（{last}）"
                ) from e
            time.sleep(poll)

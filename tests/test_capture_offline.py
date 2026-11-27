"""抓屏内核的离线测试。

不需要真实窗口、不需要显卡、不需要显示器：用 ``WindowGrabber.__new__()``
手工装配对象，再喂伪造的 native 帧，专测"静默失败"这一类路径。
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from pywgc.capture import WindowGrabber
from pywgc.errors import BlankFrameError, StaleFrameError

W, H = 320, 240


class _FakeFrame:
    """伪造 windows_capture 的 Frame（只有回调会用到的两个字段）。"""

    def __init__(self, buf, ts=0):
        self.frame_buffer = buf
        self.timespan = ts


class _FakeCtl:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True

    def wait(self):
        return True


def _make(last_img=None, fill=100):
    """装配一个处于"WGC 已就绪、手上有 1 帧"状态的 grabber。"""
    g = WindowGrabber.__new__(WindowGrabber)
    g.title = None
    g._hwnd_hint = 0x1234
    g.win = {
        "hwnd": 0x1234,
        "pid": 0,
        "title": "",
        "visible": True,
        "w": W,
        "h": H,
        "x": 0,
        "y": 0,
    }
    g.region = None
    g._roi = (0, 0, W, H)
    g._mon = {"left": 0, "top": 0, "width": W, "height": H}
    g._bufs = [np.full((H, W, 3), fill, np.uint8) for _ in range(4)]
    g._front = 0
    g._write = 1
    g._n = 1
    g._closed = False
    g._control = None
    g._lock = threading.Lock()
    g._cap = None
    g.sct = None
    g._mss_mod = None
    g._last_img = last_img
    g._last_frame_t = time.monotonic()
    g._last_ts = None
    g._stale_ms = 3000.0
    g._heals = 0
    g._max_heals = 3
    g._cb_errors = 0
    g._blank_streak = 0
    g._blank_limit = 3
    g._border_ok = False
    g._wgc_error = None
    g._backend = "wgc"
    return g


# ---------------------------------------------------------------- 帧槽
def test_frame_slot_delivers_latest_frame():
    g = _make()
    b1 = np.zeros((H, W, 4), np.uint8)
    b1[..., 0] = 10
    b2 = np.zeros((H, W, 4), np.uint8)
    b2[..., 0] = 20
    g.on_frame_arrived(_FakeFrame(b1, ts=1), _FakeCtl())
    g.on_frame_arrived(_FakeFrame(b2, ts=2), _FakeCtl())

    img = g.grab()
    assert img.shape == (H, W, 3)
    assert img.dtype == np.uint8
    # BGRA 的第 0 通道就是 BGR 的 B；拿到 20 说明给的是第二帧（最新）
    assert int(img[0, 0, 0]) == 20


def test_frame_slot_is_independent_copy():
    """grab() 必须返回副本：调用方原地改图不能污染帧槽。"""
    g = _make()
    img1 = g.grab()
    img1[:] = 0
    img2 = g.grab()
    assert int(img2[0, 0, 0]) == 100


def test_roi_crop_applied():
    """回调只截取 ROI 区域，不是整个窗口表面。"""
    g = _make(fill=0)
    g._roi = (10, 20, 32, 16)
    # 帧槽尺寸由 _init_wgc 按 ROI 分配，这里手工对齐
    g._bufs = [np.zeros((16, 32, 3), np.uint8) for _ in range(4)]
    g._front = 0
    g._write = 1
    buf = np.zeros((H, W, 4), np.uint8)
    buf[:, :, 0] = 1  # 整张表面 B=1
    buf[20:36, 10:42, 0] = 9  # ROI 内 B=9
    g.on_frame_arrived(_FakeFrame(buf), _FakeCtl())
    assert g._cb_errors == 0, "ROI 对齐时回调不应吞异常"
    blue = g._bufs[g._front][..., 0]  # BGR 的 B 通道
    assert int(blue.min()) == 9 and int(blue.max()) == 9, "只应包含 ROI 内的像素"


def test_callback_error_is_counted_not_raised():
    """回调里抛异常会打死 native 采集线程，所以必须吞掉 —— 但必须计数。"""
    g = _make()
    g.on_frame_arrived(object(), None)  # 没有 frame_buffer 属性
    assert g._cb_errors == 1
    assert g._n == 1  # 没有新帧入槽


def test_control_captured_once():
    g = _make()
    c1, c2 = _FakeCtl(), _FakeCtl()
    g.on_frame_arrived(_FakeFrame(np.zeros((H, W, 4), np.uint8)), c1)
    g.on_frame_arrived(_FakeFrame(np.zeros((H, W, 4), np.uint8)), c2)
    assert g._control is c1, "僵尸会话不能覆盖活动会话的 control"


# ---------------------------------------------------------------- 健康度
def test_stats_exposes_all_health_fields():
    g = _make()
    s = g.stats
    for key in (
        "backend",
        "frames",
        "heals",
        "max_heals",
        "cb_errors",
        "blank_streak",
        "last_ts",
        "borderless",
        "age_ms",
    ):
        assert key in s, f"stats 缺少 {key}"
    assert s["backend"] == "wgc"
    assert s["age_ms"] is not None and s["age_ms"] < 1000
    assert s["borderless"] is False


def test_stale_frame_triggers_heal_then_returns_fresh():
    """零新帧超过 stale_ms 必须走自愈，而不是继续交同一张老图。"""
    g = _make()
    g._last_frame_t = time.monotonic() - 5.0  # 5s 没新帧
    calls = []

    def _heal(age_ms):
        calls.append(age_ms)
        g._last_frame_t = time.monotonic()  # 模拟重建后立刻有新帧
        g._heals += 1
        return True

    g._maybe_heal = _heal
    img = g.grab()
    assert calls and calls[0] > 3000
    assert img.shape == (H, W, 3)
    assert g._heals == 1


def test_stale_and_unhealable_raises_under_on_stale_raise():
    """自愈救不了时，on_stale="raise" 绝不能静默返回老图。"""
    g = _make()
    g._last_frame_t = time.monotonic() - 5.0
    g._maybe_heal = lambda age: False
    with pytest.raises(StaleFrameError):
        g.grab(on_stale="raise")


def test_stale_and_unhealable_returns_last_under_on_stale_last():
    g = _make(fill=42)
    g._last_frame_t = time.monotonic() - 5.0
    g._maybe_heal = lambda age: False
    img = g.grab(on_stale="last")
    assert int(img[0, 0, 0]) == 42


def test_closed_session_raises_when_raising():
    g = _make()
    g._n = 0
    g._last_img = None
    g._closed = True
    with pytest.raises(StaleFrameError):
        g.grab(on_stale="raise")


def test_closed_session_returns_last_when_lenient():
    g = _make()
    g._last_img = np.full((H, W, 3), 5, np.uint8)
    g._n = 0
    g._closed = True
    assert int(g.grab(on_stale="last")[0, 0, 0]) == 5


# ---------------------------------------------------------------- 黑帧策略
def test_blank_frame_detected_and_counted():
    g = _make(fill=0)
    g.grab()  # grab 内部已经做过一次判定
    assert g._blank_streak == 1
    # 非黑帧必须把计数清零
    assert g._is_blank(np.full((H, W, 3), 50, np.uint8)) is False
    assert g._blank_streak == 0


def test_single_black_frame_is_delivered_but_not_cached():
    """场景切换会闪黑 1 帧：可以照常交付，但绝不能当成有效帧缓存。"""
    g = _make(fill=0)
    g._last_img = np.full((H, W, 3), 77, np.uint8)
    img = g.grab()
    assert int(img.mean()) == 0
    assert int(g._last_img.mean()) == 77, "黑帧不能污染 _last_img"


def test_persistent_blank_falls_back_to_last_valid():
    g = _make(fill=0)
    g._last_img = np.full((H, W, 3), 77, np.uint8)
    img = None
    for _ in range(g._blank_limit):
        img = g.grab()
    assert int(img.mean()) == 77


def test_persistent_blank_raises_under_on_stale_raise():
    g = _make(fill=0)
    with pytest.raises(BlankFrameError):
        for _ in range(g._blank_limit):
            g.grab(on_stale="raise")


def test_blank_without_fallback_raises_explicitly():
    """只有黑帧、又没有可回退的有效帧时必须明确报错，而不是交出黑图。"""
    g = _make(fill=0)
    g._last_img = None
    with pytest.raises(BlankFrameError):
        for _ in range(g._blank_limit):
            g.grab(on_stale="last")


# ---------------------------------------------------------------- 后端选择
@pytest.mark.parametrize("alias", ["gdi", "mss", "bitblt", "desktop"])
def test_backend_aliases_normalise(alias):
    from pywgc.capture import _BACKEND_ALIASES

    assert _BACKEND_ALIASES.get(alias, alias) == "gdi"


def test_uninitialised_instance_defaults_are_safe():
    """类级默认值保证 __new__ 装配的替身也能读属性，不 AttributeError。"""
    assert WindowGrabber._border_ok is False

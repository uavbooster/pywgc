"""Win32 层单元测试：全部可在无真实窗口的机器上跑。

重点覆盖三类"失败时不会抛异常"的调用 —— 这类 bug 只靠 try/except 是抓不到的。
"""

from __future__ import annotations

from pywgc import win32


def test_swp_flags():
    assert win32.SWP_NOSIZE == 0x0001
    assert win32.SWP_NOMOVE == 0x0002
    assert win32.SWP_NOZORDER == 0x0004
    assert win32.SWP_NOACTIVATE == 0x0010
    assert win32.ERROR_ACCESS_DENIED == 5


def test_pick_main_window_prefers_area_over_visible_shell():
    """最小化到托盘时，主窗口 visible=False，只剩一个 160x28 的壳还 visible。

    单纯"可见优先"会选中壳；打分必须让面积压倒可见性。
    """
    wins = [
        {"hwnd": 1, "pid": 9, "title": "App", "visible": False, "w": 1216, "h": 684, "x": 100, "y": 100},
        {"hwnd": 2, "pid": 9, "title": "App", "visible": True, "w": 160, "h": 28, "x": -32000, "y": -32000},
    ]
    picked = win32.pick_main_window(wins)
    assert picked is not None
    assert picked["hwnd"] == 1


def test_pick_main_window_filters_tiny_windows():
    tiny = [{"hwnd": 1, "pid": 9, "title": "x", "visible": True, "w": 50, "h": 20, "x": 0, "y": 0}]
    assert win32.pick_main_window(tiny) is None
    assert win32.pick_main_window([]) is None


def test_move_window_reports_err_instead_of_silent_false():
    """``SetWindowPos`` 失败只返回 0，不抛异常 —— 必须带回 GetLastError。

    这是实测踩过的坑：函数只包 try/except 时，失败会静默返回 False，
    排查时完全看不出是权限问题（err=5）还是句柄无效。
    """
    ok, err = win32.move_window(0, 10, 10)
    assert ok is False
    assert err != 0, "失败时必须给出 GetLastError，不能是 0"


def test_uipi_hint_only_for_access_denied():
    hint = win32.uipi_hint(win32.ERROR_ACCESS_DENIED)
    assert "UIPI" in hint
    assert win32.uipi_hint(0) == ""
    assert win32.uipi_hint(1400) == ""  # ERROR_INVALID_WINDOW_HANDLE 与权限无关


def test_window_ops_blocked_shape():
    """探测必须是零副作用且永不抛异常（会拿无效句柄也照常返回）。"""
    blocked, reason = win32.window_ops_blocked(0)
    assert isinstance(blocked, bool)
    assert isinstance(reason, str)


def test_error_text():
    assert win32.error_text(0) == ""
    assert isinstance(win32.error_text(win32.ERROR_ACCESS_DENIED), str)


def test_geometry_helpers_tolerate_bad_handles():
    """无效句柄返回 None，不抛异常。"""
    assert win32.client_rect(0) is None
    assert win32.frame_rect(0) is None
    assert win32.is_alive(0) is False
    assert win32.is_minimized(0) is False
    assert win32.is_visible(0) is False
    assert win32.is_foreground(0) is False


def test_list_windows_returns_dicts():
    wins = win32.list_windows()
    assert isinstance(wins, list)
    for w in wins[:5]:
        assert set(w) >= {"hwnd", "pid", "title", "visible", "w", "h", "x", "y"}

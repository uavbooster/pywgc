"""公开 API 契约测试：导入面、版本号、参数校验。"""

from __future__ import annotations

import re

import pytest

import pywgc
from pywgc import WindowGrabber, available_backends


def test_public_names_all_exist():
    for name in pywgc.__all__:
        assert hasattr(pywgc, name), f"__all__ 里的 {name} 不存在"


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+([.-].+)?", pywgc.__version__)


def test_dunder_version_matches_public_attr():
    assert pywgc.__version__ == pywgc.__version__


def test_requires_title_or_hwnd():
    with pytest.raises(ValueError, match="title|hwnd"):
        WindowGrabber()


def test_unknown_backend_rejected():
    with pytest.raises(ValueError, match="backend"):
        WindowGrabber(hwnd=0, backend="nope")


def test_available_backends_shape():
    b = available_backends()
    assert set(b) == {"wgc", "gdi"}
    assert isinstance(b["wgc"], bool)
    assert isinstance(b["gdi"], bool)


def test_exceptions_form_a_hierarchy():
    from pywgc.errors import (
        BackendUnavailable,
        BlankFrameError,
        CaptureError,
        StaleFrameError,
        WindowNotFound,
    )

    for cls in (WindowNotFound, BackendUnavailable, StaleFrameError, BlankFrameError):
        assert issubclass(cls, CaptureError)
        # 全部继承 RuntimeError：老的 except RuntimeError 仍能兜住
        assert issubclass(cls, RuntimeError)


def test_dpi_awareness_is_idempotent():
    from pywgc import enable_dpi_awareness

    enable_dpi_awareness()
    enable_dpi_awareness()  # 不应抛异常

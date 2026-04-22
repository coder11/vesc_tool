import os

import numpy as np
import pytest

from examples.imu_signal_bench import format_stats, prefer_qt_xcb_platform, signal_stats


def test_signal_stats_returns_window_metrics() -> None:
    stats = signal_stats(np.array([-2.0, 1.0, 4.0], dtype=np.float64))

    assert stats is not None
    assert stats.mean == pytest.approx(1.0)
    assert stats.std == pytest.approx(np.sqrt(6.0))
    assert stats.rms == pytest.approx(np.sqrt(7.0))
    assert stats.peak_to_peak == pytest.approx(6.0)


def test_signal_stats_handles_empty_values() -> None:
    stats = signal_stats(np.empty(0, dtype=np.float64))

    assert stats is None
    assert (
        format_stats(stats, "g")
        == "mean: n/a | std: n/a | RMS: n/a | peak-to-peak: n/a"
    )


def test_prefer_qt_xcb_platform_replaces_wayland_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland;xcb")

    prefer_qt_xcb_platform()

    assert os.environ["QT_QPA_PLATFORM"] == "xcb"


def test_prefer_qt_xcb_platform_preserves_explicit_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")

    prefer_qt_xcb_platform()

    assert os.environ["QT_QPA_PLATFORM"] == "wayland"

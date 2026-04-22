import os

import numpy as np
import pytest

from examples.imu_signal_bench import (
    format_stats,
    prefer_qt_xcb_platform,
    signal_psd,
    signal_stats,
)


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
        == "mean: n/a\nstd: n/a\nRMS: n/a\npeak-to-peak: n/a"
    )


def test_signal_psd_returns_frequency_bins_and_positive_power() -> None:
    sample_rate_hz = 100.0
    timestamps = np.arange(100, dtype=np.float64) / sample_rate_hz
    values = np.sin(2.0 * np.pi * 10.0 * timestamps)

    psd = signal_psd(timestamps, values)

    assert psd is not None
    frequencies, power = psd
    assert frequencies[0] == pytest.approx(0.0)
    assert frequencies[-1] == pytest.approx(sample_rate_hz / 2.0)
    assert frequencies[int(np.argmax(power))] == pytest.approx(10.0)
    assert np.all(power >= 0.0)


def test_signal_psd_handles_missing_timing() -> None:
    psd = signal_psd(
        np.array([1.0, 1.0], dtype=np.float64),
        np.array([0.0, 1.0], dtype=np.float64),
    )

    assert psd is None


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

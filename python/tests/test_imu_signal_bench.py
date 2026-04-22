import os

import numpy as np
import pytest

from examples.imu_signal_bench import (
    Biquad,
    biquad_config_lowpass,
    biquad_lowpass,
    biquad_lowpass_hz,
    format_stats,
    normalized_biquad_cutoff,
    prefer_qt_xcb_platform,
    signal_stats,
)


def test_biquad_config_lowpass_uses_firmware_coefficients() -> None:
    biquad = Biquad()

    biquad_config_lowpass(biquad, cutoff=0.25, shape=0.707)

    assert biquad.a0 == pytest.approx(0.2928748964374482)
    assert biquad.a1 == pytest.approx(0.5857497928748964)
    assert biquad.a2 == pytest.approx(0.2928748964374482)
    assert biquad.b1 == pytest.approx(0.0, abs=1e-12)
    assert biquad.b2 == pytest.approx(0.17149958574979282)


def test_biquad_lowpass_filters_impulse_causally() -> None:
    values = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float64)

    filtered = biquad_lowpass_hz(
        values,
        cutoff_hz=250.0,
        sample_rate_hz=1000.0,
        shape=0.707,
    )

    np.testing.assert_allclose(
        filtered,
        np.array(
            [
                0.2928748964374482,
                0.5857497928748964,
                0.24264697302191243,
                -0.10045584683107164,
            ],
            dtype=np.float64,
        ),
    )


def test_normalized_biquad_cutoff_uses_sample_rate() -> None:
    assert normalized_biquad_cutoff(
        cutoff_hz=15.9,
        sample_rate_hz=1000.0,
    ) == pytest.approx(0.0159)


def test_biquad_lowpass_rejects_invalid_params() -> None:
    values = np.array([1.0], dtype=np.float64)

    with pytest.raises(ValueError, match="Nyquist"):
        biquad_lowpass_hz(values, cutoff_hz=500.0, sample_rate_hz=1000.0, shape=0.707)

    with pytest.raises(ValueError, match="shape"):
        biquad_lowpass(values, cutoff=0.1, shape=0.0)


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

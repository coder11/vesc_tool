import numpy as np
import pytest

from examples.imu_signal_bench import format_stats, signal_stats


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

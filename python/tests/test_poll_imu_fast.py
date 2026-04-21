import argparse

import numpy as np
import pytest

from examples.poll_imu_fast import (
    AxisPlotHistory,
    AxisSampleBuffer,
    field_value_index,
    parse_accel_axis_arg,
)


def test_field_value_index_matches_compacted_response_values() -> None:
    mask = 0x0038

    assert field_value_index(mask, "acc_x") == 0
    assert field_value_index(mask, "acc_y") == 1
    assert field_value_index(mask, "acc_z") == 2
    assert field_value_index(mask, "gyro_x") is None


def test_parse_accel_axis_accepts_short_and_field_names() -> None:
    assert parse_accel_axis_arg("x") == "acc_x"
    assert parse_accel_axis_arg("accel-z") == "acc_z"

    with pytest.raises(argparse.ArgumentTypeError):
        parse_accel_axis_arg("roll")


def test_axis_sample_buffer_drains_in_order_after_overwrite() -> None:
    buffer = AxisSampleBuffer(3)

    for index in range(5):
        buffer.append(float(index), float(index + 10))

    timestamps, values, dropped = buffer.drain()

    assert timestamps.tolist() == [2.0, 3.0, 4.0]
    assert values.tolist() == [12.0, 13.0, 14.0]
    assert dropped == 2


def test_axis_sample_buffer_appends_batch_with_one_overwrite_window() -> None:
    buffer = AxisSampleBuffer(4)

    buffer.append(0.0, 10.0)
    buffer.append_many((1.0, 2.0, 3.0, 4.0), (11.0, 12.0, 13.0, 14.0))
    timestamps, values, dropped = buffer.drain()

    assert timestamps.tolist() == [1.0, 2.0, 3.0, 4.0]
    assert values.tolist() == [11.0, 12.0, 13.0, 14.0]
    assert dropped == 1


def test_axis_plot_history_keeps_latest_fixed_width_samples() -> None:
    history = AxisPlotHistory(3)

    history.append_samples(
        np.array([0.0, 1.0], dtype=np.float64),
        np.array([10.0, 11.0], dtype=np.float64),
    )
    history.append_samples(
        np.array([2.0, 3.0], dtype=np.float64),
        np.array([12.0, 13.0], dtype=np.float64),
    )

    assert history.count == 3
    assert history.latest_timestamp == 3.0
    assert history.valid_timestamps().tolist() == [1.0, 2.0, 3.0]
    assert history.valid_values().tolist() == [11.0, 12.0, 13.0]
    assert history.sample_hz() == pytest.approx(1.0)

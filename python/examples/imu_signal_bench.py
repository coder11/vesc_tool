#!/usr/bin/env python3
"""Live one-axis IMU signal bench for software filter tuning.

Examples:
    python examples/imu_signal_bench.py --source deterministic --axis acc_z
    python examples/imu_signal_bench.py --source vesc --axis acc_z --pipeline-depth 4
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
import numpy.typing as npt

from vesc_py import list_serial_ports
from vesc_py.fast_imu_source import (
    DEFAULT_BAUDRATE,
    DEFAULT_PIPELINE_DEPTH,
    DEFAULT_TIMEOUT,
    IMU_BENCH_FIELDS,
    VescImuSignalSource,
    imu_axis_unit,
    parse_imu_axis,
)
from vesc_py.live_signal import (
    DeterministicSignalSource,
    SignalRingHistory,
    SignalSource,
    residual,
    trailing_sma,
)

DEFAULT_SMA_WINDOW = 10
DEFAULT_HISTORY = 20000
DEFAULT_MAX_POINTS = 1200
DEFAULT_PLOT_RATE = 30.0
DEFAULT_PENDING_SAMPLES = 20000
DEFAULT_DETERMINISTIC_RATE = 500.0


class PlotTheme:
    """Simple color bundle for the PyQtGraph bench."""

    def __init__(
        self,
        *,
        pg_background: str,
        pg_foreground: str,
        window_background: str,
        text_color: str,
        muted_color: str,
        grid_alpha: float,
        line_colors: tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]],
    ) -> None:
        self.pg_background = pg_background
        self.pg_foreground = pg_foreground
        self.window_background = window_background
        self.text_color = text_color
        self.muted_color = muted_color
        self.grid_alpha = grid_alpha
        self.line_colors = line_colors


PLOT_THEMES = {
    "light": PlotTheme(
        pg_background="#ffffff",
        pg_foreground="#202124",
        window_background="#f6f7f9",
        text_color="#202124",
        muted_color="#4f5b66",
        grid_alpha=0.22,
        line_colors=((196, 57, 54), (28, 128, 75), (37, 98, 180)),
    ),
    "dark": PlotTheme(
        pg_background="#000000",
        pg_foreground="#d0d0d0",
        window_background="#000000",
        text_color="#d0d0d0",
        muted_color="#999999",
        grid_alpha=0.3,
        line_colors=((230, 88, 85), (80, 190, 120), (85, 150, 245)),
    ),
}


@dataclass(frozen=True)
class SignalStats:
    """Summary metrics for the retained signal window."""

    mean: float
    std: float
    rms: float
    peak_to_peak: float


def parse_axis_arg(text: str) -> str:
    """Argparse wrapper for IMU axis parsing."""
    try:
        return parse_imu_axis(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def import_pyqtgraph() -> tuple[Any, Any, Any]:
    """Import PyQtGraph lazily so --help does not require Qt startup."""
    if (
        sys.platform.startswith("linux")
        and "QT_QPA_PLATFORM" not in os.environ
        and "DISPLAY" in os.environ
    ):
        os.environ["QT_QPA_PLATFORM"] = "xcb"

    try:
        import pyqtgraph as pg  # type: ignore[import-untyped]
        from pyqtgraph.Qt import QtCore  # type: ignore[import-untyped]
        from pyqtgraph.Qt import QtWidgets
    except ImportError as exc:
        raise RuntimeError(
            "PyQtGraph plotting requires pyqtgraph and a Qt binding. "
            "Install the project dependencies, or install pyqtgraph and PySide6."
        ) from exc

    return pg, QtCore, QtWidgets


def autodetect_port() -> str:
    """Return the first discovered serial port, preferring VESC-like devices."""
    ports = list_serial_ports()
    if not ports:
        raise SystemExit(
            "No serial ports found. Connect the VESC over USB or pass --port explicitly."
        )
    return ports[0].system_path


def initial_y_range(axis: str) -> tuple[float, float] | None:
    """Return a sensible initial y-range for a known IMU axis."""
    if axis.startswith("acc_"):
        return (-8.0, 8.0)
    if axis.startswith("gyro_"):
        return (-2000.0, 2000.0)
    if axis in ("roll", "pitch", "yaw"):
        return (-200.0, 200.0)
    return None


def decimate_indexes(sample_count: int, max_points: int) -> npt.NDArray[np.intp] | None:
    """Return display indexes after transform computation."""
    if sample_count <= max_points:
        return None
    return cast(
        "npt.NDArray[np.intp]",
        np.linspace(0, sample_count - 1, max_points, dtype=np.intp),
    )


def history_axis_values(sample_count: int, history: int) -> npt.NDArray[np.float64]:
    """Return x positions in fixed history sample slots."""
    if sample_count <= 0:
        return np.empty(0, dtype=np.float64)
    return cast(
        "npt.NDArray[np.float64]",
        np.arange(history - sample_count, history, dtype=np.float64),
    )


def actual_rate(timestamps: deque[float]) -> float | None:
    """Return rate from recent event timestamps."""
    if len(timestamps) < 2:
        return None
    elapsed = timestamps[-1] - timestamps[0]
    if elapsed <= 0.0:
        return None
    return (len(timestamps) - 1) / elapsed


def format_rate(value: float | None) -> str:
    """Format an optional rate for status text."""
    return "measuring" if value is None else f"{value:.1f} Hz"


def format_value(value: float | None, unit: str) -> str:
    """Format an optional signal value for status text."""
    return "n/a" if value is None else f"{value:.6g} {unit}"


def signal_stats(values: npt.NDArray[np.float64]) -> SignalStats | None:
    """Return summary metrics for the supplied signal values."""
    if values.size == 0:
        return None
    return SignalStats(
        mean=float(np.mean(values)),
        std=float(np.std(values)),
        rms=float(np.sqrt(np.mean(values * values))),
        peak_to_peak=float(np.ptp(values)),
    )


def format_stats(stats: SignalStats | None, unit: str) -> str:
    """Format optional signal metrics for status text."""
    if stats is None:
        return (
            "mean: n/a | std: n/a | RMS: n/a | "
            "peak-to-peak: n/a"
        )
    return (
        f"mean: {stats.mean:.6g} {unit} | "
        f"std: {stats.std:.6g} {unit} | "
        f"RMS: {stats.rms:.6g} {unit} | "
        f"peak-to-peak: {stats.peak_to_peak:.6g} {unit}"
    )


def run_signal_bench(
    source: SignalSource,
    *,
    source_label: str,
    initial_sma_window: int,
    history: int,
    max_points: int,
    plot_rate: float,
    theme: str,
    antialias: bool,
) -> None:
    """Run the PyQtGraph live signal bench."""
    selected_theme = PLOT_THEMES[theme]
    pg, QtCore, QtWidgets = import_pyqtgraph()
    pg.setConfigOptions(
        antialias=antialias,
        background=selected_theme.pg_background,
        foreground=selected_theme.pg_foreground,
    )

    app = pg.mkQApp("VESC IMU Signal Bench")
    window = QtWidgets.QWidget()
    window.setWindowTitle("VESC IMU Signal Bench")
    window.resize(1280, 720)
    window.setStyleSheet(f"background-color: {selected_theme.window_background};")

    qt_alignment = getattr(QtCore.Qt, "AlignmentFlag", QtCore.Qt)
    qt_size_policy = getattr(QtWidgets.QSizePolicy, "Policy", QtWidgets.QSizePolicy)

    root = QtWidgets.QVBoxLayout(window)
    root.setContentsMargins(8, 8, 8, 8)
    root.setSpacing(8)

    controls = QtWidgets.QHBoxLayout()
    controls.setContentsMargins(0, 0, 0, 0)
    controls.setSpacing(8)
    root.addLayout(controls)

    source_text = QtWidgets.QLabel(source_label)
    source_text.setStyleSheet(f"color: {selected_theme.text_color}; font-weight: 700;")
    controls.addWidget(source_text)

    axis_text = QtWidgets.QLabel(f"{source.channel_name} ({source.unit})")
    axis_text.setStyleSheet(f"color: {selected_theme.muted_color};")
    controls.addWidget(axis_text)

    controls.addSpacing(12)
    controls.addWidget(QtWidgets.QLabel("SMA"))
    sma_spin = QtWidgets.QSpinBox()
    sma_spin.setRange(1, 100000)
    sma_spin.setValue(initial_sma_window)
    controls.addWidget(sma_spin)

    mode_group = QtWidgets.QButtonGroup(window)
    mode_group.setExclusive(True)
    mode_buttons: dict[str, Any] = {}
    for mode_name in ("raw", "filtered", "both", "residual"):
        button = QtWidgets.QPushButton(mode_name.title())
        button.setCheckable(True)
        if mode_name == "both":
            button.setChecked(True)
        mode_group.addButton(button)
        mode_buttons[mode_name] = button
        controls.addWidget(button)

    clear_button = QtWidgets.QPushButton("Clear")
    controls.addWidget(clear_button)
    controls.addStretch(1)

    plot_widget = pg.PlotWidget(title="Time Series")
    plot_widget.setMinimumSize(0, 0)
    plot_widget.setSizePolicy(qt_size_policy.Ignored, qt_size_policy.Ignored)
    root.addWidget(plot_widget, stretch=1)

    status = QtWidgets.QLabel("Waiting for signal data...")
    status.setAlignment(qt_alignment.AlignLeft)
    status.setWordWrap(True)
    status.setStyleSheet(f"color: {selected_theme.muted_color};")
    root.addWidget(status)

    plot = plot_widget.getPlotItem()
    plot.showGrid(x=True, y=True, alpha=selected_theme.grid_alpha)
    plot.setLabel("left", source.channel_name, units=source.unit)
    plot.setLabel("bottom", "history", units="samples")
    plot.setMouseEnabled(x=False, y=True)
    plot.setXRange(0.0, float(history), padding=0.0)
    plot.setLimits(
        xMin=0.0,
        xMax=float(history),
        minXRange=float(history),
        maxXRange=float(history),
    )
    y_range = initial_y_range(source.channel_name)
    if y_range is not None:
        plot.setYRange(*y_range, padding=0.0)
        plot.enableAutoRange(axis="y", enable=False)
    for method_name, args in (
        ("setClipToView", (True,)),
        ("setDownsampling", (1, True, "peak")),
    ):
        method = getattr(plot, method_name, None)
        if method is not None:
            method(*args)

    empty = np.empty(0, dtype=np.float64)
    raw_line = pg.PlotCurveItem(
        empty,
        empty,
        pen=pg.mkPen(selected_theme.line_colors[0], width=1.3),
        name="Raw",
        connect="all",
        skipFiniteCheck=True,
    )
    filtered_line = pg.PlotCurveItem(
        empty,
        empty,
        pen=pg.mkPen(selected_theme.line_colors[1], width=1.6),
        name="Filtered",
        connect="all",
        skipFiniteCheck=True,
    )
    residual_line = pg.PlotCurveItem(
        empty,
        empty,
        pen=pg.mkPen(selected_theme.line_colors[2], width=1.4),
        name="Residual",
        connect="all",
        skipFiniteCheck=True,
    )
    for line in (raw_line, filtered_line, residual_line):
        line.setSkipFiniteCheck(True)
        plot.addItem(line)
    legend = plot.addLegend(offset=(10, 10))
    zero_line = pg.InfiniteLine(
        pos=0.0,
        angle=0,
        pen=pg.mkPen(selected_theme.muted_color, width=1),
    )
    zero_line.setVisible(False)
    plot.addItem(zero_line)

    signal_history = SignalRingHistory(history)
    refresh_timestamps: deque[float] = deque(maxlen=120)
    dropped_pending = 0
    latest_raw: float | None = None
    latest_filtered: float | None = None
    latest_residual: float | None = None
    latest_stats: SignalStats | None = None

    def selected_mode() -> str:
        for mode_name, button in mode_buttons.items():
            if button.isChecked():
                return mode_name
        return "both"

    def clear_history() -> None:
        nonlocal latest_raw, latest_filtered, latest_residual, latest_stats
        signal_history.clear()
        latest_raw = None
        latest_filtered = None
        latest_residual = None
        latest_stats = None
        for line in (raw_line, filtered_line, residual_line):
            line.setData(x=empty, y=empty, connect="all", skipFiniteCheck=True)

    def set_visible_lines(mode_name: str) -> None:
        raw_line.setVisible(mode_name in ("raw", "both"))
        filtered_line.setVisible(mode_name in ("filtered", "both"))
        residual_line.setVisible(mode_name == "residual")
        zero_line.setVisible(mode_name == "residual")
        legend.setVisible(mode_name == "both")

    def refresh_plot() -> None:
        nonlocal dropped_pending, latest_raw, latest_filtered, latest_residual, latest_stats

        timestamps, values, dropped = source.drain()
        dropped_pending += dropped
        if timestamps.size > 0:
            refresh_timestamps.append(time.monotonic())
            signal_history.append_samples(timestamps, values)

        raw_values = signal_history.valid_values()
        if raw_values.size == 0:
            set_visible_lines(selected_mode())
            return

        x_values = history_axis_values(int(raw_values.size), history)
        filtered_values = trailing_sma(raw_values, int(sma_spin.value()))
        residual_values = residual(raw_values, filtered_values)
        latest_raw = float(raw_values[-1])
        latest_filtered = float(filtered_values[-1])
        latest_residual = float(residual_values[-1])
        latest_stats = signal_stats(raw_values)

        indexes = decimate_indexes(int(x_values.size), max_points)
        if indexes is not None:
            plot_x_values = x_values[indexes]
            plot_raw_values = raw_values[indexes]
            plot_filtered_values = filtered_values[indexes]
            plot_residual_values = residual_values[indexes]
        else:
            plot_x_values = x_values
            plot_raw_values = raw_values
            plot_filtered_values = filtered_values
            plot_residual_values = residual_values

        raw_line.setData(
            x=plot_x_values,
            y=plot_raw_values,
            connect="all",
            skipFiniteCheck=True,
        )
        filtered_line.setData(
            x=plot_x_values,
            y=plot_filtered_values,
            connect="all",
            skipFiniteCheck=True,
        )
        residual_line.setData(
            x=plot_x_values,
            y=plot_residual_values,
            connect="all",
            skipFiniteCheck=True,
        )
        set_visible_lines(selected_mode())
        plot.setXRange(0.0, float(history), padding=0.0)

    def refresh_status() -> None:
        snapshot = source.snapshot()
        history_hz = signal_history.sample_hz()
        plot_hz = actual_rate(refresh_timestamps)
        state = "stopped" if snapshot.done else "running"
        error_text = (
            f" | error: {snapshot.last_error}"
            if snapshot.last_error is not None
            else ""
        )
        status.setText(
            f"{state} | raw: {format_value(latest_raw, source.unit)} | "
            f"filtered: {format_value(latest_filtered, source.unit)} | "
            f"residual: {format_value(latest_residual, source.unit)} | "
            f"{format_stats(latest_stats, source.unit)} | "
            f"sma: {int(sma_spin.value())} | samples: {snapshot.samples} | "
            f"source avg: {snapshot.average_rate_hz:.1f} Hz | "
            f"history: {format_rate(history_hz)} | plot: {format_rate(plot_hz)} | "
            f"dropped: {snapshot.dropped + dropped_pending} | errors: {snapshot.errors}"
            f"{error_text}"
        )

    plot_timer = QtCore.QTimer()
    plot_timer.setInterval(round(1000.0 / plot_rate))
    plot_timer.timeout.connect(refresh_plot)

    status_timer = QtCore.QTimer()
    status_timer.setInterval(250)
    status_timer.timeout.connect(refresh_status)

    def stop_updates(*_args: object) -> None:
        plot_timer.stop()
        status_timer.stop()

    clear_button.clicked.connect(clear_history)
    for button in mode_buttons.values():
        button.clicked.connect(lambda _checked=False: set_visible_lines(selected_mode()))
    window.destroyed.connect(stop_updates)

    set_visible_lines("both")
    refresh_plot()
    refresh_status()
    window.show()
    source.start()
    plot_timer.start()
    status_timer.start()

    exec_app = getattr(app, "exec", None)
    if exec_app is None:
        exec_app = app.exec_
    try:
        exec_app()
    finally:
        source.stop()


def build_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Live one-axis IMU signal bench for software filter tuning.",
    )
    parser.add_argument(
        "--source",
        choices=("vesc", "deterministic"),
        default="vesc",
        help="Signal source to use (default: vesc).",
    )
    parser.add_argument(
        "--port",
        help="Serial port path for --source vesc. If omitted, autodetect is used.",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=DEFAULT_BAUDRATE,
        help=f"Serial baudrate for --source vesc (default: {DEFAULT_BAUDRATE}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SEC",
        help=f"Per-response timeout for --source vesc (default: {DEFAULT_TIMEOUT:g}).",
    )
    parser.add_argument(
        "--pipeline-depth",
        type=int,
        default=DEFAULT_PIPELINE_DEPTH,
        metavar="N",
        help=(
            "Outstanding IMU requests for --source vesc "
            f"(default: {DEFAULT_PIPELINE_DEPTH})."
        ),
    )
    parser.add_argument(
        "--axis",
        type=parse_axis_arg,
        default="acc_x",
        metavar="AXIS",
        help=f"IMU axis to capture. Valid: {', '.join(IMU_BENCH_FIELDS)}.",
    )
    parser.add_argument(
        "--sma-window",
        type=int,
        default=DEFAULT_SMA_WINDOW,
        metavar="N",
        help=f"Initial SMA sample window (default: {DEFAULT_SMA_WINDOW}).",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=DEFAULT_HISTORY,
        metavar="N",
        help=f"Retained raw sample history (default: {DEFAULT_HISTORY}).",
    )
    parser.add_argument(
        "--max-points",
        type=int,
        default=DEFAULT_MAX_POINTS,
        metavar="N",
        help=f"Maximum plotted points after decimation (default: {DEFAULT_MAX_POINTS}).",
    )
    parser.add_argument(
        "--plot-rate",
        type=float,
        default=DEFAULT_PLOT_RATE,
        metavar="HZ",
        help=f"Plot redraw rate (default: {DEFAULT_PLOT_RATE:g}).",
    )
    parser.add_argument(
        "--pending-samples",
        type=int,
        default=DEFAULT_PENDING_SAMPLES,
        metavar="N",
        help=(
            "Pending sample ring size between source and UI "
            f"(default: {DEFAULT_PENDING_SAMPLES})."
        ),
    )
    parser.add_argument(
        "--theme",
        choices=tuple(PLOT_THEMES),
        default="light",
        help="Plot theme (default: light).",
    )
    parser.add_argument(
        "--antialias",
        action="store_true",
        help="Enable PyQtGraph antialiasing. Disabled by default for speed.",
    )
    parser.add_argument(
        "--no-exclusive",
        action="store_true",
        help="Do not request exclusive serial access for --source vesc.",
    )
    parser.add_argument(
        "--deterministic-rate",
        type=float,
        default=DEFAULT_DETERMINISTIC_RATE,
        metavar="HZ",
        help=(
            "Sample rate for --source deterministic "
            f"(default: {DEFAULT_DETERMINISTIC_RATE:g})."
        ),
    )
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Validate parsed arguments with concrete CLI errors."""
    if args.baudrate <= 0:
        parser.error("--baudrate must be greater than 0")
    if args.timeout <= 0.0:
        parser.error("--timeout must be greater than 0")
    if args.pipeline_depth <= 0:
        parser.error("--pipeline-depth must be greater than 0")
    if args.sma_window <= 0:
        parser.error("--sma-window must be greater than 0")
    if args.history <= 0:
        parser.error("--history must be greater than 0")
    if args.max_points <= 0:
        parser.error("--max-points must be greater than 0")
    if args.plot_rate <= 0.0:
        parser.error("--plot-rate must be greater than 0")
    if args.pending_samples <= 0:
        parser.error("--pending-samples must be greater than 0")
    if args.deterministic_rate <= 0.0:
        parser.error("--deterministic-rate must be greater than 0")


def make_source(args: argparse.Namespace) -> tuple[SignalSource, str]:
    """Construct the requested signal source."""
    axis = cast(str, args.axis)
    if args.source == "deterministic":
        return (
            DeterministicSignalSource(
                channel_name=axis,
                unit=imu_axis_unit(axis),
                sample_rate_hz=cast(float, args.deterministic_rate),
                pending_samples=cast(int, args.pending_samples),
            ),
            f"Deterministic source @ {args.deterministic_rate:g} Hz",
        )

    port = cast(str | None, args.port) or autodetect_port()
    return (
        VescImuSignalSource(
            port=port,
            baudrate=cast(int, args.baudrate),
            axis=axis,
            timeout=cast(float, args.timeout),
            pipeline_depth=cast(int, args.pipeline_depth),
            pending_samples=cast(int, args.pending_samples),
            exclusive=not cast(bool, args.no_exclusive),
        ),
        f"VESC serial source: {port}",
    )


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    source, source_label = make_source(args)
    run_signal_bench(
        source,
        source_label=source_label,
        initial_sma_window=cast(int, args.sma_window),
        history=cast(int, args.history),
        max_points=cast(int, args.max_points),
        plot_rate=cast(float, args.plot_rate),
        theme=cast(str, args.theme),
        antialias=cast(bool, args.antialias),
    )


if __name__ == "__main__":
    main()

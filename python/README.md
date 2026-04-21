# vesc-py

Pure-Python programmatic API for talking to VESC hardware over serial or TCP.
No C++dependencies -- the VESC binary protocol (framing, CRC, serialization)
is reimplemented from the vesc_tool C++ source.

## Features

- **Device discovery** -- UDP broadcast listener (port 65109) and serial port
scanning with STM/ESP heuristics
- **Serial and TCP transports** -- 115200 8N1 serial or plain TCP sockets
- **Firmware handshake** -- automatic version detection on connect
- **IMU streaming** -- request and parse live IMU data (roll/pitch/yaw,
accelerometer, gyroscope, magnetometer, quaternion) with configurable field
masks
- **App configuration** -- read and write APPCONF from firmware, with automatic
XML schema loading matched to the detected firmware version
- **Strict typing** -- `py.typed`, passes `mypy --strict`, Pydantic models for
all public data structures

## Quick start

### Nix (recommended)

```bash
cd python
nix develop
uv sync --extra dev
uv run poe test    # or: uv run pytest
```

The Nix devShell adds `uv` and, on Linux, sets `LD_LIBRARY_PATH` so PyPI binary
wheels (e.g. NumPy) can find `libstdc++.so.6`. Python and packages still come
from `uv sync` (`pyproject.toml` / `uv.lock`).

### uv / pip

```bash
cd python
uv sync          # or: pip install -e ".[dev]"
pytest
```

## Usage

### Connect and read IMU data

```python
from vesc_py import VescClient

# Serial
client = VescClient.connect_serial("/dev/ttyACM0")

# Or TCP
client = VescClient.connect_tcp("192.168.1.100", 65102)

print(client.fw_version)

imu = client.get_imu_data()
print(f"Roll: {imu.roll:.2f}  Pitch: {imu.pitch:.2f}  Yaw: {imu.yaw:.2f}")

client.close()
```

### Read and modify app configuration

```python
conf = client.get_appconf()
print(conf["controller_id"])

conf["controller_id"] = 42
client.set_appconf(conf)
```

### Discover devices

```python
from vesc_py import list_serial_ports, udp_scan

# Serial ports (VESC/ESP ports sorted first)
for port in list_serial_ports():
    print(port.name, port.system_path, port.is_vesc)

# UDP broadcast announcements
for device in udp_scan(timeout=3.0):
    print(device.hw_name, device.ip, device.port)
```

### Live IMU plot

```bash
vesc_tool --offscreen --vescPort /dev/ttyACM0 --tcpServer 65102
python examples/imu_live_plot.py
python examples/imu_live_plot.py --tcp 192.168.1.100:65102
python examples/imu_live_plot.py --scan-udp
```

The PyQtGraph live plot uses the VESC Tool `--tcpServer` bridge by default at
`127.0.0.1:65102`. The `--mask` flag controls which IMU fields are requested
(default `0x01ff` = roll/pitch/yaw + accelerometer + gyroscope).

### Fast USB IMU poller

```bash
python examples/poll_imu_fast.py --port /dev/ttyACM0
python examples/poll_imu_fast.py --duration 10 --pipeline-depth 4
python examples/poll_imu_fast.py --ui-rate 10
python examples/poll_imu_fast.py --no-tui --status-interval 1
python examples/poll_imu_fast.py --fields rpy,acc,gyro --csv > imu.csv
```

`poll_imu_fast.py` talks directly to the VESC USB serial port and keeps the
hot path small for maximum poll rate: it pre-encodes the IMU request, validates
packets with CRC, decodes values without Pydantic models, and avoids per-sample
printing by default. In an interactive terminal it shows an in-place line-by-line
IMU view at a capped UI rate while measuring the real poll rate from every
received sample. Use `--pipeline-depth` to keep multiple requests in flight over
USB, and use `--mask` or `--fields` to reduce the response size when only some
IMU channels are needed.

### IMU setup wizard

```bash
vesc_tool --offscreen --vescPort /dev/ttyACM0 --tcpServer 65102
python examples/imu_setup.py
python examples/imu_setup.py --tcp 192.168.1.100:65102
python examples/imu_setup.py --scan-udp
python examples/imu_setup_gui.py
```

The setup wizard mirrors the VESC Tool IMU setup flow: basic IMU parameters,
gyro offsets, accelerometer offsets, and orientation calibration. Use
`--skip-basic`, `--skip-gyro`, `--skip-accel`, or `--skip-orientation` to run
only part of the flow. In interactive mode the gyro, accelerometer, and
orientation steps keep sampling until you press `s`/Enter to save, `r` to retry,
`k` to skip, or `c` to cancel. Calibration readouts and saved calibration values
use a 3 second rolling mean by default; pass `--mean-seconds 0` to disable it or
another value to tune the window.

`imu_setup_gui.py` provides a PySide6 clone of the VESC Tool IMU wizard with the
same detector/profile, gyro, accelerometer, and orientation pages.

## Project structure

```
python/
  pyproject.toml          # package metadata, dependencies
  flake.nix               # Nix devShell
  src/vesc_py/
    __init__.py            # public API re-exports
    crc.py                 # CRC-16/XMODEM + CRC-32C
    buffer.py              # big-endian binary serialization (VescBuffer)
    packet.py              # VESC packet framing (encode + streaming decode)
    comm_ids.py            # COMM_PACKET_ID enum subset
    models.py              # Pydantic models (FwVersion, ImuValues, ...)
    fw_version.py          # COMM_FW_VERSION build/parse
    imu.py                 # COMM_GET_IMU_DATA build/parse
    imu_setup.py           # IMU setup profiles and calibration math
    appconf.py             # APPCONF XML loading, signature, serialize/deserialize
    config_paths.py        # firmware version -> config XML resolution
    discovery.py           # UDP scan + serial port listing
    client.py              # VescClient with serial/TCP transports
  tests/                   # unit tests (pytest, no hardware required)
  examples/
    imu_live_plot.py       # PyQtGraph live IMU plot
    poll_imu_fast.py       # Direct USB serial high-rate IMU poller
    imu_setup.py           # Interactive IMU setup wizard
    imu_setup_gui.py       # PySide6 IMU setup wizard
```

## Protocol notes

The binary protocol is reimplemented from the vesc_tool C++ source:

- **Packet framing** (`packet.cpp`) -- start byte (2/3/4), 1-3 byte big-endian
length, payload, CRC-16/XMODEM, stop byte 0x03
- **Serialization** (`vbytearray.cpp`) -- big-endian integers, scaled
fixed-point doubles, and a custom `double32_auto` 32-bit float encoding
(not IEEE 754)
- **Config signature** (`configparams.cpp`) -- CRC-32C over parameter names,
types, wire types, and enum labels in serialization order
- **Config XML** -- `res/config/<major>.<minor>/parameters_appconf.xml` with
`_o`_ multi-version directory aliases

## Development scripts

Dev tasks use [poethepoet](https://github.com/nat-n/poethepoet) (`[tool.poe.tasks]` in `pyproject.toml`):

```bash
uv sync --extra dev

uv run poe lint        # pylint on the library
uv run poe typecheck   # mypy --strict on library + examples
uv run poe test        # pytest
uv run poe check       # lint, then typecheck, then test (stops on first failure)
```

## License

Same as the parent vesc_tool repository (GPL-3.0).

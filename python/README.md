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
nix develop .#python
pytest
```

The Nix devShell provides Python 3.12 with all dependencies pre-installed and
sets `PYTHONPATH` to `src/` automatically.

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
python examples/imu_live_plot.py --serial /dev/ttyACM0
python examples/imu_live_plot.py --tcp 192.168.1.100:65102
python examples/imu_live_plot.py --scan-serial
python examples/imu_live_plot.py --scan-udp
```

The `--mask` flag controls which IMU fields are requested (default `0x1FF` =
roll/pitch/yaw + accelerometer + gyroscope).

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
    appconf.py             # APPCONF XML loading, signature, serialize/deserialize
    config_paths.py        # firmware version -> config XML resolution
    discovery.py           # UDP scan + serial port listing
    client.py              # VescClient with serial/TCP transports
  tests/                   # unit tests (pytest, no hardware required)
  examples/
    imu_live_plot.py       # matplotlib live IMU plot
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
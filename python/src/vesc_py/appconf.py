"""App configuration: XML loading, signature, serialize/deserialize.

Matches configparams.cpp logic for loading parameters_appconf.xml, computing
the CRC32C signature, and binary (de)serialization in SerOrder.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from typing import SupportsFloat, SupportsInt, Union, cast

from vesc_py.buffer import VescBuffer
from vesc_py.crc import crc32c


class CfgType(IntEnum):
    UNDEFINED = 0
    DOUBLE = 1
    INT = 2
    QSTRING = 3
    ENUM = 4
    BOOL = 5
    BITFIELD = 6


class VescTx(IntEnum):
    UNDEFINED = 0
    UINT8 = 1
    INT8 = 2
    UINT16 = 3
    INT16 = 4
    UINT32 = 5
    INT32 = 6
    DOUBLE16 = 7
    DOUBLE32 = 8
    DOUBLE32_AUTO = 9


@dataclass
class ConfigParam:
    name: str = ""
    type: CfgType = CfgType.UNDEFINED
    vTx: VescTx = VescTx.UNDEFINED
    vTx_double_scale: float = 0.0
    val_double: float = 0.0
    val_int: int = 0
    val_string: str = ""
    enum_names: list[str] = field(default_factory=list)
    max_double: float = 99.0
    min_double: float = 0.0
    max_int: int = 99
    min_int: int = 0
    max_len: int = 0
    transmittable: bool = True


@dataclass
class AppConfSchema:
    params: dict[str, ConfigParam] = field(default_factory=dict)
    ser_order: list[str] = field(default_factory=list)


def load_appconf_xml(path: Path) -> AppConfSchema:
    """Parse a parameters_appconf.xml file into an AppConfSchema."""
    tree = ET.parse(path)
    root = tree.getroot()

    schema = AppConfSchema()

    params_el = root.find("Params")
    if params_el is not None:
        for param_el in params_el:
            p = ConfigParam(name=param_el.tag)

            for child in param_el:
                tag = child.tag
                text = child.text or ""

                if tag == "type":
                    p.type = CfgType(int(text))
                elif tag == "vTx":
                    p.vTx = VescTx(int(text))
                elif tag == "vTxDoubleScale":
                    p.vTx_double_scale = float(text)
                elif tag == "valDouble":
                    p.val_double = float(text)
                elif tag == "valInt":
                    p.val_int = int(text)
                elif tag == "valString":
                    p.val_string = text
                elif tag == "enumNames":
                    p.enum_names.append(text)
                elif tag == "maxDouble":
                    p.max_double = float(text)
                elif tag == "minDouble":
                    p.min_double = float(text)
                elif tag == "maxInt":
                    p.max_int = int(text)
                elif tag == "minInt":
                    p.min_int = int(text)
                elif tag == "maxLen":
                    p.max_len = int(text)
                elif tag == "transmittable":
                    p.transmittable = bool(int(text))

            schema.params[p.name] = p

    ser_order_el = root.find("SerOrder")
    if ser_order_el is not None:
        for ser_el in ser_order_el:
            if ser_el.tag == "ser" and ser_el.text:
                schema.ser_order.append(ser_el.text)

    return schema


def signature(schema: AppConfSchema) -> int:
    """Compute CRC32C signature matching ConfigParams::getSignature()."""
    sig_str = ""
    for name in schema.ser_order:
        sig_str += name
        p = schema.params.get(name)
        if p is not None:
            sig_str += str(int(p.type))
            sig_str += str(int(p.vTx))
            for en in p.enum_names:
                sig_str += en

    return crc32c(sig_str.encode("utf-8"))


_Scalar = Union[int, float, str, bool]


def _as_float(val: object) -> float:
    return float(cast(SupportsFloat, val))


def _as_int(val: object) -> int:
    return int(cast(SupportsInt, val))


def serialize_appconf(schema: AppConfSchema, values: dict[str, object]) -> bytes:
    """Serialize app config values into a binary blob (signature + fields in SerOrder)."""
    buf = VescBuffer()
    buf.append_uint32(signature(schema))

    for name in schema.ser_order:
        p = schema.params.get(name)
        if p is None:
            raise KeyError(f"Parameter {name!r} in SerOrder not found in schema")

        val = values.get(name)

        if p.type == CfgType.DOUBLE:
            fval = _as_float(val) if val is not None else p.val_double
            if p.vTx == VescTx.DOUBLE16:
                buf.append_double16(fval, p.vTx_double_scale)
            elif p.vTx == VescTx.DOUBLE32:
                buf.append_double32(fval, p.vTx_double_scale)
            elif p.vTx == VescTx.DOUBLE32_AUTO:
                buf.append_double32_auto(fval)
            else:
                raise ValueError(f"Parameter {name!r}: unsupported vTx {p.vTx} for DOUBLE")

        elif p.type == CfgType.INT:
            ival = _as_int(val) if val is not None else p.val_int
            if p.vTx == VescTx.UINT8:
                buf.append_uint8(ival)
            elif p.vTx == VescTx.INT8:
                buf.append_int8(ival)
            elif p.vTx == VescTx.UINT16:
                buf.append_uint16(ival)
            elif p.vTx == VescTx.INT16:
                buf.append_int16(ival)
            elif p.vTx == VescTx.UINT32:
                buf.append_uint32(ival)
            elif p.vTx == VescTx.INT32:
                buf.append_int32(ival)
            else:
                raise ValueError(f"Parameter {name!r}: unsupported vTx {p.vTx} for INT")

        elif p.type == CfgType.QSTRING:
            sval = str(val) if val is not None else p.val_string
            buf.append_string(sval)

        elif p.type in (CfgType.ENUM, CfgType.BOOL, CfgType.BITFIELD):
            ival = _as_int(val) if val is not None else p.val_int
            buf.append_int8(ival)

        elif p.type == CfgType.UNDEFINED:
            raise ValueError(f"Parameter {name!r}: type is UNDEFINED")

    return buf.to_bytes()


def deserialize_appconf(schema: AppConfSchema, data: bytes) -> dict[str, object]:
    """Deserialize a binary app config blob, verifying the signature."""
    buf = VescBuffer(data)
    sig = buf.pop_uint32()
    expected = signature(schema)
    if sig != expected:
        raise ValueError(
            f"Signature mismatch: got 0x{sig:08X}, expected 0x{expected:08X}"
        )

    values: dict[str, object] = {}
    for name in schema.ser_order:
        p = schema.params.get(name)
        if p is None:
            raise KeyError(f"Parameter {name!r} in SerOrder not found in schema")

        if p.type == CfgType.DOUBLE:
            if p.vTx == VescTx.DOUBLE16:
                values[name] = buf.pop_double16(p.vTx_double_scale)
            elif p.vTx == VescTx.DOUBLE32:
                values[name] = buf.pop_double32(p.vTx_double_scale)
            elif p.vTx == VescTx.DOUBLE32_AUTO:
                values[name] = buf.pop_double32_auto()
            else:
                raise ValueError(f"Parameter {name!r}: unsupported vTx {p.vTx} for DOUBLE")

        elif p.type in (CfgType.INT, CfgType.BITFIELD):
            # BITFIELD always deserializes as uint8 regardless of vTx
            if p.vTx == VescTx.UINT8 or p.type == CfgType.BITFIELD:
                values[name] = buf.pop_uint8()
            elif p.vTx == VescTx.INT8:
                values[name] = buf.pop_int8()
            elif p.vTx == VescTx.UINT16:
                values[name] = buf.pop_uint16()
            elif p.vTx == VescTx.INT16:
                values[name] = buf.pop_int16()
            elif p.vTx == VescTx.UINT32:
                values[name] = buf.pop_uint32()
            elif p.vTx == VescTx.INT32:
                values[name] = buf.pop_int32()
            else:
                raise ValueError(f"Parameter {name!r}: unsupported vTx {p.vTx} for INT")

        elif p.type == CfgType.QSTRING:
            values[name] = buf.pop_string()

        elif p.type in (CfgType.ENUM, CfgType.BOOL):
            values[name] = buf.pop_int8()

        elif p.type == CfgType.UNDEFINED:
            raise ValueError(f"Parameter {name!r}: type is UNDEFINED")

    return values

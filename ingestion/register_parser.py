"""
ingestion/register_parser.py
Modbus register parser supporting INT16_SIGNED, INT16_UNSIGNED,
INT32_BE, INT32_LE, FLOAT32_BE.
"""
from __future__ import annotations
import struct
from typing import List


# Secure Elite 440 register map: (address, parameter, type, scale)
SECURE_ELITE_440_MAP = [
    (0,  "voltage_l1",          "INT16_SIGNED",   0.1),
    (2,  "voltage_l2",          "INT16_SIGNED",   0.1),
    (4,  "voltage_l3",          "INT16_SIGNED",   0.1),
    (6,  "current_l1",          "INT16_SIGNED",   0.01),
    (12, "kw",                  "INT32_BE",       0.001),
    (16, "kvar",                "INT32_BE",       0.001),
    (20, "kva",                 "INT32_BE",       0.001),
    (24, "pf",                  "INT16_SIGNED",   0.001),
    (28, "frequency",           "INT16_SIGNED",   0.01),
    (32, "kwh_cumulative",      "INT32_BE",       0.001),
    (36, "kvah_cumulative",     "INT32_BE",       0.001),
]


def parse_int16_signed(registers: List[int], offset: int) -> int:
    """Parse one register as signed 16-bit integer."""
    raw = registers[offset] & 0xFFFF
    if raw >= 0x8000:
        raw -= 0x10000
    return raw


def parse_int16_unsigned(registers: List[int], offset: int) -> int:
    """Parse one register as unsigned 16-bit integer."""
    return registers[offset] & 0xFFFF


def parse_int32_be(registers: List[int], offset: int) -> int:
    """Parse two registers as big-endian signed 32-bit integer (high word first)."""
    high = registers[offset] & 0xFFFF
    low = registers[offset + 1] & 0xFFFF
    raw = (high << 16) | low
    # Treat as signed 32-bit
    if raw >= 0x80000000:
        raw -= 0x100000000
    return raw


def parse_int32_le(registers: List[int], offset: int) -> int:
    """Parse two registers as little-endian signed 32-bit integer (low word first)."""
    low = registers[offset] & 0xFFFF
    high = registers[offset + 1] & 0xFFFF
    raw = (high << 16) | low
    if raw >= 0x80000000:
        raw -= 0x100000000
    return raw


def parse_float32_be(registers: List[int], offset: int) -> float:
    """Parse two registers as IEEE 754 big-endian float."""
    high = registers[offset] & 0xFFFF
    low = registers[offset + 1] & 0xFFFF
    raw_bytes = struct.pack(">HH", high, low)
    value, = struct.unpack(">f", raw_bytes)
    return value


def parse_register(
    registers: List[int],
    offset: int,
    reg_type: str,
    scale: float,
) -> float:
    """
    Parse a register at offset with the given type and apply scale factor.
    """
    if reg_type == "INT16_SIGNED":
        raw = parse_int16_signed(registers, offset)
    elif reg_type == "INT16_UNSIGNED":
        raw = parse_int16_unsigned(registers, offset)
    elif reg_type == "INT32_BE":
        raw = parse_int32_be(registers, offset)
    elif reg_type == "INT32_LE":
        raw = parse_int32_le(registers, offset)
    elif reg_type == "FLOAT32_BE":
        return parse_float32_be(registers, offset) * scale
    else:
        raise ValueError(f"Unsupported register type: {reg_type}")
    return raw * scale


def parse_secure_elite_440(registers: List[int]) -> dict:
    """
    Parse Secure Elite 440 register block (minimum 38 registers from address 0).
    Returns dict of parameter_name → value (physical units).
    Always verify against meter display on first deployment.
    """
    if len(registers) < 38:
        raise ValueError(
            f"Insufficient registers: got {len(registers)}, need >= 38 for Secure Elite 440."
        )
    result = {}
    for addr, param, reg_type, scale in SECURE_ELITE_440_MAP:
        result[param] = parse_register(registers, addr, reg_type, scale)
    return result

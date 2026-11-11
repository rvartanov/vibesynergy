"""Minimal FIT encoder for building synthetic test files (no real athlete data in the repo).

Implements just enough of the FIT protocol: file header, definition and data messages,
developer fields and CRCs. Field numbers follow the FIT profile shipped with fitdecode.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta

FIT_EPOCH = 631065600

# base type -> (id, struct code, size)
TYPES = {
    "enum": (0x00, "B", 1),
    "sint8": (0x01, "b", 1),
    "uint8": (0x02, "B", 1),
    "sint16": (0x83, "h", 2),
    "uint16": (0x84, "H", 2),
    "sint32": (0x85, "i", 4),
    "uint32": (0x86, "I", 4),
    "uint32z": (0x8C, "I", 4),
    "float32": (0x88, "f", 4),
}

_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)  # fmt: skip


def crc(data: bytes, value: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[value & 0xF]
        value = (value >> 4) & 0x0FFF
        value = value ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[value & 0xF]
        value = (value >> 4) & 0x0FFF
        value = value ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return value


def fit_time(dt: datetime) -> int:
    return int(dt.timestamp()) - FIT_EPOCH


class FitWriter:
    def __init__(self) -> None:
        self.body = bytearray()
        self._defs: dict[int, tuple] = {}

    def message(
        self,
        global_num: int,
        fields: list[tuple[int, str, object]],
        dev_fields: list[tuple[int, int, str, object]] = (),
        local: int = 0,
    ) -> None:
        """fields: (field number, base type, value); strings as ("string", size) types are
        not needed by the tests. A value that is a tuple/list is written as a FIT array
        field (several consecutive values under one field number), as real devices do for
        per-zone arrays such as ``time_in_speed_zone``. dev_fields: (field number, dev data
        index, type, value)."""
        signature = (
            global_num,
            tuple((n, t, len(v) if isinstance(v, tuple | list) else 1) for n, t, v in fields),
            tuple((n, i, t) for n, i, t, _ in dev_fields),
        )
        if self._defs.get(local) != signature:
            header = 0x40 | local | (0x20 if dev_fields else 0)
            out = struct.pack("<BBBHB", header, 0, 0, global_num, len(fields))
            for num, type_name, value in fields:
                type_id, _, size = TYPES[type_name]
                count = len(value) if isinstance(value, tuple | list) else 1
                out += struct.pack("<BBB", num, size * count, type_id)
            if dev_fields:
                out += struct.pack("<B", len(dev_fields))
                for num, index, type_name, _ in dev_fields:
                    out += struct.pack("<BBB", num, TYPES[type_name][2], index)
            self.body += out
            self._defs[local] = signature
        data = struct.pack("<B", local)
        for _, type_name, value in fields:
            values = value if isinstance(value, tuple | list) else (value,)
            for v in values:
                data += struct.pack("<" + TYPES[type_name][1], v)
        for _, _, type_name, value in dev_fields:
            data += struct.pack("<" + TYPES[type_name][1], value)
        self.body += data

    def raw(self, data: bytes) -> None:
        self.body += data
        self._defs.clear()

    def to_bytes(self) -> bytes:
        header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(self.body), b".FIT")
        header += struct.pack("<H", crc(header))
        content = header + bytes(self.body)
        return content + struct.pack("<H", crc(content))


def _string_message(local: int, global_num: int, fields: list[tuple[int, object]]) -> bytes:
    """Definition + data for a message with string fields (field_description)."""
    defs = b""
    data = struct.pack("<B", local)
    for num, value in fields:
        if isinstance(value, str):
            raw = value.encode() + b"\0"
            defs += struct.pack("<BBB", num, len(raw), 0x07)
            data += raw
        else:
            type_name, v = value
            type_id, code, size = TYPES[type_name]
            defs += struct.pack("<BBB", num, size, type_id)
            data += struct.pack("<" + code, v)
    header = struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(fields))
    return header + defs + data


def activity_fit(
    start: datetime,
    seconds: int = 60,
    *,
    sport: int = 2,  # 1 running, 2 cycling
    serial: int = 3999999999,
    utc_offset_h: int = 2,
    ftp: int = 250,
    pace_zones: tuple[float, ...] | None = None,  # configured zone-high speeds, m/s
    pace_zone_times: tuple[float, ...] | None = None,  # recorded seconds per zone, session
) -> bytes:
    """A small activity: per-second records with HR, power, speed, position and a
    developer field, one lap, one session, zones and thresholds.

    ``pace_zones``/``pace_zone_times`` add the device's own real pace-zone records: a
    ``speed_zone`` message per configured zone (as hr_zone/power_zone already do), and a
    session-level ``time_in_zone`` message with the recorded per-zone speed-zone arrays
    (``time_in_speed_zone``, ``speed_zone_high_boundary``), matching the documented FIT SDK
    message 216. Never derived from heart rate or power."""
    w = FitWriter()
    w.message(
        0,
        [
            (0, "enum", 4),  # type = activity
            (1, "uint16", 1),  # manufacturer = garmin
            (2, "uint16", 3121),  # product
            (3, "uint32z", serial),
            (4, "uint32", fit_time(start)),
        ],
    )
    # Developer data: one app with a "Form Power" field (as Stryd does).
    w.raw(
        _string_message(
            1, 207, [(1, ("uint8", 0)), (3, ("uint8", 0))]
        )  # developer_data_id: application_id byte (simplified), developer_data_index
    )
    w.raw(
        _string_message(
            2,
            206,
            [
                (0, ("uint8", 0)),  # developer_data_index
                (1, ("uint8", 7)),  # field_definition_number
                (2, ("uint8", 0x84)),  # fit_base_type_id = uint16
                (3, "Form Power"),
                (8, "Watts"),
            ],
        )
    )
    w.message(7, [(1, "uint8", 190), (2, "uint8", 172), (3, "uint16", ftp)], local=3)
    for index, high in enumerate((120, 140, 155, 170, 190)):
        w.message(8, [(254, "uint16", index), (1, "uint8", high)], local=4)
    for index, high in enumerate((140, 190, 225, 262, 300, 375, 1000)):
        w.message(9, [(254, "uint16", index), (1, "uint16", high)], local=5)
    if pace_zones:
        for index, high in enumerate(pace_zones):
            w.message(53, [(254, "uint16", index), (0, "uint16", round(high * 1000))], local=10)
    if pace_zone_times:
        w.message(
            216,
            [
                (0, "uint16", 18),  # reference_mesg = session
                (3, "uint32", tuple(round(s * 1000) for s in pace_zone_times)),
                (7, "uint16", tuple(round(h * 1000) for h in pace_zones)),
            ],
            local=11,
        )
    lat0, lon0 = 52.37, 4.89
    for i in range(seconds):
        t = start + timedelta(seconds=i)
        w.message(
            20,
            [
                (253, "uint32", fit_time(t)),
                (0, "sint32", round((lat0 + i * 1e-5) / (180 / 2**31))),
                (1, "sint32", round(lon0 / (180 / 2**31))),
                (78, "uint32", (10 + 500) * 5),  # enhanced_altitude 10 m
                (3, "uint8", 120 + i % 30),  # heart_rate
                (4, "uint8", 85),  # cadence
                (5, "uint32", i * 800),  # distance, 8 m/s in cm
                (73, "uint32", 8000),  # enhanced_speed 8 m/s in mm/s
                (7, "uint16", 200 + i % 50),  # power
            ],
            dev_fields=[(7, 0, "uint16", 60 + i % 5)],
            local=6,
        )
    end = start + timedelta(seconds=seconds)
    lap_fields = [
        (253, "uint32", fit_time(end)),
        (2, "uint32", fit_time(start)),
        (7, "uint32", seconds * 1000),
        (8, "uint32", seconds * 1000),
        (9, "uint32", seconds * 800),
        (15, "uint8", 135),
        (16, "uint8", 149),
        (19, "uint16", 224),
    ]
    w.message(19, lap_fields, local=7)
    w.message(
        18,
        [
            (253, "uint32", fit_time(end)),
            (2, "uint32", fit_time(start)),
            (7, "uint32", seconds * 1000),
            (8, "uint32", seconds * 1000),
            (9, "uint32", seconds * 800),
            (5, "enum", sport),
            (6, "enum", 0),
            (16, "uint8", 135),
            (17, "uint8", 149),
            (20, "uint16", 224),
            (11, "uint16", 42),
            (22, "uint16", 5),
            (34, "uint16", 230),
        ],
        local=8,
    )
    w.message(
        34,
        [
            (253, "uint32", fit_time(end)),
            (5, "uint32", fit_time(end) + utc_offset_h * 3600),  # local_timestamp
            (1, "uint16", 1),  # num_sessions
        ],
        local=9,
    )
    return w.to_bytes()


def monitoring_fit(day: datetime, *, serial: int = 3999999999, minutes: int = 5) -> bytes:
    """A daily monitoring file: HR each minute (full then 16-bit timestamps), steps, stress."""
    w = FitWriter()
    w.message(
        0,
        [
            (0, "enum", 32),  # type = monitoring_b
            (1, "uint16", 1),
            (2, "uint16", 3121),
            (3, "uint32z", serial),
            (4, "uint32", fit_time(day)),
        ],
    )
    t0 = fit_time(day)
    w.message(55, [(253, "uint32", t0), (27, "uint8", 55)], local=1)
    for i in range(1, minutes):
        w.message(55, [(26, "uint16", (t0 + 60 * i) & 0xFFFF), (27, "uint8", 55 + i)], local=2)
    # steps: cycles field with activity_type walking (6)
    w.message(55, [(253, "uint32", t0 + 600), (5, "enum", 6), (3, "uint32", 1200)], local=3)
    for i, level in enumerate((20, -1, 35)):
        w.message(227, [(0, "sint16", level), (1, "uint32", t0 + 180 * i)], local=4)
    return w.to_bytes()


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)

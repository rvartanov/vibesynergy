"""Bounds and guards added after the security and reliability review."""

from __future__ import annotations

import gzip
import io
import sqlite3
import zipfile
from pathlib import Path

import pytest

from vibesynergy import imports
from vibesynergy.fit.decode import fit_files_in
from vibesynergy.query import planned_load, sql


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def test_walk_zip_reads_a_normal_nested_export() -> None:
    inner = _zip({"a.fit": b"one", "b.fit": b"two"})
    outer = _zip({"activities.zip": inner, "readme.txt": b"hello"})
    assert dict(imports.walk_zip(io.BytesIO(outer))) == {
        "activities.zip/a.fit": b"one",
        "activities.zip/b.fit": b"two",
        "readme.txt": b"hello",
    }


def test_walk_zip_stops_at_zips_nested_too_deep() -> None:
    data = _zip({"x.txt": b"x"})
    for _ in range(imports.MAX_ZIP_DEPTH + 1):
        data = _zip({"deeper.zip": data})
    with pytest.raises(imports.ArchiveTooLarge):
        list(imports.walk_zip(io.BytesIO(data)))


def test_walk_zip_stops_when_it_unpacks_to_too_much(monkeypatch) -> None:
    monkeypatch.setattr(imports, "MAX_UNPACKED_BYTES", 1000)
    bomb = _zip({f"{n}.bin": b"\0" * 400 for n in range(5)})
    with pytest.raises(imports.ArchiveTooLarge):
        list(imports.walk_zip(io.BytesIO(bomb)))
    monkeypatch.setattr(imports, "MAX_UNPACKED_BYTES", 10**9)
    monkeypatch.setattr(imports, "MAX_MEMBER_BYTES", 100)
    with pytest.raises(imports.ArchiveTooLarge):
        list(imports.walk_zip(io.BytesIO(bomb)))


def test_gunzip_is_bounded() -> None:
    packed = gzip.compress(b"\0" * 5000)
    assert imports.gunzip(packed) == b"\0" * 5000
    with pytest.raises(imports.ArchiveTooLarge):
        imports.gunzip(packed, limit=100)


def test_fit_files_in_does_not_follow_endless_nesting() -> None:
    fit = b"\x0e\x10\x00\x00\x00\x00\x00\x00.FIT\x00\x00"
    data = _zip({"a.fit": fit})
    assert [name for name, _ in fit_files_in(data)] == ["a.fit"]
    for _ in range(6):
        data = _zip({"deeper.zip": data})
    assert list(fit_files_in(data)) == []


def test_sql_tool_bounds_rows_and_value_size(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table daily_metric (n integer)")
        conn.executemany("insert into daily_metric values (?)", [(n,) for n in range(10)])
    out = sql.run(db, "select 1 union all select 2 union all select 3", limit=-1)
    assert len(out["rows"]) == 1 and out["truncated"] is True
    with pytest.raises(ValueError):
        sql.run(db, f"select hex(zeroblob({sql.MAX_VALUE_BYTES}))")


def test_a_descending_zone_range_is_the_same_range() -> None:
    table = {1: 0.5, 2: 0.6, 3: 0.7, 4: 0.8, 5: 0.9}
    up = planned_load._zones(table, {"low": 2, "high": 3})
    assert planned_load._zones(table, {"low": 3, "high": 2}) == up == pytest.approx(0.65)

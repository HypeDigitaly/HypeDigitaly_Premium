"""Timestamp correctness + atomic write (Wave 3.1, Bugs 1 & 6).

  * save_last_run_timestamp writes a UTC (+00:00) ISO string atomically (no
    leftover .tmp file).
  * get_last_run_timestamp parses BOTH legacy +02:00 stamps and new UTC stamps.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from gpt_scraper_v3 import xml_sitemap as xs


def _cfg_with_ts_file(make_config, path):
    return make_config(LAST_RUN_FILE=str(path))


def test_save_last_run_timestamp_writes_utc_atomic(make_config, tmp_path):
    ts_file = tmp_path / "last_run.txt"
    _cfg_with_ts_file(make_config, ts_file)

    xs.save_last_run_timestamp("combined")

    assert ts_file.exists()
    # No leftover temp file.
    assert not (tmp_path / "last_run.txt.tmp").exists()

    content = ts_file.read_text(encoding="utf-8").strip()
    parsed = datetime.fromisoformat(content)
    assert parsed.tzinfo is not None
    # Stored as UTC -> +00:00 offset.
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)
    assert content.endswith("+00:00")


def test_get_last_run_parses_legacy_plus_two(make_config, tmp_path):
    ts_file = tmp_path / "last_run.txt"
    _cfg_with_ts_file(make_config, ts_file)
    # Old-format stamp with a +02:00 offset (pre-fix writers used this).
    ts_file.write_text("2026-02-15T12:00:00+02:00", encoding="utf-8")

    parsed = xs.get_last_run_timestamp("combined")
    assert parsed is not None
    assert parsed.tzinfo is not None
    # +02:00 means UTC is 10:00.
    assert parsed.astimezone(timezone.utc).hour == 10


def test_get_last_run_parses_new_utc(make_config, tmp_path):
    ts_file = tmp_path / "last_run.txt"
    _cfg_with_ts_file(make_config, ts_file)
    ts_file.write_text("2026-02-15T12:00:00+00:00", encoding="utf-8")

    parsed = xs.get_last_run_timestamp("combined")
    assert parsed is not None
    assert parsed.astimezone(timezone.utc).hour == 12


def test_save_then_read_roundtrip(make_config, tmp_path):
    ts_file = tmp_path / "last_run.txt"
    _cfg_with_ts_file(make_config, ts_file)

    before = datetime.now(timezone.utc)
    xs.save_last_run_timestamp("combined")
    parsed = xs.get_last_run_timestamp("combined")
    after = datetime.now(timezone.utc)

    assert parsed is not None
    assert before.replace(microsecond=0) <= parsed.astimezone(timezone.utc)
    assert parsed.astimezone(timezone.utc) <= after.replace(microsecond=0) \
        or (parsed.astimezone(timezone.utc) - after).total_seconds() < 1


def test_get_last_run_missing_file_returns_none(make_config, tmp_path):
    ts_file = tmp_path / "does_not_exist.txt"
    _cfg_with_ts_file(make_config, ts_file)
    assert not os.path.exists(ts_file)
    assert xs.get_last_run_timestamp("combined") is None

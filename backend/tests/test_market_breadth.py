from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import market_breadth

CN = ZoneInfo("Asia/Shanghai")


def _ms(day: int, hour: int, minute: int) -> int:
    today = market_breadth.cn_today()
    return int(datetime(today.year, today.month, day, hour, minute, tzinfo=CN).timestamp() * 1000)


def test_market_breadth_appends_and_reads_latest(tmp_path):
    snapshot = {
        "source": "tdxapi",
        "event_ts": _ms(market_breadth.cn_today().day, 10, 0),
        "ingest_ts": _ms(market_breadth.cn_today().day, 10, 0),
        "up_count": 2800,
        "down_count": 1800,
        "flat_count": 100,
        "total_count": 4700,
        "up_down_ratio": 2800 / 1800,
        "market_temperature": "warm",
        "major_index_change_pct": 0.006,
        "major_indices": [{"symbol": "000001.SH", "change_pct": 0.006}],
        "exchanges": {"sh": {"up": 1000, "down": 800, "flat": 50, "total": 1850}},
    }

    market_breadth.append(tmp_path, snapshot)
    got = market_breadth.read_latest(tmp_path)

    assert got is not None
    assert got["source"] == "tdxapi"
    assert got["up_count"] == 2800
    assert got["down_count"] == 1800
    assert got["market_temperature"] == "warm"
    assert got["major_indices"][0]["symbol"] == "000001.SH"


def test_market_breadth_safe_latest_falls_back_to_disk(monkeypatch, tmp_path):
    snapshot = {
        "source": "tdxapi",
        "event_ts": _ms(market_breadth.cn_today().day, 10, 1),
        "ingest_ts": _ms(market_breadth.cn_today().day, 10, 1),
        "up_count": 800,
        "down_count": 2600,
        "flat_count": 100,
        "total_count": 3500,
        "up_down_ratio": 800 / 2600,
        "market_temperature": "cold",
    }
    market_breadth.append(tmp_path, snapshot)

    def fail_latest(*args, **kwargs):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(market_breadth, "latest", fail_latest)

    got = market_breadth.safe_latest(tmp_path)

    assert got["status"] == "stale_fallback"
    assert got["error"] == "sidecar down"
    assert got["market_temperature"] == "cold"

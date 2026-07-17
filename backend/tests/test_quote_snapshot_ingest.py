from __future__ import annotations

import threading

from app.services import quote_snapshot_ingest


def test_ingestor_keeps_only_a_bounded_pending_batch(monkeypatch):
    received: list[list[dict]] = []
    first_started = threading.Event()
    release_first = threading.Event()

    monkeypatch.setattr(
        quote_snapshot_ingest.quote_snapshot_mysql_store,
        "enabled",
        lambda: True,
    )

    def fake_upsert(records):
        received.append(records)
        if len(received) == 1:
            first_started.set()
            release_first.wait(2.0)
        return len(records)

    monkeypatch.setattr(
        quote_snapshot_ingest.quote_snapshot_mysql_store,
        "upsert",
        fake_upsert,
    )

    ingestor = quote_snapshot_ingest.QuoteSnapshotIngestor()
    ingestor.start()
    try:
        first = [{"symbol": "000001.SZ", "event_ts": 1}]
        second = [{"symbol": "000001.SZ", "event_ts": 2}]
        third = [{"symbol": "000001.SZ", "event_ts": 3}]
        assert ingestor.submit(first) is True
        assert first_started.wait(2.0)
        assert ingestor.submit(second) is True
        assert ingestor.submit(third) is True
        release_first.set()
    finally:
        ingestor.stop()

    assert received == [first, third]


def test_ingestor_does_not_start_when_disabled(monkeypatch):
    monkeypatch.setattr(
        quote_snapshot_ingest.quote_snapshot_mysql_store,
        "enabled",
        lambda: False,
    )
    ingestor = quote_snapshot_ingest.QuoteSnapshotIngestor()
    ingestor.start()
    assert ingestor.submit([{"symbol": "000001.SZ", "event_ts": 1}]) is False
    ingestor.stop()

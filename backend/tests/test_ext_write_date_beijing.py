"""扩展数据文件上传 / JSON 写入的落盘分区日期 — 缺省按北京日期, 非法日期返回 400。

定时拉取的落盘日期已经改用 `cn_today()` (test_ext_pull_time_window), 但同一张时序表
的另外两条写入路径仍是裸 `date.today()`:

- `POST /{id}/upload`: 前端「文件」页签从不传 `snapshot_date`, 每次都走缺省值;
- `POST /{id}/ingest`: 「接口」页签给出的 curl 示例不带 `date`, 同样走缺省值;
- `write_ext_parquet(snapshot_date=None)` 的服务层缺省值。

UTC 容器里北京 00:00-08:00 之间写入的数据会落进前一天的分区。显式传入的日期
则直接 `date.fromisoformat`, 非法值抛 ValueError → 500。

判据不依赖跑测试的机器时区: 把北京日期钉在 2026-03-02, 断言分区按它落盘。
"""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException, UploadFile

from app.api import ext_data as ext_api
from app.services import ext_data as ext_svc
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField

BEIJING_DAY = date(2026, 3, 2)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ExtConfigStore(tmp_path).upsert(ExtConfig(
        id="hot",
        label="人气",
        mode="timeseries",
        fields=[ExtField("symbol", "string"), ExtField("heat", "float")],
    ))
    # 北京日期钉死; raising=False: 未修复代码里模块没有 cn_today, 钉了也不会被用到
    monkeypatch.setattr(ext_api, "cn_today", lambda: BEIJING_DAY, raising=False)
    monkeypatch.setattr(ext_svc, "cn_today", lambda: BEIJING_DAY, raising=False)
    # DuckDB 视图刷新与本测试无关
    monkeypatch.setattr(ext_api, "_refresh_views", lambda request: None)
    return tmp_path


def _request(data_dir: Path) -> SimpleNamespace:
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=data_dir)))
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _partitions(data_dir: Path) -> list[str]:
    base = data_dir / "ext_data" / "hot" / "timeseries"
    return sorted(p.name for p in base.iterdir()) if base.exists() else []


def _csv_upload() -> UploadFile:
    return UploadFile(file=io.BytesIO(b"symbol,heat\n000001.SZ,1.5\n"), filename="hot.csv")


async def test_upload_without_snapshot_date_lands_in_the_beijing_day(data_dir: Path) -> None:
    resp = await ext_api.upload_data(_request(data_dir), "hot", file=_csv_upload(), snapshot_date=None)

    assert resp["date"] == "2026-03-02"
    assert _partitions(data_dir) == ["date=2026-03-02"]
    part = data_dir / "ext_data" / "hot" / "timeseries" / "date=2026-03-02" / "part.parquet"
    assert pl.read_parquet(part)["heat"].to_list() == [1.5]


def test_ingest_without_date_lands_in_the_beijing_day(data_dir: Path) -> None:
    body = ext_api.IngestReq(rows=[{"symbol": "000001.SZ", "heat": 2.5}])

    resp = ext_api.ingest_data(_request(data_dir), "hot", body)

    assert resp["date"] == "2026-03-02"
    assert _partitions(data_dir) == ["date=2026-03-02"]


def test_service_default_snapshot_date_is_the_beijing_day(data_dir: Path) -> None:
    config = ExtConfigStore(data_dir).get("hot")
    df = pl.DataFrame({"symbol": ["000001.SZ"], "heat": [3.5]})

    ext_svc.write_ext_parquet(df, config, data_dir, snapshot_date=None)

    assert _partitions(data_dir) == ["date=2026-03-02"]


async def test_explicit_dates_still_win(data_dir: Path) -> None:
    resp = await ext_api.upload_data(
        _request(data_dir), "hot", file=_csv_upload(), snapshot_date="2026-01-05",
    )
    assert resp["date"] == "2026-01-05"

    body = ext_api.IngestReq(date="2026-01-06", rows=[{"symbol": "000001.SZ", "heat": 2.5}])
    assert ext_api.ingest_data(_request(data_dir), "hot", body)["date"] == "2026-01-06"

    assert _partitions(data_dir) == ["date=2026-01-05", "date=2026-01-06"]


@pytest.mark.parametrize("bad", ["not-a-date", "2026-13-01", "2026/03/02"])
async def test_upload_rejects_an_invalid_snapshot_date_with_400(data_dir: Path, bad: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await ext_api.upload_data(_request(data_dir), "hot", file=_csv_upload(), snapshot_date=bad)

    assert excinfo.value.status_code == 400
    assert _partitions(data_dir) == []


@pytest.mark.parametrize("bad", ["not-a-date", "2026-13-01", "2026/03/02"])
def test_ingest_rejects_an_invalid_date_with_400(data_dir: Path, bad: str) -> None:
    body = ext_api.IngestReq(date=bad, rows=[{"symbol": "000001.SZ", "heat": 2.5}])

    with pytest.raises(HTTPException) as excinfo:
        ext_api.ingest_data(_request(data_dir), "hot", body)

    assert excinfo.value.status_code == 400
    assert _partitions(data_dir) == []

"""分钟涨跌幅基准价为 0: 板块切换 / 板块分时两个接口不得返回 500。

两处口径相同: pct = 分钟 close / ref - 1, ref 优先前一交易日日K收盘, 缺失退化为
当日首根分钟 close。基准价为 0 (或分钟 close 为 0 的无效行) 时 pct 为 inf,
Starlette JSONResponse 以 allow_nan=False 渲染, 整个响应 500。
价格 <= 0 视为无效: 前收无效时退化为首根有效分钟 close, 无效分钟行不参与计算。
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ext_data as ext_api
from app.api import sector_rotation as sector_rotation_api
from app.services import rps_rotation, sector_rotation
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet

DAY = "2026-09-11"
PREV = "2026-09-10"
SYMS_A = ["000001.SZ", "000002.SZ"]
SYMS_B = ["000003.SZ", "000004.SZ"]
STAMPS = [(9, 35), (9, 40), (10, 0), (10, 40)]


def _reset_caches() -> None:
    rps_rotation._map_cache.clear()
    rps_rotation._map_ts.clear()
    sector_rotation.invalidate_cache()
    ext_api._DIMENSION_INTRADAY_CACHE.clear()


def _build(tmp_path: Path, *, prev_closes: list[float] | None, first_close_000002: float) -> Path:
    """A 题材 = 000001 (恒 101) + 000002 (首根 first_close_000002, 之后 100, 末根 102);
    B 题材 = 000003/000004 (恒 101)。prev_closes=None 表示没有前一交易日日K。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rows = []
    for i, (h, m) in enumerate(STAMPS):
        ts = datetime.fromisoformat(f"{DAY}T{h:02d}:{m:02d}:00")
        for sym in SYMS_A + SYMS_B:
            close = 101.0
            if sym == "000002.SZ":
                close = first_close_000002 if i == 0 else (102.0 if i == len(STAMPS) - 1 else 100.0)
            rows.append({"symbol": sym, "datetime": ts, "close": close, "amount": 1e6})
    (data_dir / "kline_minute" / f"date={DAY}").mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(data_dir / "kline_minute" / f"date={DAY}" / "part.parquet")
    if prev_closes is not None:
        (data_dir / "kline_daily" / f"date={PREV}").mkdir(parents=True)
        pl.DataFrame({"symbol": SYMS_A + SYMS_B, "close": prev_closes}).write_parquet(
            data_dir / "kline_daily" / f"date={PREV}" / "part.parquet"
        )
    config = ExtConfig(
        id="ext_gn",
        label="测试概念",
        mode="snapshot",
        fields=[ExtField("symbol", "string", "代码"), ExtField("所属概念", "string", "所属概念")],
    )
    ExtConfigStore(data_dir).upsert(config)
    write_ext_parquet(
        pl.DataFrame({"symbol": SYMS_A + SYMS_B, "所属概念": ["A题材", "A题材", "B题材", "B题材"]}),
        config,
        data_dir,
    )
    return data_dir


def _client(data_dir: Path) -> tuple[TestClient, SimpleNamespace]:
    app = FastAPI()
    app.include_router(sector_rotation_api.router)
    app.include_router(ext_api.router)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir, db=None))
    app.state.repo = repo
    app.state.data_dir = data_dir
    return TestClient(app, raise_server_exceptions=False), repo


@pytest.fixture(autouse=True)
def _isolate_caches():
    _reset_caches()
    yield
    _reset_caches()


def _assert_all_finite(values) -> None:
    for v in values:
        assert v is None or math.isfinite(v), values


# ── 板块切换 (services/sector_rotation) ───────────────────────────────────────


def test_sector_rotation_zero_prev_close_falls_back_to_first_minute_close(tmp_path):
    data_dir = _build(tmp_path, prev_closes=[100.0, 0.0, 100.0, 100.0], first_close_000002=100.0)
    repo = _client(data_dir)[1]

    result = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5)

    json.dumps(result, allow_nan=False)  # 改动前: ValueError: Out of range float values
    assert result["status"] == "ok"
    assert result["basis"] == "mixed"  # 000002 前收无效 → 退化为首根分钟 close
    sectors = {item["name"]: item for item in result["sectors"]}
    # 末桶: 000001 = 101/100-1 = 1%, 000002 = 102/100-1 = 2% → A 题材等权 1.5%
    assert sectors["A题材"]["pct_now"] == pytest.approx(0.015, abs=1e-4)
    assert sectors["B题材"]["pct_now"] == pytest.approx(0.01, abs=1e-4)
    for row in result["series"]["matrix"]:
        _assert_all_finite(row)


def test_sector_rotation_api_zero_prev_close_is_not_500(tmp_path):
    data_dir = _build(tmp_path, prev_closes=[100.0, 0.0, 100.0, 100.0], first_close_000002=100.0)
    client, _repo = _client(data_dir)

    resp = client.get("/api/sector-rotation", params={"bucket": 5})

    assert resp.status_code == 200, resp.text[:200]
    assert resp.json()["status"] == "ok"


def test_sector_rotation_zero_first_minute_close_without_prev_daily(tmp_path):
    """无前一交易日日K (全部退化为首根分钟 close), 且 000002 首根分钟 close=0。"""
    data_dir = _build(tmp_path, prev_closes=None, first_close_000002=0.0)
    repo = _client(data_dir)[1]

    result = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5)

    json.dumps(result, allow_nan=False)
    assert result["status"] == "ok"
    assert result["basis"] == "first_close"
    sectors = {item["name"]: item for item in result["sectors"]}
    # 000001 基准 101 → 0%; 000002 基准为首根有效 close 100 → 末根 2% → A 题材 1%
    assert sectors["A题材"]["pct_now"] == pytest.approx(0.01, abs=1e-4)
    for row in result["series"]["matrix"]:
        _assert_all_finite(row)
        # close=0 的无效分钟行不参与计算, 不会出现 -100%
        assert all(v is None or v > -0.5 for v in row), row


# ── 板块分时 (api/ext_data dimension-intraday) ────────────────────────────────


def _dimension_intraday(client: TestClient):
    return client.get(
        f"{ext_api.router.prefix}/ext_gn/dimension-intraday",
        params={"field": "所属概念", "value": "A题材"},
    )


def test_dimension_intraday_zero_prev_close_is_not_500(tmp_path):
    data_dir = _build(tmp_path, prev_closes=[100.0, 0.0, 100.0, 100.0], first_close_000002=100.0)
    client, _repo = _client(data_dir)

    resp = _dimension_intraday(client)

    assert resp.status_code == 200, resp.text[:200]
    body = resp.json()
    assert body["status"] == "ok"
    assert body["basis"] == "mixed"
    last = body["points"][-1]
    assert last["time"] == "10:40"
    assert last["sector"] == pytest.approx(0.015, abs=1e-4)
    assert last["market"] == pytest.approx(0.0125, abs=1e-4)
    _assert_all_finite([p["sector"] for p in body["points"]] + [p["market"] for p in body["points"]])


def test_dimension_intraday_zero_first_minute_close_without_prev_daily(tmp_path):
    data_dir = _build(tmp_path, prev_closes=None, first_close_000002=0.0)
    client, _repo = _client(data_dir)

    resp = _dimension_intraday(client)

    assert resp.status_code == 200, resp.text[:200]
    body = resp.json()
    assert body["basis"] == "first_close"
    points = {p["time"]: p for p in body["points"]}
    # 09:35 只有 000001 有有效分钟 (000002 的 close=0 行无效) → 板块 0%, 不是 -50%
    assert points["09:35"]["sector"] == pytest.approx(0.0, abs=1e-4)
    assert points["10:40"]["sector"] == pytest.approx(0.01, abs=1e-4)
    _assert_all_finite([p["sector"] for p in body["points"]] + [p["market"] for p in body["points"]])

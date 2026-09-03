"""#187 回归: screener JIT 即时计算路径不得丢失 turnover_rate 存储列。

历史日期走 _compute_enriched_full (scan_parquet + compute_indicators),
warmup 读取白名单漏掉 turnover_rate 时, 即时计算后该列丢失 —— 自定义 SQL
用 turnover_rate 做条件的请求在 DuckDB 注册视图里找不到列, Binder Error
被 except 吞掉返回空结果 (无任何报错提示)。
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import polars as pl

from app.services.screener import ScreenerService


def _write_enriched(tmp_path, days: int, turnover_by_day: dict[str, float]) -> None:
    base = tmp_path / "kline_daily_enriched"
    base.mkdir(parents=True, exist_ok=True)
    start = date(2026, 9, 1) - timedelta(days=days)
    for i in range(days + 1):
        d = start + timedelta(days=i)
        part = base / f"date={d.isoformat()}"
        part.mkdir(exist_ok=True)
        pl.DataFrame(
            {
                "symbol": ["600000.SH", "000001.SZ"],
                "date": [d, d],
                "open": [10.0, 20.0],
                "high": [11.0, 21.0],
                "low": [9.0, 19.0],
                "close": [10.5, 20.5],
                "volume": [100.0, 200.0],
                "amount": [1050.0, 4100.0],
                "raw_close": [10.5, 20.5],
                "raw_high": [11.0, 21.0],
                "raw_low": [9.0, 19.0],
                "turnover_rate": [turnover_by_day.get(d.isoformat(), 1.0), 2.0],
                "consecutive_limit_ups": [0, 0],
                "consecutive_limit_downs": [0, 0],
            }
        ).write_parquet(part / "part.parquet")


def _service(tmp_path, instruments: pl.DataFrame | None = None) -> ScreenerService:
    repo = MagicMock()
    repo.store.data_dir = tmp_path
    # 最新日缓存与 repo 级历史缓存均未命中 → 走 scan_parquet + 即时计算慢路径
    repo.get_enriched_latest_asset.return_value = (pl.DataFrame(), None)
    repo.get_enriched_history.return_value = None
    # instruments 不可用 (空维表) 时 turnover_rate 无从重算, 只能靠存储列透传
    repo.get_instruments_asset.return_value = instruments or pl.DataFrame()
    repo.get_historical_shares.return_value = pl.DataFrame()
    return ScreenerService(repo, asset_type="stock")


def test_historical_jit_frame_keeps_turnover_rate(tmp_path) -> None:
    target = date(2026, 9, 1)
    _write_enriched(tmp_path, 30, {target.isoformat(): 5.5})
    svc = _service(tmp_path)  # 无 instruments → 无重算路径, 纯存储列透传

    df = svc._load_enriched_for_date(target)
    assert not df.is_empty()
    assert "turnover_rate" in df.columns, "JIT 即时计算后 turnover_rate 列不应丢失"
    # 目标日的值来自存储列透传, 不是置 null
    row = df.filter(pl.col("symbol") == "600000.SH").row(0, named=True)
    assert row["turnover_rate"] == 5.5


def test_custom_sql_can_filter_on_turnover_rate(tmp_path) -> None:
    target = date(2026, 9, 1)
    _write_enriched(tmp_path, 30, {target.isoformat(): 5.5})
    svc = _service(tmp_path)

    result = svc.run(target, ["turnover_rate > 3"], limit=10)
    # 600000 (5.5) 命中; 000001 恒为 2.0 被过滤 — 条件真正生效而非空结果
    assert [r["symbol"] for r in result.rows] == ["600000.SH"]

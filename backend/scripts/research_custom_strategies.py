"""在挂载的 data 目录上快速回测自定义策略。

用法 (容器内):
  uv run python /app/scripts/research_custom_strategies.py
  uv run python /app/scripts/research_custom_strategies.py --start 2025-10-01 --end 2026-08-10
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import types
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

try:
    import duckdb  # noqa: F401
except ModuleNotFoundError:
    sys.modules["duckdb"] = types.ModuleType("duckdb")

from app.backtest.engine import BacktestEngine
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.engine import StrategyEngine

logger = logging.getLogger("research")

DEFAULT_STRATEGIES = [
    # 当前冠军与变体
    "custom_dual_edge",
    "custom_dual_edge_focus",
    "custom_dual_edge_freq",
    "custom_dual_compress",
    "custom_compress_burst",
    "custom_dual_fill",
    "custom_triple_short",
    "custom_vol_thrust",
    "custom_gap_pull_hold",
    "custom_yi_jin_er_strict",
    "custom_seal_follow",
    "custom_rsi_bounce",
    "custom_amount_thrust",
    "custom_ma20_reclaim",
    "custom_dual_edge_v2",
    # 竞价/分歧基线
    "custom_auction_gap_v2",
    "custom_auction_gap_v2_freq",
    "custom_auction_gap_v3",
    "custom_fenqi_yizhi",
    "custom_lowvol_leader",
    # 论坛战法
    "custom_yi_jin_er",
    "custom_ruo_zhuan_qiang",
    "custom_longtou_shouyin",
    "custom_huangjin_keng",
    "custom_platform_breakout",
    "custom_platform_breakout_v2",
    "custom_suoliang_huicai",
    "custom_ma5_trend",
    # 既有自定义
    "custom_auction_gap_hold",
    "custom_first_board_follow",
    "custom_broken_board_weak_open",
    "custom_rs_pullback",
    "custom_core_sat",
    "custom_triple_edge",
    # 内置对照
    "strong_open",
    "broken_board_recovery",
    "limit_up_momentum",
    "adaptive_relative_momentum",
]


@dataclass
class _StoreRef:
    data_dir: Path


class _ResearchRepository:
    def __init__(self, data_dir: Path) -> None:
        self.store = _StoreRef(data_dir)

    def get_enriched_range(self, *args, **kwargs):
        return None

    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        dirname = "instruments_etf" if asset_type == "etf" else "instruments"
        files = list((self.store.data_dir / dirname).rglob("*.parquet"))
        if not files:
            return pl.DataFrame()
        return pl.scan_parquet(
            str(self.store.data_dir / dirname / "**" / "*.parquet"),
            extra_columns="ignore",
        ).collect()

    def get_index_daily(self, symbol: str, start: date, end: date, columns=None) -> pl.DataFrame:
        base = self.store.data_dir / "kline_index_daily"
        if not any(base.rglob("*.parquet")):
            return pl.DataFrame()
        lf = pl.scan_parquet(
            str(base / "**" / "*.parquet"),
            extra_columns="ignore",
        ).filter(
            (pl.col("symbol") == symbol)
            & (pl.col("date") >= start)
            & (pl.col("date") <= end)
        )
        if columns:
            names = set(lf.collect_schema().names())
            lf = lf.select([c for c in columns if c in names])
        return lf.sort("date").collect()


def _scan_enriched(data_dir: Path) -> pl.LazyFrame:
    # 分区 parquet 可能出现 schema 漂移 (如后加 quote_ts), 忽略多余列。
    return pl.scan_parquet(
        str(data_dir / "kline_daily_enriched" / "**" / "*.parquet"),
        extra_columns="ignore",
    )


def _trading_range(data_dir: Path) -> tuple[date, date]:
    lf = _scan_enriched(data_dir)
    row = (
        lf.select(pl.col("date").min().alias("start"), pl.col("date").max().alias("end"))
        .collect()
        .row(0, named=True)
    )
    return row["start"], row["end"]


def _summarize(result) -> dict:
    stats = result.stats or {}
    return {
        "error": result.error,
        "elapsed_ms": round(float(result.elapsed_ms or 0), 1),
        "total_return": stats.get("total_return"),
        "max_drawdown": stats.get("max_drawdown"),
        "sharpe": stats.get("sharpe"),
        "win_rate": stats.get("win_rate"),
        "trade_count": stats.get("trade_count") or stats.get("trades") or stats.get("n_trades"),
        "final_equity": stats.get("final_equity") or stats.get("equity"),
        "avg_hold_days": stats.get("avg_hold_days"),
        "profit_factor": stats.get("profit_factor"),
        "raw_keys": sorted(stats.keys()) if stats else [],
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/app/data")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES))
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not (data_dir / "kline_daily_enriched").exists():
        logger.error("missing kline_daily_enriched under %s", data_dir)
        return 1

    data_start, data_end = _trading_range(data_dir)
    start = date.fromisoformat(args.start) if args.start else max(data_start, date(2025, 10, 1))
    end = date.fromisoformat(args.end) if args.end else data_end
    logger.info("data range %s ~ %s | backtest %s ~ %s", data_start, data_end, start, end)

    strategy_dirs = [
        Path("/app/app/strategy/builtin"),
        data_dir / "strategies" / "custom",
        data_dir / "strategies" / "ai",
    ]
    # 兼容源码布局
    local_builtin = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
    if local_builtin.exists() and local_builtin not in strategy_dirs:
        strategy_dirs.insert(0, local_builtin)

    engine = StrategyEngine(strategy_dirs=strategy_dirs)
    loaded = {s["id"]: s.get("name") for s in engine.list_strategies()}
    logger.info("loaded %d strategies", len(loaded))

    repo = _ResearchRepository(data_dir)
    bt_engine = BacktestEngine(repo)
    service = StrategyBacktestService(bt_engine, engine)

    rows = []
    for sid in [s.strip() for s in args.strategies.split(",") if s.strip()]:
        if sid not in loaded:
            logger.warning("skip missing strategy: %s", sid)
            rows.append({"strategy_id": sid, "error": "not_loaded"})
            continue
        meta = engine.get(sid).meta if engine.get(sid) else {}
        defaults = (meta or {}).get("backtest_defaults") or {}
        entry_fill = defaults.get("entry_fill", "open_t+1")
        exit_fill = defaults.get("exit_fill", "open_t+1")
        max_pos = int(defaults.get("max_positions", args.max_positions))
        sizing = defaults.get("position_sizing", "equal")
        logger.info("run %s (%s) entry=%s exit=%s max_pos=%s", sid, loaded.get(sid), entry_fill, exit_fill, max_pos)
        try:
            result = service.run(
                StrategyBacktestConfig(
                    strategy_id=sid,
                    symbols=None,
                    start=start,
                    end=end,
                    params=None,
                    overrides=None,
                    entry_fill=entry_fill,
                    exit_fill=exit_fill,
                    commission_pct=0.0002,
                    stamp_tax_pct=0.0005,
                    slippage_bps=8.0,
                    max_positions=max_pos,
                    max_exposure_pct=1.0,
                    initial_capital=args.capital,
                    position_sizing=sizing,
                    mode="position",
                    asset_type="stock",
                )
            )
            summary = _summarize(result)
        except Exception as exc:
            logger.exception("backtest failed: %s", sid)
            summary = {"error": str(exc)}
        row = {"strategy_id": sid, "name": loaded.get(sid), **summary}
        rows.append(row)
        logger.info("result %s: %s", sid, json.dumps(row, ensure_ascii=False, default=str))

    print(json.dumps({"start": str(start), "end": str(end), "results": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

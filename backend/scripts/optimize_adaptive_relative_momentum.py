"""为截面动量加速策略做确定性的训练/验证/留出集搜索。

示例：
PYTHONPATH=backend conda run -n base python backend/scripts/optimize_adaptive_relative_momentum.py
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import types
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

# conda base 可能没有项目可选的 duckdb；研究脚本只用 Polars，不实例化 DataStore。
try:
    import duckdb  # noqa: F401
except ModuleNotFoundError:
    sys.modules["duckdb"] = types.ModuleType("duckdb")

from app.backtest.engine import BacktestEngine
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.engine import StrategyEngine

logger = logging.getLogger(__name__)
STRATEGY_ID = "adaptive_relative_momentum"


@dataclass
class _StoreRef:
    data_dir: Path


class _ResearchRepository:
    """BacktestEngine 所需的最小只读仓储，避免研究脚本依赖 DuckDB。"""

    def __init__(self, data_dir: Path) -> None:
        self.store = _StoreRef(data_dir)

    def get_enriched_range(self, *args, **kwargs) -> None:
        return None

    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        dirname = "instruments_etf" if asset_type == "etf" else "instruments"
        files = list((self.store.data_dir / dirname).rglob("*.parquet"))
        if not files:
            return pl.DataFrame()
        return pl.scan_parquet(str(self.store.data_dir / dirname / "**" / "*.parquet")).collect()

    def get_index_daily(
        self,
        symbol: str,
        start: date,
        end: date,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        base = self.store.data_dir / "kline_index_daily"
        if not any(base.rglob("*.parquet")):
            return pl.DataFrame()
        lf = pl.scan_parquet(str(base / "**" / "*.parquet")).filter(
            (pl.col("symbol") == symbol)
            & (pl.col("date") >= start)
            & (pl.col("date") <= end)
        )
        if columns:
            lf = lf.select([col for col in columns if col in lf.collect_schema().names()])
        return lf.sort("date").collect()


def _asset_profile(data_dir: Path, asset_type: str) -> dict:
    dirname = "kline_etf_enriched" if asset_type == "etf" else "kline_daily_enriched"
    base = data_dir / dirname
    files = list(base.rglob("*.parquet"))
    if not files:
        return {"asset_type": asset_type, "status": "missing", "rows": 0, "symbols": 0, "dates": 0}

    lf = pl.scan_parquet(str(base / "**" / "*.parquet"))
    schema = set(lf.collect_schema().names())
    if {"open", "high", "low", "close"}.issubset(schema):
        lf = lf.filter(pl.all_horizontal(
            *[pl.col(col).is_not_null() & pl.col(col).is_finite() & (pl.col(col) > 0)
              for col in ("open", "high", "low", "close")]
        ))
    if {"symbol", "date"}.issubset(schema):
        lf = lf.unique(subset=["symbol", "date"], keep="last")
    stats = (
        lf.select([
            pl.len().alias("rows"),
            pl.col("symbol").n_unique().alias("symbols"),
            pl.col("date").n_unique().alias("dates"),
            pl.col("date").min().alias("start"),
            pl.col("date").max().alias("end"),
        ])
        .collect()
        .row(0, named=True)
    )
    serializable = {
        key: str(value) if isinstance(value, date) else value
        for key, value in stats.items()
    }
    return {"asset_type": asset_type, "status": "ok", **serializable}


def _trading_dates(data_dir: Path) -> list[date]:
    lf = pl.scan_parquet(str(data_dir / "kline_daily_enriched" / "**" / "*.parquet"))
    return (
        lf.filter(
            pl.col("close").is_not_null()
            & pl.col("close").is_finite()
            & (pl.col("close") > 0)
        )
        .select("date")
        .unique()
        .sort("date")
        .collect()["date"]
        .to_list()
    )


def _split_dates(dates: list[date], warmup_days: int = 60) -> dict[str, tuple[date, date]]:
    if len(dates) < warmup_days + 90:
        raise ValueError(f"股票有效交易日仅 {len(dates)} 天，不足以切分训练/验证/留出集")
    formal = dates[warmup_days:]
    train_end = max(1, int(len(formal) * 0.45))
    valid_end = max(train_end + 1, int(len(formal) * 0.70))
    return {
        "train": (formal[0], formal[train_end - 1]),
        "validation": (formal[train_end], formal[valid_end - 1]),
        "test": (formal[valid_end], formal[-1]),
        "full": (formal[0], formal[-1]),
    }


def _candidate(rng: random.Random) -> dict:
    return {
        "params": {
            "top_pct": rng.choice([0.01, 0.03, 0.05, 0.10, 0.15]),
            "breadth_min": rng.choice([0.30, 0.40, 0.50, 0.60]),
            "min_momentum_20d": rng.choice([0.00, 0.03, 0.06, 0.10, 0.15]),
            "min_momentum_60d": rng.choice([0.03, 0.08, 0.12, 0.18, 0.25]),
            "max_momentum_5d": rng.choice([0.10, 0.18, 0.25, 0.35]),
            "max_annual_vol": rng.choice([0.45, 0.60, 0.80, 1.20]),
            "breakout_buffer": rng.choice([0.03, 0.05, 0.08, 0.12, 0.18]),
            "min_vol_ratio": rng.choice([0.5, 0.8, 1.0, 1.2]),
        },
        "max_positions": rng.choice([1, 3, 5, 8]),
        "position_sizing": rng.choice(["equal", "score_weight"]),
        "overrides": {
            "max_hold_days": rng.choice([10, 20, 40, 60]),
            "stop_loss": rng.choice([0.05, 0.08, 0.11]),
            "trailing_stop": rng.choice([0.06, 0.10, 0.14, None]),
            "trailing_take_profit_activate": rng.choice([0.15, 0.20, None]),
            "trailing_take_profit_drawdown": rng.choice([0.06, 0.10, None]),
        },
    }


def _candidates(trials: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    default = {
        "params": {},
        "max_positions": 5,
        "position_sizing": "equal",
        "overrides": {},
    }
    result = [default]
    seen = {json.dumps(default, sort_keys=True)}
    while len(result) < max(trials, 1):
        item = _candidate(rng)
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _run(
    service: StrategyBacktestService,
    strategy_id: str,
    period: tuple[date, date],
    candidate: dict,
    asset_type: str = "stock",
) -> dict:
    result = service.run(StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=None,
        start=period[0],
        end=period[1],
        params=candidate.get("params"),
        overrides=candidate.get("overrides"),
        entry_fill="open_t+1",
        exit_fill="open_t+1",
        commission_pct=0.0002,
        stamp_tax_pct=0.0005 if asset_type == "stock" else 0.0,
        slippage_bps=5.0,
        max_positions=int(candidate.get("max_positions", 5)),
        max_exposure_pct=1.0,
        initial_capital=1_000_000.0,
        position_sizing=candidate.get("position_sizing", "equal"),
        mode="position",
        asset_type=asset_type,
    ))
    return {
        "strategy_id": strategy_id,
        "error": result.error,
        "stats": result.stats,
        "elapsed_ms": result.elapsed_ms,
    }


def _score(row: dict) -> tuple[float, float, int]:
    stats = row.get("stats") or {}
    return (
        float(stats.get("total_return") or -999.0),
        float(stats.get("max_drawdown") or -999.0),
        int(stats.get("n_trades") or 0),
    )


def optimize(data_dir: Path, trials: int, seed: int, min_trades: int, output: Path) -> dict:
    repo = _ResearchRepository(data_dir)
    engine = BacktestEngine(repo)  # type: ignore[arg-type]
    strategy_dir = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
    strategy_engine = StrategyEngine(lambda _: pl.DataFrame(), strategy_dirs=[strategy_dir])
    service = StrategyBacktestService(engine, strategy_engine)

    profiles = {asset: _asset_profile(data_dir, asset) for asset in ("stock", "etf")}
    splits = _split_dates(_trading_dates(data_dir))
    candidates = _candidates(trials, seed)

    train_rows = []
    for index, candidate in enumerate(candidates, start=1):
        row = _run(service, STRATEGY_ID, splits["train"], candidate)
        row["candidate"] = candidate
        train_rows.append(row)
        total_return = row["stats"].get("total_return") if row["stats"] else None
        print(f"train {index:>3}/{len(candidates)} return={total_return}")

    eligible = [
        row for row in train_rows
        if not row["error"] and int(row["stats"].get("n_trades", 0)) >= min_trades
    ]
    if not eligible:
        raise RuntimeError(f"没有达到最少 {min_trades} 笔交易的有效训练配置")
    train_top = sorted(eligible, key=_score, reverse=True)[: min(12, len(eligible))]

    validation_rows = []
    for row in train_top:
        checked = _run(service, STRATEGY_ID, splits["validation"], row["candidate"])
        checked["candidate"] = row["candidate"]
        checked["train_stats"] = row["stats"]
        validation_rows.append(checked)
    validation_ok = [
        row for row in validation_rows
        if not row["error"] and int(row["stats"].get("n_trades", 0)) >= min_trades
    ]
    if not validation_ok:
        raise RuntimeError("训练集前列配置在验证集均无足够交易")
    winner = sorted(validation_ok, key=_score, reverse=True)[0]

    test = _run(service, STRATEGY_ID, splits["test"], winner["candidate"])
    full = _run(service, STRATEGY_ID, splits["full"], winner["candidate"])

    preset_candidate = {
        "params": {}, "overrides": {}, "max_positions": 5, "position_sizing": "equal",
    }
    preset_default = {
        split_name: _run(service, STRATEGY_ID, splits[split_name], preset_candidate)
        for split_name in ("train", "validation", "test", "full")
    }

    baselines = []
    for meta in strategy_engine.list_strategies():
        strategy_id = str(meta["id"])
        baseline = _run(service, strategy_id, splits["test"], {
            "params": {}, "overrides": {}, "max_positions": winner["candidate"]["max_positions"],
            "position_sizing": winner["candidate"]["position_sizing"],
        })
        baselines.append(baseline)
    baselines.sort(key=_score, reverse=True)

    etf_profile = profiles["etf"]
    etf_test = {
        "status": "insufficient_data" if int(etf_profile.get("dates", 0)) < 120 else "ready",
        "required_dates": 120,
        "available_dates": int(etf_profile.get("dates", 0)),
        "result": None,
    }
    if etf_test["status"] == "ready":
        etf_period = (
            date.fromisoformat(str(etf_profile["start"])),
            date.fromisoformat(str(etf_profile["end"])),
        )
        etf_test["result"] = _run(service, STRATEGY_ID, etf_period, winner["candidate"], "etf")

    report = {
        "strategy_id": STRATEGY_ID,
        "objective": "validation_total_return",
        "seed": seed,
        "trials": len(candidates),
        "min_trades": min_trades,
        "data_profiles": profiles,
        "data_warnings": [
            "当前股票仅约一年，统计证据较弱",
            "本地股票与ETF均无复权因子，跨公司行动收益可能失真",
            "ETF历史不足时拒绝输出策略收益",
        ],
        "splits": {key: [str(value[0]), str(value[1])] for key, value in splits.items()},
        "winner": winner,
        "holdout_test": test,
        "full_period_reference": full,
        "preset_default": preset_default,
        "holdout_baselines": baselines,
        "etf_test": etf_test,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "data",
    )
    parser.add_argument("--trials", type=int, default=64)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-trades", type=int, default=8)
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            Path(__file__).resolve().parents[2]
            / "data"
            / "backtest_results"
            / "adaptive_relative_momentum.json"
        ),
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    report = optimize(
        args.data_dir.resolve(),
        args.trials,
        args.seed,
        args.min_trades,
        args.output.resolve(),
    )
    print(json.dumps({
        "output": str(args.output.resolve()),
        "winner": report["winner"],
        "holdout_test": report["holdout_test"],
        "etf_test": report["etf_test"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

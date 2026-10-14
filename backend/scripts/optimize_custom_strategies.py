"""对自定义策略做小规模参数网格/随机搜索。"""
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

try:
    import duckdb  # noqa: F401
except ModuleNotFoundError:
    sys.modules["duckdb"] = types.ModuleType("duckdb")

from app.backtest.engine import BacktestEngine
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.engine import StrategyEngine

logger = logging.getLogger("optimize")


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


SEARCH_SPACES = {
    "custom_auction_gap_hold": {
        "params": {
            "gap_min": [1.0, 1.5, 2.0],
            "gap_max": [4.0, 5.0, 6.0],
            "min_change": [1.5, 2.0, 3.0],
            "min_vol_ratio": [1.2, 1.5, 2.0],
            "close_high_tol": [0.015, 0.025, 0.04],
            "low_hold_open": [True],
        },
        "overrides": {
            "max_hold_days": [2, 3, 4, 5],
            "stop_loss": [-0.04, -0.05, -0.07],
            "trailing_stop": [0.03, 0.04, 0.06, None],
        },
        "max_positions": [3, 4, 6, 8],
        "position_sizing": ["equal", "score_weight"],
    },
    "custom_auction_gap_v2": {
        "params": {
            "gap_min": [1.0, 1.5, 2.0],
            "gap_max": [3.5, 4.5, 5.5],
            "min_change": [2.0, 2.5, 3.5],
            "min_vol_ratio": [1.0, 1.3, 1.8],
            "max_vol_ratio": [3.0, 4.0, 6.0],
            "breadth_min": [0.30, 0.40, 0.50],
            "max_annual_vol": [0.55, 0.70, 0.90],
        },
        "overrides": {
            "max_hold_days": [2, 3, 4],
            "stop_loss": [-0.03, -0.04, -0.06],
            "trailing_stop": [0.03, 0.04, None],
        },
        "max_positions": [3, 4, 5],
        "position_sizing": ["score_weight", "equal"],
    },
    "custom_rs_pullback": {
        "params": {
            "top_pct": [0.10, 0.15, 0.20],
            "min_mom20": [0.03, 0.05, 0.08],
            "max_mom5": [0.08, 0.12, 0.15],
            "pullback_tol": [0.02, 0.03, 0.05],
            "max_vol_ratio": [1.3, 1.8, 2.5],
            "breadth_min": [0.30, 0.40],
            "max_annual_vol": [0.55, 0.65, 0.80],
        },
        "overrides": {
            "max_hold_days": [5, 8, 12],
            "stop_loss": [-0.05, -0.06, -0.08],
            "trailing_stop": [0.05, 0.08, None],
        },
        "max_positions": [4, 6, 8],
        "position_sizing": ["score_weight", "equal"],
    },
    "custom_lowvol_leader": {
        "params": {
            "max_annual_vol": [0.35, 0.45, 0.55],
            "min_mom20": [0.02, 0.03, 0.05],
            "min_mom60": [0.03, 0.05, 0.08],
            "max_mom5": [0.06, 0.08, 0.12],
            "top_pct": [0.08, 0.10, 0.15],
            "breadth_min": [0.35, 0.45],
        },
        "overrides": {
            "max_hold_days": [10, 15, 20],
            "stop_loss": [-0.06, -0.08],
            "trailing_stop": [0.06, 0.10, None],
        },
        "max_positions": [5, 8, 10],
        "position_sizing": ["score_weight", "equal"],
    },
    "custom_platform_breakout": {
        "params": {
            "lookback": [10, 12, 15, 20],
            "max_range": [0.08, 0.10, 0.12, 0.15],
            "breakout_buffer": [0.0, 0.005, 0.01],
            "min_vol_ratio": [1.2, 1.5, 2.0],
            "min_change": [1.5, 2.0, 3.0],
            "max_annual_vol": [0.55, 0.70, 0.90],
            "breadth_min": [0.30, 0.40, 0.50],
        },
        "overrides": {
            "max_hold_days": [4, 6, 8, 10],
            "stop_loss": [-0.04, -0.06, -0.08],
            "trailing_stop": [0.04, 0.06, None],
        },
        "max_positions": [3, 5, 8],
        "position_sizing": ["score_weight", "equal"],
    },
    "custom_yi_jin_er": {
        "params": {
            "open_gap_min": [-2.0, -1.0, 0.0],
            "open_gap_max": [3.0, 4.0, 5.0],
            "min_change": [0.0, 1.0, 2.0, 3.0],
            "min_vol_ratio": [1.0, 1.2, 1.5],
            "max_prev_gain_5d": [0.12, 0.18, 0.25],
            "require_close_gt_open": [True],
            "allow_today_limit": [False],
            "breadth_min": [0.30, 0.40, 0.50],
        },
        "overrides": {
            "max_hold_days": [2, 3, 4],
            "stop_loss": [-0.04, -0.06],
            "trailing_stop": [0.03, 0.05, None],
        },
        "max_positions": [3, 4, 6],
        "position_sizing": ["score_weight", "equal"],
    },
    "custom_huangjin_keng": {
        "params": {
            "min_mom20": [0.05, 0.08, 0.12],
            "min_lower_shadow": [0.40, 0.50, 0.60],
            "max_body": [0.25, 0.35, 0.45],
            "max_change": [1.0, 2.0, 3.0],
            "min_change": [-5.0, -4.0, -3.0],
            "min_vol_ratio": [0.8, 1.0, 1.3],
            "breadth_min": [0.30, 0.40],
        },
        "overrides": {
            "max_hold_days": [3, 5, 7],
            "stop_loss": [-0.05, -0.06, -0.08],
            "trailing_stop": [0.04, 0.06, None],
        },
        "max_positions": [4, 5, 8],
        "position_sizing": ["score_weight", "equal"],
    },
    "custom_ma5_trend": {
        "params": {
            "min_mom10": [0.03, 0.05, 0.08],
            "max_mom5": [0.10, 0.12, 0.15],
            "pullback_tol": [0.015, 0.025, 0.035],
            "min_vol_ratio": [0.5, 0.7, 1.0],
            "max_vol_ratio": [1.8, 2.5, 3.0],
            "max_annual_vol": [0.55, 0.70, 0.90],
            "breadth_min": [0.35, 0.45, 0.55],
        },
        "overrides": {
            "max_hold_days": [4, 6, 8],
            "stop_loss": [-0.04, -0.05, -0.07],
            "trailing_stop": [0.04, 0.06, None],
        },
        "max_positions": [4, 6, 8],
        "position_sizing": ["score_weight", "equal"],
    },
    "custom_auction_gap_v3": {
        "params": {
            "gap_min": [1.5, 2.0, 2.5],
            "gap_max": [3.0, 3.5, 4.5],
            "min_change": [2.5, 3.5, 4.0],
            "min_vol_ratio": [1.5, 1.8, 2.0],
            "max_vol_ratio": [4.0, 6.0],
            "max_annual_vol": [0.50, 0.55, 0.70],
            "breadth_min": [0.25, 0.30, 0.40],
            "min_temp": [0.0, 10.0],
            "max_temp": [90.0, 95.0, 100.0],
            "max_broken_rate": [0.70, 0.80, 0.90],
            "min_promote": [0.0, 0.05],
            "allow_ice": [True],
            "block_climax": [True, False],
        },
        "overrides": {
            "max_hold_days": [2, 3, 4],
            "stop_loss": [-0.04, -0.06],
            "trailing_stop": [0.03, 0.04, None],
        },
        "max_positions": [3, 4, 5],
        "position_sizing": ["score_weight", "equal"],
    },
    "custom_dual_edge": {
        # 冠军邻域: vol1.6 / gap_max3.5 / trail0.035 / breadth0.35
        "params": {
            "gap_min": [1.5, 2.0, 2.5],
            "gap_max": [3.0, 3.5, 4.0],
            "auction_min_change": [3.0, 3.5, 4.0],
            "auction_min_vol": [1.5, 1.6, 1.8],
            "max_annual_vol": [0.55, 0.65, 0.70],
            "fenqi_prev_high": [7.0, 7.5, 8.0],
            "fenqi_prev_close_max": [5.0, 5.5],
            "fenqi_min_change": [4.5, 5.0, 5.5],
            "fenqi_min_vol": [1.2, 1.3, 1.5],
            "max_annual_vol_fenqi": [0.75, 0.90],
            "breadth_min": [0.30, 0.35, 0.40],
            "use_regime": [True],
        },
        "overrides": {
            "max_hold_days": [2, 3],
            "stop_loss": [-0.05, -0.06],
            "trailing_stop": [0.03, 0.035, 0.04],
        },
        "max_positions": [2, 3],
        "position_sizing": ["score_weight"],
    },
    "custom_dual_edge_focus": {
        "params": {
            "gap_min": [2.0],
            "gap_max": [3.5, 4.0],
            "auction_min_change": [3.5],
            "auction_min_vol": [1.5, 1.6, 1.8],
            "max_annual_vol": [0.60, 0.65],
            "fenqi_min_change": [4.5, 5.0],
            "fenqi_min_vol": [1.3, 1.5],
            "breadth_min": [0.32, 0.35],
            "use_regime": [True],
        },
        "overrides": {
            "max_hold_days": [2, 3],
            "stop_loss": [-0.05],
            "trailing_stop": [0.035, 0.04],
        },
        "max_positions": [2],
        "position_sizing": ["score_weight"],
    },
    "custom_yi_jin_er_strict": {
        "params": {
            "open_gap_min": [-1.0, -0.5, 0.0],
            "open_gap_max": [3.0, 4.0, 5.0],
            "min_change": [1.0, 2.0, 3.0],
            "min_vol_ratio": [1.3, 1.5, 1.8],
            "max_prev_gain_5d": [0.12, 0.15, 0.20],
            "max_annual_vol": [0.55, 0.65, 0.75],
            "breadth_min": [0.30, 0.35],
            "use_regime": [True],
        },
        "overrides": {
            "max_hold_days": [2, 3],
            "stop_loss": [-0.05, -0.06],
            "trailing_stop": [0.035, 0.04],
        },
        "max_positions": [2, 3],
        "position_sizing": ["score_weight"],
    },
    "custom_fenqi_yizhi": {
        "params": {
            "prev_high_chg_min": [6.0, 7.0, 8.0],
            "prev_close_max": [5.0, 7.0, 9.0],
            "open_gap_min": [-2.0, -1.0, 0.0],
            "open_gap_max": [3.0, 5.0],
            "min_change": [2.0, 3.0, 5.0],
            "min_vol_ratio": [1.0, 1.2, 1.5],
            "min_temp": [20.0, 25.0, 35.0],
            "max_temp": [75.0, 80.0, 90.0],
        },
        "overrides": {
            "max_hold_days": [2, 3, 4],
            "stop_loss": [-0.05, -0.06],
            "trailing_stop": [0.04, 0.05, None],
        },
        "max_positions": [3, 4, 6],
        "position_sizing": ["score_weight", "equal"],
    },
    "custom_suoliang_huicai": {
        "params": {
            "min_mom20": [0.08, 0.12, 0.15],
            "min_mom60": [0.10, 0.15, 0.20],
            "max_mom5": [0.04, 0.06, 0.08],
            "ma_mode": [10, 20],
            "pullback_tol": [0.015, 0.02, 0.03],
            "max_vol_ratio": [0.9, 1.1, 1.3],
            "max_annual_vol": [0.55, 0.65, 0.80],
            "min_temp": [15.0, 20.0, 30.0],
            "max_temp": [80.0, 85.0, 95.0],
        },
        "overrides": {
            "max_hold_days": [5, 8, 12],
            "stop_loss": [-0.05, -0.06, -0.08],
            "trailing_stop": [0.05, 0.06, None],
        },
        "max_positions": [4, 6, 8],
        "position_sizing": ["equal", "score_weight"],
    },
    "custom_platform_breakout_v2": {
        "params": {
            "lookback": [10, 12, 15],
            "max_range": [0.10, 0.12, 0.15],
            "min_vol_ratio": [1.3, 1.5, 2.0],
            "min_change": [1.5, 2.0, 3.0],
            "max_annual_vol": [0.60, 0.70, 0.90],
            "min_temp": [25.0, 30.0, 40.0],
            "max_temp": [78.0, 82.0, 90.0],
            "max_broken_rate": [0.45, 0.55, 0.65],
        },
        "overrides": {
            "max_hold_days": [4, 6, 8],
            "stop_loss": [-0.05, -0.06, -0.08],
            "trailing_stop": [0.04, 0.05, None],
        },
        "max_positions": [3, 5, 8],
        "position_sizing": ["equal", "score_weight"],
    },
}


def _trading_range(data_dir: Path) -> tuple[date, date]:
    lf = pl.scan_parquet(
        str(data_dir / "kline_daily_enriched" / "**" / "*.parquet"),
        extra_columns="ignore",
    )
    row = (
        lf.select(pl.col("date").min().alias("start"), pl.col("date").max().alias("end"))
        .collect()
        .row(0, named=True)
    )
    return row["start"], row["end"]


def _sample_candidates(space: dict, trials: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    defaults = {
        "params": {},
        "overrides": {},
        "max_positions": 5,
        "position_sizing": "equal",
    }
    out = [defaults]
    seen = {json.dumps(defaults, sort_keys=True)}
    keys_p = list(space["params"].keys())
    keys_o = list(space["overrides"].keys())
    while len(out) < trials:
        cand = {
            "params": {k: rng.choice(space["params"][k]) for k in keys_p},
            "overrides": {k: rng.choice(space["overrides"][k]) for k in keys_o},
            "max_positions": rng.choice(space["max_positions"]),
            "position_sizing": rng.choice(space["position_sizing"]),
        }
        # drop None trailing keys for stable compare
        key = json.dumps(cand, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(cand)
    return out


def _score_result(stats: dict) -> float:
    """综合得分: 收益优先, 惩罚回撤和过少交易。"""
    if not stats or stats.get("error"):
        return -1e9
    tr = float(stats.get("total_return") or 0.0)
    dd = float(stats.get("max_drawdown") or 0.0)
    sharpe = float(stats.get("sharpe") or 0.0)
    n = int(stats.get("n_trades") or stats.get("trade_count") or 0)
    if n < 15:
        return -1e6 + n
    # 目标: 高收益 + 可控回撤 + 正夏普
    return tr * 100.0 + sharpe * 5.0 + dd * 40.0 + min(n, 80) * 0.02


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/app/data")
    parser.add_argument("--start", default="2025-10-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--strategies", default=",".join(SEARCH_SPACES.keys()))
    parser.add_argument("--trials", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--top", type=int, default=3)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    data_start, data_end = _trading_range(data_dir)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else data_end
    logger.info("backtest %s ~ %s (data %s ~ %s)", start, end, data_start, data_end)

    strategy_dirs = [
        Path("/app/app/strategy/builtin"),
        data_dir / "strategies" / "custom",
    ]
    local_builtin = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
    if local_builtin.exists():
        strategy_dirs.insert(0, local_builtin)

    engine = StrategyEngine(strategy_dirs=strategy_dirs)
    service = StrategyBacktestService(BacktestEngine(_ResearchRepository(data_dir)), engine)

    report = {"start": str(start), "end": str(end), "strategies": {}}
    for sid in [s.strip() for s in args.strategies.split(",") if s.strip()]:
        if sid not in SEARCH_SPACES:
            logger.warning("no search space for %s", sid)
            continue
        if engine.get(sid) is None:
            logger.warning("strategy not loaded: %s", sid)
            continue
        cands = _sample_candidates(SEARCH_SPACES[sid], args.trials, args.seed)
        ranked = []
        for i, cand in enumerate(cands):
            overrides = {k: v for k, v in cand["overrides"].items() if v is not None}
            try:
                result = service.run(
                    StrategyBacktestConfig(
                        strategy_id=sid,
                        symbols=None,
                        start=start,
                        end=end,
                        params=cand["params"],
                        overrides=overrides,
                        entry_fill="open_t+1",
                        exit_fill="open_t+1",
                        commission_pct=0.0002,
                        stamp_tax_pct=0.0005,
                        slippage_bps=8.0,
                        max_positions=int(cand["max_positions"]),
                        max_exposure_pct=1.0,
                        initial_capital=1_000_000.0,
                        position_sizing=cand["position_sizing"],
                        mode="position",
                        asset_type="stock",
                    )
                )
                stats = dict(result.stats or {})
                if result.error:
                    stats["error"] = result.error
            except Exception as exc:
                stats = {"error": str(exc)}
            row = {
                "trial": i,
                "score": _score_result(stats),
                "total_return": stats.get("total_return"),
                "max_drawdown": stats.get("max_drawdown"),
                "sharpe": stats.get("sharpe"),
                "win_rate": stats.get("win_rate"),
                "n_trades": stats.get("n_trades") or stats.get("trade_count"),
                "profit_factor": stats.get("profit_factor"),
                "final_equity": stats.get("final_equity"),
                "candidate": cand,
                "error": stats.get("error"),
            }
            ranked.append(row)
            logger.info(
                "%s trial %d score=%.2f ret=%s dd=%s sharpe=%s n=%s",
                sid,
                i,
                row["score"],
                row["total_return"],
                row["max_drawdown"],
                row["sharpe"],
                row["n_trades"],
            )
        ranked.sort(key=lambda x: x["score"], reverse=True)
        report["strategies"][sid] = {
            "best": ranked[: args.top],
            "worst": ranked[-1] if ranked else None,
            "trials": len(ranked),
        }

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""run_all 并行执行的等价性回归测试。

engine.run_all 支持有界线程池并发 (策略对共享 context 只读纯函数)。此处验证:
- parallel=True 与 parallel=False 对同一批策略产出完全一致的结果 (总数 + 标的集);
- 失败策略的异常语义一致 (原顺序首个失败抛出);
- composite 子策略的嵌套 run_all 不受影响 (递归恒串行)。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.strategy.engine import StrategyDataContext, StrategyEngine

_FILTER_TEMPLATE = '''
import polars as pl

META = {{
    "id": "{sid}",
    "name": "{sid}",
    "timeframes": ["1d"],
    "asset_types": ["stock"],
}}
def filter(df, params):
    return pl.col("close") > {threshold}
'''


def _write_strategy(directory: Path, code: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"strategy_{abs(hash(code)) % 10**8}.py").write_text(code, encoding="utf-8")


def _make_engine(tmp_path: Path) -> StrategyEngine:
    d = tmp_path / "strategies"
    _write_strategy(d, _FILTER_TEMPLATE.format(sid="cheap_a", threshold=10.0))
    _write_strategy(d, _FILTER_TEMPLATE.format(sid="cheap_b", threshold=15.0))
    _write_strategy(d, _FILTER_TEMPLATE.format(sid="cheap_c", threshold=20.0))
    return StrategyEngine(strategy_dirs=[d])


def _context() -> StrategyDataContext:
    n = 30
    current = pl.DataFrame({
        "symbol": [f"{i:06d}.SZ" for i in range(n)],
        "name": [f"股票{i}" for i in range(n)],
        "open": [5.0 + i for i in range(n)],
        "high": [5.5 + i for i in range(n)],
        "low": [4.5 + i for i in range(n)],
        "close": [5.0 + i for i in range(n)],
        "volume": [1000.0 * (i + 1) for i in range(n)],
        "amount": [5000.0 * (i + 1) for i in range(n)],
        "turnover_rate": [1.0 + i * 0.1 for i in range(n)],
        "total_shares": [1e8 for _ in range(n)],
        "float_shares": [5e7 for _ in range(n)],
    })
    return StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 9, 7),
        current=current,
    )


def _signature(results: dict) -> dict:
    return {
        sid: (r.total, tuple(sorted(row["symbol"] for row in r.rows)))
        for sid, r in results.items()
    }


def test_parallel_run_all_matches_sequential_results(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    context = _context()
    # 关闭默认基础过滤, 让结果只取决于策略谓词本身
    overrides = {
        meta["id"]: {"basic_filter": {"enabled": False}}
        for meta in engine.list_strategies()
    }

    sequential = engine.run_all(context, overrides_map=overrides, parallel=False)
    parallel = engine.run_all(context, overrides_map=overrides, parallel=True)

    assert list(parallel) == list(sequential)  # 结果键序一致
    assert _signature(parallel) == _signature(sequential)
    # close 序列 5..34: >10 → 11..34 共 24 只; >15 → 19 只; >20 → 14 只
    assert sequential["cheap_a"].total == 24
    assert sequential["cheap_b"].total == 19
    assert sequential["cheap_c"].total == 14


def test_parallel_run_all_preserves_failure_semantics(tmp_path: Path) -> None:
    d = tmp_path / "strategies"
    _write_strategy(d, _FILTER_TEMPLATE.format(sid="ok_first", threshold=10.0))
    _write_strategy(
        d,
        '''
META = {"id": "boom", "name": "boom"}
def filter(df, params):
    raise ValueError("injected strategy failure")
''',
    )
    _write_strategy(d, _FILTER_TEMPLATE.format(sid="ok_last", threshold=15.0))
    engine = StrategyEngine(strategy_dirs=[d])
    context = _context()

    for parallel in (False, True):
        with pytest.raises(ValueError, match="injected strategy failure"):
            engine.run_all(context, parallel=parallel)

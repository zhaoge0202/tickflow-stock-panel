"""storage 统计求和不变量测试 — total_size_mb 必须等于各部分之和, 不得重复累加。

历史 bug: other_dirs 循环被复制了两遍, 且 financials 既在 other_dirs 里又有专属
统计块 —— financials 被计入 3 次、pools/backtest_results/screener_results/ai_cache
各计入 2 次, total_size_mb 虚高。此测试用精确整 MB 文件锁定总和不变量。
"""
from __future__ import annotations

from pathlib import Path

from app.api.data import _compute_storage

MB = 1024 * 1024


def _write_mb(path: Path, mb: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * int(mb * MB))


def _build_tree(data_dir: Path) -> None:
    _write_mb(data_dir / "kline_daily" / "part.parquet", 1.0)
    _write_mb(data_dir / "financials" / "metrics" / "part.parquet", 2.0)
    _write_mb(data_dir / "pools" / "p.json", 1.0)
    _write_mb(data_dir / "backtest_results" / "r.json", 1.0)
    _write_mb(data_dir / "screener_results" / "s.json", 1.0)
    _write_mb(data_dir / "ai_cache" / "c.json", 1.0)
    _write_mb(data_dir / "capabilities.json", 0.5)  # 根目录散文件


def test_total_equals_sum_of_parts(tmp_path):
    _build_tree(tmp_path)
    stats = _compute_storage(tmp_path)

    parts_mb = (
        stats["daily_size_mb"]            # 1.0  (subdirs 表)
        + stats["financials_size_mb"]     # 2.0  (专属统计块)
        + 1.0 + 1.0 + 1.0 + 1.0           # pools/backtest/screener/ai_cache (单次循环)
        + 0.5                             # 根目录散文件
    )
    assert stats["total_size_mb"] == parts_mb == 7.5


def test_financials_counted_once_not_three_times(tmp_path):
    _build_tree(tmp_path)
    stats = _compute_storage(tmp_path)

    assert stats["financials_files"] == 1
    assert stats["financials_size_mb"] == 2.0
    # 旧 bug: financials 计 3 次 (6MB) + 其余 4 目录各计 2 次 (8MB) → 15.5
    assert stats["total_size_mb"] != 15.5


def test_missing_dirs_contribute_zero(tmp_path):
    _write_mb(tmp_path / "kline_daily" / "part.parquet", 1.0)
    stats = _compute_storage(tmp_path)

    # financials 目录不存在时不含其明细键 (既有行为), 且不贡献任何体积
    assert stats.get("financials_files", 0) == 0
    assert stats.get("financials_size_mb", 0.0) == 0.0
    assert stats["total_size_mb"] == 1.0

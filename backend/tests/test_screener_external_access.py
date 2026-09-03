"""#224 回归: screener 自定义 SQL 的内存连接必须关闭 external_access。

conditions/order_by 是用户可控的 SQL 片段; 隔离连接若允许外部访问,
注入的 read_parquet/COPY 可读写任意文件 (文件写 RCE)。
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import polars as pl

from app.services.screener import ScreenerService


def _service_with_panel(panel: pl.DataFrame) -> ScreenerService:
    svc = ScreenerService(MagicMock(), asset_type="stock")
    svc._load_enriched_for_date = lambda d: panel  # type: ignore[method-assign]
    return svc


def _panel() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["600000.SH", "000001.SZ"],
            "close": [10.0, 20.0],
            "turnover_rate": [1.0, 2.0],
        }
    )


def test_normal_condition_still_works() -> None:
    svc = _service_with_panel(_panel())
    result = svc.run(date(2026, 9, 2), ["close > 15"], limit=10)
    assert [r["symbol"] for r in result.rows] == ["000001.SZ"]


def test_injected_read_parquet_is_rejected() -> None:
    # 注入试图读任意文件: external_access 关闭后 DuckDB 直接报错,
    # run() 的 except 分支吞错返回空结果, 而非泄漏文件内容
    svc = _service_with_panel(_panel())
    result = svc.run(
        date(2026, 9, 2),
        ["1=1) UNION SELECT * FROM read_parquet('/etc/passwd') --"],
        limit=10,
    )
    assert result.rows == []


def test_injected_read_of_valid_parquet_leaks_nothing(tmp_path) -> None:
    # 用合法 parquet 做注入目标才能区分新旧代码: /etc/passwd 不是 parquet,
    # 未加固的连接读它也会报错; 合法同构文件在未加固连接下会真的混入结果
    victim = tmp_path / "victim.parquet"
    _panel().write_parquet(victim)
    svc = _service_with_panel(pl.DataFrame(
        {"symbol": ["999999.SZ"], "close": [99.0], "turnover_rate": [9.0]}
    ))
    result = svc.run(
        date(2026, 9, 2),
        [f"1=1) UNION SELECT * FROM read_parquet('{victim}') --"],
        limit=10,
    )
    # 加固后注入被拒 → 整条查询 fail-closed 返回空 (或至多剩原面板行);
    # 未加固时 victim 的 2 行会混入结果
    assert all(r["symbol"] == "999999.SZ" for r in result.rows)
    assert len(result.rows) <= 1


def test_injected_copy_write_is_rejected(tmp_path) -> None:
    target = tmp_path / "pwned.csv"
    svc = _service_with_panel(_panel())
    result = svc.run(
        date(2026, 9, 2),
        [f"close > 0); COPY enriched TO '{target}' --"],
        limit=10,
    )
    assert result.rows == []
    assert not target.exists()


def test_order_by_injection_also_isolated(tmp_path) -> None:
    # order_by 同样是拼接片段, 不能借 external 函数逃逸
    svc = _service_with_panel(_panel())
    result = svc.run(
        date(2026, 9, 2),
        ["close > 0"],
        order_by=f"close; COPY enriched TO '{tmp_path / 'x.csv'}'",
        limit=10,
    )
    assert result.rows == []
    assert not (tmp_path / "x.csv").exists()


def test_external_access_switch_is_the_effective_barrier(tmp_path) -> None:
    # 正反对照: 同一条注入 SQL, 未关 external_access 的普通内存连接能读到
    # 任意 parquet 文件 (证明攻击面真实存在); 关闭后直接报错。
    import duckdb

    victim = tmp_path / "victim.parquet"
    _panel().write_parquet(victim)
    inject = f"SELECT * FROM read_parquet('{victim}')"

    plain = duckdb.connect(database=":memory:")
    try:
        assert plain.execute(inject).pl().height == 2  # 普通连接: 可读 → 攻击面成立
    finally:
        plain.close()

    hardened = duckdb.connect(
        database=":memory:", config={"enable_external_access": False}
    )
    try:
        raised = False
        try:
            hardened.execute(inject)
        except Exception:
            raised = True
        assert raised, "external_access=False 的连接不应能读外部文件"
    finally:
        hardened.close()

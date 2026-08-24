"""kline 每秒热路径测试 — _get_stock_info 走内存缓存, _attach_ext 复用签名缓存 value_map。

契约等值: 输出与旧的 DuckDB 扫描 / 逐配置读 parquet 路径一致。
"""
from __future__ import annotations

from types import SimpleNamespace

import polars as pl

from app.api import screener
from app.api.kline import _attach_ext, _get_stock_info
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField


class _FakeRepo:
    """最小 repo 替身: _get_stock_info/_load_ext_value_maps 只用到这些成员。"""

    def __init__(self, instruments: pl.DataFrame, data_dir) -> None:
        self._instruments = instruments
        self.store = SimpleNamespace(data_dir=data_dir, db=None)
        self.execute_one_calls = 0

    def get_instruments(self) -> pl.DataFrame:
        return self._instruments

    def execute_one(self, *args, **kwargs):
        self.execute_one_calls += 1
        raise AssertionError("热路径不得再走 DuckDB 扫 instruments parquet")


def _instruments_df() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ", "510300.SH"],
        "name": ["浦发银行", "平安银行", "沪深300ETF"],
        "total_shares": [1.0e9, 2.0e9, None],
        "float_shares": [5.0e8, 1.0e9, None],
    })


# ---------------------------------------------------------------- _get_stock_info

def test_get_stock_info_reads_memory_cache():
    repo = _FakeRepo(_instruments_df(), None)
    assert _get_stock_info(repo, "600000.SH") == {
        "name": "浦发银行", "total_shares": 1.0e9, "float_shares": 5.0e8,
    }
    assert repo.execute_one_calls == 0


def test_get_stock_info_null_shares_become_none():
    repo = _FakeRepo(_instruments_df(), None)
    info = _get_stock_info(repo, "510300.SH")
    assert info["name"] == "沪深300ETF"
    assert info["total_shares"] is None
    assert info["float_shares"] is None


def test_get_stock_info_missing_symbol_returns_empty():
    repo = _FakeRepo(_instruments_df(), None)
    assert _get_stock_info(repo, "999999.SH") == {}


def test_get_stock_info_missing_columns_returns_empty_like_sql_error():
    df = pl.DataFrame({"symbol": ["600000.SH"], "name": ["浦发银行"]})
    repo = _FakeRepo(df, None)
    assert _get_stock_info(repo, "600000.SH") == {}


def test_get_stock_info_repo_failure_returns_empty():
    repo = SimpleNamespace()
    repo.get_instruments = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    assert _get_stock_info(repo, "600000.SH") == {}


# ---------------------------------------------------------------- _attach_ext

def test_attach_ext_empty_columns_untouched():
    resp = {"symbol": "600000.SH", "stock_info": {"name": "x"}, "rows": []}
    repo = _FakeRepo(_instruments_df(), None)
    out = _attach_ext(resp, repo, "600000.SH", None)
    assert out is resp
    assert "ext" not in out["stock_info"]
    out = _attach_ext(resp, repo, "600000.SH", "   ")
    assert "ext" not in out["stock_info"]


def test_attach_ext_invalid_spec_untouched():
    resp = {"symbol": "600000.SH", "stock_info": {"name": "x"}, "rows": []}
    repo = _FakeRepo(_instruments_df(), None)
    out = _attach_ext(resp, repo, "600000.SH", "foo_bar")  # 无 '.' 分隔
    assert out is resp
    assert "ext" not in out["stock_info"]


def test_attach_ext_maps_values_from_loader(monkeypatch):
    resp = {"symbol": "600000.SH", "stock_info": {"name": "x"}, "rows": []}
    original_info = resp["stock_info"]
    repo = _FakeRepo(_instruments_df(), None)
    monkeypatch.setattr(
        screener, "_load_ext_value_maps",
        lambda r, cols: {"cfg1__score": {"600000.SH": 88.5}, "cfg1__note": {}},
    )
    out = _attach_ext(resp, repo, "600000.SH", "cfg1.score,cfg1.note")
    assert out["stock_info"]["ext"] == {"cfg1__score": 88.5, "cfg1__note": None}
    # 原地更新 resp 是该函数契约; 但原 stock_info 字典对象不被修改 (与旧实现一致)
    assert out is resp
    assert "ext" not in original_info
    assert out["stock_info"] is not original_info


def test_attach_ext_loader_failure_yields_none_values(monkeypatch):
    resp = {"symbol": "600000.SH", "stock_info": {}, "rows": []}
    repo = _FakeRepo(_instruments_df(), None)

    def _boom(r, cols):
        raise RuntimeError("boom")

    monkeypatch.setattr(screener, "_load_ext_value_maps", _boom)
    out = _attach_ext(resp, repo, "600000.SH", "cfg1.score")
    assert out["stock_info"]["ext"] == {"cfg1__score": None}


# ------------------------------------------- _attach_ext 与真实 _load_ext_value_maps 集成

def _setup_ext_snapshot(tmp_path):
    store = ExtConfigStore(tmp_path)
    store.upsert(ExtConfig(
        id="benchx", label="基准扩展", mode="snapshot",
        fields=[ExtField(name="score", dtype="float")],
    ))
    cfg_dir = tmp_path / "ext_data" / "benchx"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [f"{600000 + i}.SH" for i in range(50)],
        "score": [float(i) for i in range(50)],
    }).write_parquet(cfg_dir / "part.parquet")
    return store


def test_attach_ext_real_loader_returns_parquet_values(tmp_path):
    _setup_ext_snapshot(tmp_path)
    repo = _FakeRepo(_instruments_df(), tmp_path)
    resp = {"symbol": "600005.SH", "stock_info": {"name": "x"}, "rows": []}
    out = _attach_ext(resp, repo, "600005.SH", "benchx.score")
    assert out["stock_info"]["ext"] == {"benchx__score": 5.0}
    assert isinstance(out["stock_info"]["ext"]["benchx__score"], float)


def test_attach_ext_real_loader_unknown_symbol_is_none(tmp_path):
    _setup_ext_snapshot(tmp_path)
    repo = _FakeRepo(_instruments_df(), tmp_path)
    resp = {"symbol": "999999.SH", "stock_info": {}, "rows": []}
    out = _attach_ext(resp, repo, "999999.SH", "benchx.score")
    assert out["stock_info"]["ext"] == {"benchx__score": None}

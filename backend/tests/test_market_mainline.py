"""市场主线(market_mainline)与过滤配置单元测试。"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.services import market_mainline, preferences


def _write_enriched(root, rows: list[dict]) -> None:
    enriched = root / "kline_daily_enriched"
    by_date: dict[date, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    for d, day_rows in by_date.items():
        part = enriched / f"date={d.isoformat()}" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(day_rows).write_parquet(part)


def _fake_repo(tmp_path):
    import types

    return types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path))


def _patch_map(monkeypatch, mapping: dict[str, list[str]], kind: str = "concept") -> None:
    map_df = pl.DataFrame(
        {"_sym_up": [s for s, ms in mapping.items() for _ in ms],
         kind: [m for _, ms in mapping.items() for m in ms]},
        schema={"_sym_up": pl.Utf8, kind: pl.Utf8},
    ).unique()

    def fake_load(repo, k="concept"):
        return (map_df, map_df[kind].n_unique()) if k == kind else (pl.DataFrame(), 0)

    monkeypatch.setattr(market_mainline, "_load_concept_map_df", fake_load)


def _mk_rows(d: date, spec: list[tuple[str, int, float]]) -> list[dict]:
    return [
        {"symbol": sym, "date": d, "consecutive_limit_ups": consec, "amount": amt}
        for sym, consec, amt in spec
    ]


class TestComputeMainline:
    def _setup(self, tmp_path, monkeypatch):
        d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
        # 概念 X: d1 三个涨停(2,1,1), d2 三个涨停(3,2,1); 概念 Y: 单股 2 板
        # S5 无概念映射; 大概念 BIG 成员 700 家但只有 5 家涨停(数据里只写 5 行)
        rows = _mk_rows(d1, [("S1.SH", 2, 5e8), ("S2.SH", 1, 1e8), ("S3.SH", 1, 2e8),
                             ("S4.SH", 2, 3e8), ("S5.SH", 1, 1e8),
                             ("B1.SH", 1, 1e8), ("B2.SH", 1, 1e8)])
        rows += _mk_rows(d2, [("S1.SH", 3, 6e8), ("S2.SH", 2, 2e8), ("S3.SH", 0, 1e8),
                              ("S4.SH", 3, 4e8), ("S5.SH", 1, 1e8),
                              ("B1.SH", 2, 1e8), ("B2.SH", 0, 1e8)])
        _write_enriched(tmp_path, rows)
        mapping = {
            "S1.SH": ["X"], "S2.SH": ["X"], "S3.SH": ["X"],
            "S4.SH": ["X", "Y"], "S5.SH": [],
            "B1.SH": ["BIG"], "B2.SH": ["BIG"],
            **{f"F{i}.SH": ["BIG"] for i in range(700)},  # BIG 成员 702 → 超 600 上限
        }
        _patch_map(monkeypatch, mapping)
        return _fake_repo(tmp_path), d1, d2

    def test_aggregation_and_big_concept_filter(self, tmp_path, monkeypatch):
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        out = market_mainline.compute_mainline_range(
            repo, tmp_path, d1, d2, kind="concept",
            filter_cfg={"min_members": 4, "max_members": 600, "blacklist": []},
        )
        members = set(out["member"].to_list())
        assert "BIG" not in members  # 成员数超上限被过滤
        assert "X" in members
        x_d2 = out.filter((pl.col("date") == d2) & (pl.col("member") == "X")).to_dicts()[0]
        assert x_d2["limit_up_count"] == 3        # S1,S2,S4
        assert x_d2["ge2_count"] == 3
        assert x_d2["max_boards"] == 3
        assert x_d2["rungs_filled"] == 2          # 档位 {2,3}
        assert x_d2["leader_symbol"] == "S1.SH"   # 最高板且成交额大
        assert x_d2["rank"] == 1

    def test_bare_dataframe_map_return_compat(self, tmp_path, monkeypatch):
        """回归: _load_concept_map_df 若返回裸 DataFrame(旧版/被改动实现),
        元组解包会把两列拆成两个 Series, Series.is_empty() 能通过但后续
        group_by 报 'Series' object has no attribute 'group_by'
        (用户反馈: 市场环境点重算偶发报错)。compute 应兼容不炸。"""
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        bare = pl.DataFrame(
            {"_sym_up": ["S1.SH", "S2.SH", "S3.SH", "S4.SH", "B1.SH", "B2.SH"],
             "concept": ["X", "X", "X", "X", "BIG", "BIG"]},
            schema={"_sym_up": pl.Utf8, "concept": pl.Utf8},
        )
        monkeypatch.setattr(
            market_mainline, "_load_concept_map_df",
            lambda r, k="concept": bare if k == "concept" else
            pl.DataFrame(schema={"_sym_up": pl.Utf8, "industry": pl.Utf8}),
        )
        out = market_mainline.compute_mainline_range(
            repo, tmp_path, d1, d2, kind="concept",
            filter_cfg={"min_members": 4, "max_members": 600, "blacklist": []},
        )
        assert "X" in set(out["member"].to_list())
        assert out.filter((pl.col("date") == d2) & (pl.col("member") == "X")).height == 1

    def test_blacklist_and_min_limit_up(self, tmp_path, monkeypatch):
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        out = market_mainline.compute_mainline_range(
            repo, tmp_path, d1, d2, kind="concept",
            filter_cfg={"min_members": 1, "max_members": 5000, "blacklist": ["X"]},
        )
        # X 被黑名单; BIG 只有 2-3 家涨停 < _MIN_LIMIT_UP=3 也不参与 → 只剩空/无 X
        assert "X" not in set(out["member"].to_list())

    def test_upsert_replaces_same_day_kind(self, tmp_path, monkeypatch):
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        cfg = {"min_members": 4, "max_members": 600, "blacklist": []}
        first = market_mainline.compute_mainline_range(repo, tmp_path, d1, d1, kind="concept", filter_cfg=cfg)
        market_mainline.upsert_mainline_history(tmp_path, first)
        both = market_mainline.compute_mainline_range(repo, tmp_path, d1, d2, kind="concept", filter_cfg=cfg)
        market_mainline.upsert_mainline_history(tmp_path, both)
        stored = pl.read_parquet(market_mainline.mainline_path(tmp_path))
        assert set(stored["date"].to_list()) == {d1, d2}
        # 同日重算不产生重复行
        assert stored.filter(pl.col("date") == d1).height == first.height

    def test_incremental_fills_missing_days(self, tmp_path, monkeypatch):
        repo, d1, d2 = self._setup(tmp_path, monkeypatch)
        cfg = {"min_members": 4, "max_members": 600, "blacklist": []}
        first = market_mainline.compute_mainline_range(repo, tmp_path, d1, d1, kind="concept", filter_cfg=cfg)
        market_mainline.upsert_mainline_history(tmp_path, first)
        new = market_mainline.compute_mainline_incremental(repo, tmp_path, kind="concept")
        assert not new.is_empty()
        assert set(new["date"].to_list()) == {d2}

    def test_industry_level_truncation(self, tmp_path, monkeypatch):
        d1 = date(2024, 1, 2)
        rows = _mk_rows(d1, [("S1.SH", 2, 5e8), ("S2.SH", 1, 1e8),
                             ("S3.SH", 1, 2e8), ("S4.SH", 3, 4e8)])
        _write_enriched(tmp_path, rows)
        _patch_map(
            monkeypatch,
            {"S1.SH": ["计算机-软件开发-垂直应用软件"],
             "S2.SH": ["计算机-软件开发-垂直应用软件"],
             "S3.SH": ["计算机-IT服务-IT服务Ⅲ"],
             "S4.SH": ["计算机-软件开发-垂直应用软件"]},
            kind="industry",
        )
        out = market_mainline.compute_mainline_range(
            _fake_repo(tmp_path), tmp_path, d1, d1, kind="industry",
            filter_cfg={"min_members": 1, "max_members": 5000, "blacklist": []},
        )
        members = set(out["member"].to_list())
        assert "计算机-软件开发" in members
        assert all(m.count("-") <= 1 for m in members)
        sw = out.filter(pl.col("member") == "计算机-软件开发").to_dicts()[0]
        assert sw["limit_up_count"] == 3
        assert sw["max_boards"] == 3


class TestMainlineFilterPreferences:
    def test_blacklist_string_parsing_and_clamp(self, tmp_path, monkeypatch):
        path = tmp_path / "preferences.json"
        monkeypatch.setattr(preferences, "_path", lambda: path)
        got = preferences.set_mainline_filter_config({
            "max_members": 99999,          # 超上限被夹到 5000
            "min_members": 0,              # 低于下限被夹到 1
            "blacklist": "融资融券, 沪股通；深股通",  # noqa: RUF001
        })
        assert got["max_members"] == 5000
        assert got["min_members"] == 1
        assert set(got["blacklist"]) == {"融资融券", "沪股通", "深股通"}
        # 部分更新: 只改黑名单, 其他保持
        got2 = preferences.set_mainline_filter_config({"blacklist": ["ST板块"]})
        assert got2["blacklist"] == ["ST板块"]
        assert got2["max_members"] == 5000

    def test_defaults(self, tmp_path, monkeypatch):
        path = tmp_path / "preferences.json"
        monkeypatch.setattr(preferences, "_path", lambda: path)
        cfg = preferences.get_mainline_filter_config()
        assert cfg == {"min_members": 4, "max_members": 600, "blacklist": [], "exclude_st": True}

    def test_sentiment_exclude_st_roundtrip(self, tmp_path, monkeypatch):
        path = tmp_path / "preferences.json"
        monkeypatch.setattr(preferences, "_path", lambda: path)
        assert preferences.get_sentiment_exclude_st() is True  # 默认剔除
        assert preferences.set_sentiment_exclude_st(False) is False
        assert preferences.get_sentiment_exclude_st() is False
        # 经主线过滤配置部分更新同样生效
        got = preferences.set_mainline_filter_config({"exclude_st": True})
        assert got["exclude_st"] is True


class TestExcludeST:
    """风险警示股剔除: 维表名称含 ST → 主线聚合前过滤。"""

    @staticmethod
    def _write_instruments(tmp_path, names: dict[str, str]) -> None:
        part = tmp_path / "instruments" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": list(names),
            "name": list(names.values()),
        }).write_parquet(part)

    def _reset_cache(self, monkeypatch):
        monkeypatch.setattr(market_mainline, "_ST_SYMBOLS_CACHE", None)

    def test_load_risk_warning_symbols(self, tmp_path, monkeypatch):
        self._reset_cache(monkeypatch)
        self._write_instruments(tmp_path, {
            "s1.SH": "*ST环保", "S2.SH": "ST万邦", "S3.SZ": "正常股",
            "s4.BJ": "S*ST京", "S5.SH": "斯太尔",  # 中文名含"斯"不含 ST 标记
        })
        got = market_mainline.load_risk_warning_symbols(tmp_path)
        assert got == frozenset({"S1.SH", "S2.SH", "S4.BJ"})  # 大写归一
        # 缓存命中: 再次读取不重扫磁盘
        self._write_instruments(tmp_path, {"S9.SH": "ST新增"})
        assert market_mainline.load_risk_warning_symbols(tmp_path) == got

    def test_load_risk_warning_symbols_empty_dir(self, tmp_path, monkeypatch):
        self._reset_cache(monkeypatch)
        assert market_mainline.load_risk_warning_symbols(tmp_path) == frozenset()

    def test_compute_mainline_excludes_st(self, tmp_path, monkeypatch):
        """S1(ST) 涨停被剔除 → 概念 X 计数/高度/龙头随之变化; 关闭开关恢复。"""
        self._reset_cache(monkeypatch)
        self._write_instruments(tmp_path, {"S1.SH": "*ST一", "S2.SH": "正常一"})
        repo, d1, d2 = TestComputeMainline()._setup(tmp_path, monkeypatch)
        cfg = {"min_members": 4, "max_members": 600, "blacklist": []}

        out = market_mainline.compute_mainline_range(
            repo, tmp_path, d1, d2, kind="concept", filter_cfg=cfg, exclude_st=True,
        )
        x_d1 = out.filter((pl.col("date") == d1) & (pl.col("member") == "X")).to_dicts()[0]
        assert x_d1["limit_up_count"] == 3      # S1(ST) 被剔除, 剩 S2,S3,S4
        assert x_d1["ge2_count"] == 1           # 仅 S4=2板
        assert x_d1["max_boards"] == 2
        assert x_d1["leader_symbol"] == "S4.SH"

        out_keep = market_mainline.compute_mainline_range(
            repo, tmp_path, d1, d2, kind="concept", filter_cfg=cfg, exclude_st=False,
        )
        x_d1_keep = out_keep.filter((pl.col("date") == d1) & (pl.col("member") == "X")).to_dicts()[0]
        assert x_d1_keep["limit_up_count"] == 4
        assert x_d1_keep["ge2_count"] == 2      # S1=2板, S4=2板
        assert x_d1_keep["leader_symbol"] == "S1.SH"

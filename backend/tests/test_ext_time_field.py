"""日内序列表 (PullConfig.time_field) 测试: 写入多盘并存 + 默认行为不变。"""
from __future__ import annotations

from datetime import date

from app.services.ext_data import (
    ExtConfig,
    ExtField,
    PullConfig,
    rows_to_parquet,
)


def _config(time_field: str | None) -> ExtConfig:
    return ExtConfig(
        id="auction",
        label="集合竞价",
        mode="timeseries",
        fields=[
            ExtField("symbol", "string", "代码"),
            ExtField("ts", "string", "时刻"),
            ExtField("v", "float", "值"),
        ],
        pull=PullConfig(url="https://x", time_field=time_field),
    )


def test_pull_config_time_field_roundtrip_and_normalize():
    assert PullConfig().time_field is None                      # 缺省
    assert PullConfig(time_field="  ").time_field is None       # 空白归一 None
    p = PullConfig(time_field="ts")
    assert PullConfig.from_dict(p.to_dict()).time_field == "ts"
    assert PullConfig.from_dict({}).time_field is None          # 旧配置文件兼容


def test_rows_to_parquet_time_field_keeps_multiple_disks(tmp_path):
    cfg = _config(time_field="ts")
    day = date(2026, 9, 15)
    first = [
        {"symbol": "000001.SZ", "ts": "09:15:03", "v": 1.0},
        {"symbol": "000001.SZ", "ts": "09:18:00", "v": 2.0},
    ]
    assert rows_to_parquet(first, cfg, tmp_path, snapshot_date=day) == 2

    # 重拉同一日: 同 [symbol, ts] 覆盖 (keep=last), 新盘追加
    second = [
        {"symbol": "000001.SZ", "ts": "09:15:03", "v": 1.5},   # 更新
        {"symbol": "000001.SZ", "ts": "09:21:00", "v": 3.0},   # 新盘
    ]
    # 返回值 = 合并去重后的分区总行数 (2 旧 + 2 新 - 1 覆盖 = 3)
    assert rows_to_parquet(second, cfg, tmp_path, snapshot_date=day) == 3

    import polars as pl

    out = pl.read_parquet(
        tmp_path / "ext_data" / "auction" / "timeseries"
        / f"date={day.isoformat()}" / "part.parquet"
    ).sort("ts")
    assert out.height == 3
    by_ts = {r["ts"]: r["v"] for r in out.to_dicts()}
    assert by_ts == {"09:15:03": 1.5, "09:18:00": 2.0, "09:21:00": 3.0}


def test_rows_to_parquet_without_time_field_unchanged(tmp_path):
    """未配置 time_field: 默认按 symbol 去重, 行为与改造前完全一致。"""
    cfg = _config(time_field=None)
    day = date(2026, 9, 15)
    assert rows_to_parquet(
        [{"symbol": "000001.SZ", "ts": "09:15:03", "v": 1.0}], cfg, tmp_path,
        snapshot_date=day,
    ) == 1
    assert rows_to_parquet(
        [{"symbol": "000001.SZ", "ts": "09:18:00", "v": 2.0}], cfg, tmp_path,
        snapshot_date=day,
    ) == 1

    import polars as pl

    out = pl.read_parquet(
        tmp_path / "ext_data" / "auction" / "timeseries"
        / f"date={day.isoformat()}" / "part.parquet"
    )
    assert out.height == 1
    assert out.row(0, named=True)["v"] == 2.0

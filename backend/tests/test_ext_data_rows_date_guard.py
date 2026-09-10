"""`/rows`、`/dimension-members` 的 date 入参校验 — 非法日期返回 400 而不是拼进路径。

`_read_ext_dataframe` 用 `date=<入参>` 拼出时序分区目录名。同一文件的 `/sync`、
`/ingest`、`/backfill` 都先 `date.fromisoformat` 再用, 只有这条读取路径把原始
字符串直接拼进 `Path`, 于是 `date=x/../../../../kline_daily` 读到的是配置目录
之外的 `part.parquet`。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from fastapi import HTTPException

from app.api.ext_data import _read_ext_dataframe
from app.services.ext_data import ExtConfig, ExtField


def _cfg() -> ExtConfig:
    return ExtConfig(
        id="hot",
        label="人气",
        mode="timeseries",
        fields=[ExtField("symbol", "string"), ExtField("heat", "float")],
    )


def _layout(tmp_path: Path) -> Path:
    """建出真实目录形状: data/ext_data/hot/timeseries/ 和它外面的一份 parquet。"""
    data_dir = tmp_path / "data"
    partition = data_dir / "ext_data" / "hot" / "timeseries" / "date=2026-09-11"
    partition.mkdir(parents=True)
    pl.DataFrame({"symbol": ["000001.SZ"], "heat": [1.0]}).write_parquet(
        partition / "part.parquet"
    )

    outside = data_dir / "kline_daily"
    outside.mkdir(parents=True)
    pl.DataFrame({"symbol": ["不该被读到"], "heat": [9.9]}).write_parquet(
        outside / "part.parquet"
    )
    return data_dir


def test_rows_date_does_not_leave_the_config_directory(tmp_path: Path) -> None:
    data_dir = _layout(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        _read_ext_dataframe(_cfg(), data_dir, "x/../../../../kline_daily")

    assert excinfo.value.status_code == 400


@pytest.mark.parametrize(
    "bad",
    ["not-a-date", "2026-13-01", "2026/09/11", "../../../../kline_daily"],
)
def test_rows_rejects_a_date_that_is_not_a_date(tmp_path: Path, bad: str) -> None:
    data_dir = _layout(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        _read_ext_dataframe(_cfg(), data_dir, bad)

    assert excinfo.value.status_code == 400


def test_rows_still_reads_the_named_partition(tmp_path: Path) -> None:
    data_dir = _layout(tmp_path)

    df, active = _read_ext_dataframe(_cfg(), data_dir, "2026-09-11")

    assert active == "2026-09-11"
    assert df.get_column("symbol").to_list() == ["000001.SZ"]


def test_rows_returns_empty_for_a_valid_date_with_no_partition(tmp_path: Path) -> None:
    data_dir = _layout(tmp_path)

    df, active = _read_ext_dataframe(_cfg(), data_dir, "2026-01-02")

    assert active == "2026-01-02"
    assert df.is_empty()


def test_rows_without_a_date_still_picks_the_latest_partition(tmp_path: Path) -> None:
    data_dir = _layout(tmp_path)

    df, active = _read_ext_dataframe(_cfg(), data_dir, None)

    assert active == "2026-09-11"
    assert df.get_column("symbol").to_list() == ["000001.SZ"]

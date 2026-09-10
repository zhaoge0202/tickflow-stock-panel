"""CSV 编码转换的换行保真回归测试。

ensure_utf8_csv 把 GBK 系 CSV 转成 UTF-8 再交给 Polars。转换本身用
`write_text`，默认换行转换在 Windows 上把文本里的 \n 写成 \r\n；源文件本来
就是 CRLF 时结果是 \r\r\n。多出来的 \r 会落在最后一列，使列名带上 \r、每行
的值也带上 \r，该列于是从数值被推断成字符串。

而本函数针对的正是同花顺 / 东财 / 通达信和 Windows 中文 Excel 导出的文件，
这些工具导出的就是 CRLF，所以这条路径上的文件基本都会命中。

判据取“同一份内容的 UTF-8 版本”：它不经过转换，是这份数据本该被解析成的
样子，转换后的 GBK 版本必须与它逐列一致。纯本地文件操作，不需要数据源。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from app.services.ext_data import ensure_utf8_csv

# 名称/代码/收盘价三列，CRLF 换行，最后一列是数值——受损时最容易看出来。
CSV_TEXT = "名称,代码,收盘价\r\n浦发银行,600000,12.34\r\n招商银行,600036,45.67\r\n"


def _read(path: Path) -> pl.DataFrame:
    return pl.read_csv(ensure_utf8_csv(path), infer_schema_length=10000)


def test_gbk_crlf_csv_parses_the_same_as_its_utf8_twin(tmp_path: Path) -> None:
    gbk = tmp_path / "gbk.csv"
    gbk.write_bytes(CSV_TEXT.encode("gb18030"))
    utf8 = tmp_path / "utf8.csv"
    utf8.write_bytes(CSV_TEXT.encode("utf-8"))

    converted = _read(gbk)
    untouched = _read(utf8)

    # UTF-8 那份原样返回，不经过转换，所以它就是判据。
    assert converted.columns == untouched.columns
    assert converted.dtypes == untouched.dtypes
    assert converted.to_dicts() == untouched.to_dicts()


def test_gbk_crlf_csv_keeps_the_last_column_numeric(tmp_path: Path) -> None:
    path = tmp_path / "gbk.csv"
    path.write_bytes(CSV_TEXT.encode("gb18030"))

    df = _read(path)

    # 列名不带 \r，最后一列仍是数值——修复前分别是 "收盘价\r" 和 String。
    assert df.columns == ["名称", "代码", "收盘价"]
    assert df["收盘价"].dtype == pl.Float64
    assert df["收盘价"].to_list() == [12.34, 45.67]


def test_transcoded_file_has_no_extra_carriage_return(tmp_path: Path) -> None:
    path = tmp_path / "gbk.csv"
    path.write_bytes(CSV_TEXT.encode("gb18030"))

    out = ensure_utf8_csv(path)

    assert out != path  # 确实走了转换分支
    assert out.read_bytes().count(b"\r") == path.read_bytes().count(b"\r")


def test_lf_only_source_stays_lf(tmp_path: Path) -> None:
    # 反向保护：源文件是 LF 时不能被转换成 CRLF。
    lf_text = CSV_TEXT.replace("\r\n", "\n")
    path = tmp_path / "gbk_lf.csv"
    path.write_bytes(lf_text.encode("gb18030"))

    out = ensure_utf8_csv(path)

    assert out.read_bytes().count(b"\r") == 0
    assert _read(path)["收盘价"].dtype == pl.Float64


def test_utf8_source_is_returned_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "utf8.csv"
    path.write_bytes(CSV_TEXT.encode("utf-8"))

    assert ensure_utf8_csv(path) == path

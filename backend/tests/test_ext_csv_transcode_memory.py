"""CSV 编码转换的内存占用回归测试。

上传路径专门用 `_write_upload_capped` 分块落盘（issue #204），docstring 写明
是为了「避免 `await file.read()` 把整个文件读入内存(大文件可能触发高内存占用、
进程 OOM 或服务不可用)」。但紧接着的 `ensure_utf8_csv` 曾用 `read_bytes()`
把同一个文件整个读回内存再整体解码，把这层保护抵消掉：50MB 上限的文件实测
峰值 302MB（6.05×），解码出的 str 比原字节还大。

这里用 tracemalloc 量峰值，判据是「与文件大小无关」而不是某个绝对值：转换
按块进行时峰值只跟块大小有关，文件翻倍不会让峰值翻倍。
"""
from __future__ import annotations

import tracemalloc
from pathlib import Path

from app.services.ext_data import ensure_utf8_csv

_ROW = "浦发银行,600000,12.34,上海证券交易所\r\n"
_HEADER = "名称,代码,收盘价,交易所\r\n"


def _gbk_csv(path: Path, size_bytes: int) -> Path:
    body = _ROW * (size_bytes // len(_ROW.encode("gb18030")))
    path.write_bytes((_HEADER + body).encode("gb18030"))
    return path


def _peak_bytes(path: Path) -> int:
    tracemalloc.start()
    try:
        ensure_utf8_csv(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def test_transcode_peak_memory_is_far_below_the_file(tmp_path: Path) -> None:
    path = _gbk_csv(tmp_path / "big.csv", 8 * 1024 * 1024)

    peak = _peak_bytes(path)

    # 分块转换时峰值只跟块大小有关。修复前是文件的 6 倍。
    assert peak < path.stat().st_size


def test_transcode_peak_memory_does_not_grow_with_the_file(tmp_path: Path) -> None:
    small = _peak_bytes(_gbk_csv(tmp_path / "small.csv", 2 * 1024 * 1024))
    large = _peak_bytes(_gbk_csv(tmp_path / "large.csv", 8 * 1024 * 1024))

    # 文件大 4 倍，峰值不应跟着涨：留一倍余量给解释器噪声。
    assert large < small * 2


def test_multibyte_character_on_a_chunk_boundary_survives(tmp_path: Path) -> None:
    # GBK 一个汉字两字节，分块时可能正好被切开。增量解码器负责把半个字符
    # 留到下一块；若改成逐块独立 decode，这里会解码失败并整份回退。
    import polars as pl

    from app.services.ext_data import _TRANSCODE_CHUNK_BYTES

    filler = "浦发银行" * ((_TRANSCODE_CHUNK_BYTES // 8) + 1)
    text = f"名称,备注\r\n浦发银行,{filler}\r\n"
    path = tmp_path / "boundary.csv"
    path.write_bytes(text.encode("gb18030"))
    assert path.stat().st_size > _TRANSCODE_CHUNK_BYTES

    df = pl.read_csv(ensure_utf8_csv(path), infer_schema_length=10000)

    assert df.columns == ["名称", "备注"]
    assert df["备注"][0] == filler

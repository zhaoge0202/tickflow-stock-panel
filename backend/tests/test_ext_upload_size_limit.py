"""扩展数据上传体积上限回归测试(issue #204)。

_write_upload_capped 分块把上传写入临时文件, 累计超过上限即拒绝(413),
避免 `await file.read()` 把整个文件读入内存。纯逻辑, 不需真实数据源。
"""
from __future__ import annotations

import io

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app.api.ext_data import _write_upload_capped


def _upload(data: bytes) -> UploadFile:
    return UploadFile(io.BytesIO(data), filename="x.csv")


async def test_under_limit_writes_full_content(tmp_path):
    data = b"code,close\n000001.SZ,10.5\n600000.SH,8.2\n"
    dest = tmp_path / "upload.csv"
    await _write_upload_capped(_upload(data), dest, max_bytes=1024)
    assert dest.read_bytes() == data


async def test_at_limit_is_allowed(tmp_path):
    data = b"x" * 64
    dest = tmp_path / "upload.csv"
    await _write_upload_capped(_upload(data), dest, max_bytes=64)
    assert dest.read_bytes() == data


async def test_over_limit_raises_413(tmp_path):
    data = b"x" * 200
    dest = tmp_path / "upload.csv"
    with pytest.raises(HTTPException) as exc:
        await _write_upload_capped(_upload(data), dest, max_bytes=64)
    assert exc.value.status_code == 413
    assert "过大" in str(exc.value.detail)

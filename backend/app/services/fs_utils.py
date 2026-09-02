"""文件系统小工具 — 原子写等。

历史遗留: json_report_store / strategy_cache / kline_sync 等模块里各有一份内联的
同款原子写。新代码统一用本模块的 atomic_write_text, 一处实现一处维护。
"""
from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """临时文件 + os.replace 原子替换, 避免读侧读到半截 JSON。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

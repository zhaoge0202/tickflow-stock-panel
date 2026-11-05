"""文本 IO 编码/换行回归 — 针对 cp936 (Windows 中文默认码) 环境。

背景 (2026-11-04 Windows 全量测试):
  1. `test_paper_execution_extension` 26 例全红: 用例用 `write_text()` 不指定 encoding
     写含中文的 demo.py, cp936 机器上落盘是 GBK 字节, 而策略引擎按 UTF-8 读 →
     "load strategy demo.py failed: 'utf-8' codec can't decode byte 0xc0" → unknown strategy。
  2. `test_py_file_import_through_resource_api`: 资源导入把 payload 解码后按文本写回,
     Windows 的 `Path.write_text()` 默认把 "\n" 译成 "\r\n", 于是原本 CRLF 的 .py
     变成 "\r\r\n", 导入结果与导出源不再字节一致。

这里把两条契约钉死: 产品代码的文本 IO 必须显式 UTF-8; 文本写盘不得做换行翻译。
两条断言与平台无关 (用 io.text_encoding 打桩模拟 cp936 默认码), 因此在 Linux CI
上也会失败, 不依赖"必须跑在中文 Windows 上"。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from app.services import config_backup
from app.services.fs_utils import atomic_write_text

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _call_args(source: str, start: int) -> str | None:
    """从 `start` (左括号后一位) 起取到配对右括号为止的参数文本。"""
    depth = 1
    for index in range(start, len(source)):
        char = source[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return source[start:index]
    return None


def _text_io_without_encoding() -> list[str]:
    """扫描 app/: read_text/write_text/文本模式 open 缺 encoding 的调用点。"""
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\.(read_text|write_text)\(|(?<![\w.])open\(", source):
            args = _call_args(source, match.end())
            if args is None or "encoding=" in args:
                continue
            if re.fullmatch(r"\s*[rbuRBU]*(['\"]).{0,20}\1\s*", args, re.S):
                continue  # 位置参数传 encoding (只认短字面量; 多行三引号是内容)
            if match.group(0).endswith("open("):
                # 只在参数里出现字面量时按文件 IO 处理: `open(0)` 这类同名遮蔽不算
                if '"' not in args and "'" not in args:
                    continue
                if re.search(r"['\"][rwxab+]*b[rwxab+]*['\"]", args):
                    continue  # 二进制模式 ("rb"/"r+b"/"wb"...), 不需要 encoding
            line = source.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(APP_DIR.parent)}:{line}: {match.group(0)}{args[:60]}")
    return offenders


def test_app_text_io_always_specifies_encoding():
    """产品代码任何文本读写都必须显式 encoding (默认码在中文 Windows 上是 cp936)。"""
    offenders = _text_io_without_encoding()
    assert not offenders, "以下文本 IO 未指定 encoding:\n" + "\n".join(offenders)


@pytest.fixture
def gbk_default_encoding(monkeypatch):
    """把 CPython 的默认文本编码模拟成 cp936 (io.text_encoding 是唯一入口)。"""
    real = io.text_encoding

    def fake(encoding=None, stacklevel=2):
        return real(encoding, stacklevel + 1) if encoding else "gbk"

    monkeypatch.setattr(io, "text_encoding", fake)
    return fake


def test_atomic_write_text_stays_utf8_under_gbk_default(tmp_path, gbk_default_encoding):
    """默认码被模拟成 GBK 时, 原子写仍必须落 UTF-8 字节 (否则中文配置文件全乱码)。"""
    target = tmp_path / "override.json"
    atomic_write_text(target, '{"label": "依赖因子"}')

    assert target.read_bytes().decode("utf-8") == '{"label": "依赖因子"}'
    assert "依赖因子" in target.read_text(encoding="utf-8")


def test_write_payload_keeps_bytes_and_crlf(tmp_path):
    """资源导入写盘不得翻译换行: CRLF 的 .py 导入后必须与导出源字节一致。

    回归点: 曾经 payload 解码后走 write_text, Windows 上 CRLF → "\\r\\r\\n"。
    """
    body = 'META = {"id": "demo"}\r\ndef filter(df, params):\r\n    return df\r\n'
    target = tmp_path / "strategies" / "custom" / "demo.py"

    config_backup._write_payload(target, body.encode("utf-8"))

    assert target.read_bytes() == body.encode("utf-8")
    assert b"\r\r\n" not in target.read_bytes()


def test_write_payload_reads_back_chinese_json(tmp_path, gbk_default_encoding):
    """GBK 默认码下, 含中文的 json 资源写入再读回必须完全一致。"""
    import json

    payload = {"label": "依赖因子", "formula": "rank(close)"}
    target = tmp_path / "user_data" / "custom_factors" / "uf_dep.json"

    config_backup._write_payload(target, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    assert json.loads(target.read_text(encoding="utf-8")) == payload
    # 若读侧按 cp936 解 UTF-8 字节, 中文会变乱码 —— 这里用产品同款读取路径复核
    assert json.loads(target.read_bytes().decode("utf-8")) == payload


def test_strategy_loader_reads_utf8_under_gbk_default(tmp_path, gbk_default_encoding):
    """策略引擎必须按 UTF-8 读源码: 中文 META 在 GBK 默认码机器上也要能加载。"""
    from app.strategy.engine import StrategyEngine

    directory = tmp_path / "strategies" / "custom"
    directory.mkdir(parents=True)
    (directory / "demo_utf8.py").write_text(
        'META = {"id": "demo_utf8", "name": "示例策略", "params": [], "scoring": {}, "limit": 1}\n'
        'BASIC_FILTER = {"enabled": False}\n'
        'ENTRY_SIGNALS = ["entry"]\n'
        'EXIT_SIGNALS = ["exit"]\n'
        "def filter(df, params):\n"
        "    return None\n",
        encoding="utf-8",
    )

    engine = StrategyEngine([directory])
    assert engine.has("demo_utf8")


def test_gbk_simulation_is_effective(gbk_default_encoding):
    """自检: 打桩确实把默认文本编码改成了 gbk (否则上面几条断言形同虚设)。"""
    assert io.text_encoding(None) == "gbk"

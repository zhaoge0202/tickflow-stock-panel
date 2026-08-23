"""loader.install_plugin 单元测试 — uv 安装必须显式指定目标环境。

回归场景: dev.ps1 直接跑 backend/.venv/Scripts/python.exe 而不 activate venv,
后端进程的 VIRTUAL_ENV 为空; 此时 `uv pip install -r ...` 不传 --python 会落到
PATH 上的基础解释器 (如 conda base, 常为只读) → 装错环境 + exit 2 (访问拒绝)。
修复: uv 命令显式传 `--python sys.executable` 锁定后端自身 venv, 与 pip 回退
路径 (sys.executable -m pip) 的目标一致。
"""
from __future__ import annotations

import subprocess
import sys

from app.data_providers.custom import loader


def _patch_uv_install(monkeypatch, returncodes):
    """替换 loader.shutil.which 与 subprocess.run, 捕获 uv 命令行。"""
    calls: list[list[str]] = []

    def fake_which(cmd, *_a, **_k):
        return "/fake/uv" if cmd == "uv" else None

    def fake_run(cmd, *_a, **_k):
        calls.append(list(cmd))
        code = returncodes.pop(0) if isinstance(returncodes, list) else returncodes
        return subprocess.CompletedProcess(cmd, code)

    monkeypatch.setattr(loader.shutil, "which", fake_which)
    monkeypatch.setattr(loader.subprocess, "run", fake_run)
    return calls


def _fake_python_plugin(monkeypatch, tmp_path):
    """在临时目录铺设最小 python 插件, 并把 plugins_dir 指过去 (自包含)。"""
    pdir = tmp_path / "baostock"
    pdir.mkdir()
    (pdir / "plugin.yaml").write_text(
        "name: baostock\nruntime: python\nentry: p:provider\n", encoding="utf-8",
    )
    (pdir / "requirements.txt").write_text("baostock\n", encoding="utf-8")
    monkeypatch.setattr(loader, "plugins_dir", lambda: tmp_path)
    return pdir


def test_install_plugin_uv_targets_running_interpreter(monkeypatch, tmp_path):
    """uv 分支必须传 --python sys.executable, 不能留给 uv 自动探测。"""
    _fake_python_plugin(monkeypatch, tmp_path)
    calls = _patch_uv_install(monkeypatch, [0])

    ok, msg = loader.install_plugin("baostock")
    assert ok, msg
    assert calls, "应调用 uv"
    uv_cmd = calls[0]
    assert uv_cmd[0] == "/fake/uv"
    assert "--python" in uv_cmd
    idx = uv_cmd.index("--python")
    assert uv_cmd[idx + 1] == sys.executable


def test_install_plugin_uv_retry_keeps_python_target(monkeypatch, tmp_path):
    """exit 2 → --no-config 重试时也必须保持 --python 目标。"""
    _fake_python_plugin(monkeypatch, tmp_path)
    calls = _patch_uv_install(monkeypatch, [2, 0])

    ok, msg = loader.install_plugin("baostock")
    assert ok, msg
    assert len(calls) == 2, f"应触发一次重试, 实际 {len(calls)} 次"
    for cmd in calls:
        assert "--python" in cmd
        idx = cmd.index("--python")
        assert cmd[idx + 1] == sys.executable


def test_uninstall_plugin_uv_targets_running_interpreter(monkeypatch, tmp_path):
    """uv 卸载同样必须传 --python sys.executable, 否则会动到基础解释器。"""
    calls: list[list[str]] = []

    def fake_which(cmd, *_a, **_k):
        return "/fake/uv" if cmd == "uv" else None

    def fake_run(cmd, *_a, **_k):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    _fake_python_plugin(monkeypatch, tmp_path)
    monkeypatch.setattr(loader.shutil, "which", fake_which)
    monkeypatch.setattr(loader.subprocess, "run", fake_run)

    ok, msg = loader.uninstall_plugin("baostock")
    assert ok, msg
    assert calls, "应调用 uv"
    uv_cmd = calls[0]
    assert uv_cmd[0] == "/fake/uv"
    assert "uninstall" in uv_cmd
    assert "--python" in uv_cmd
    idx = uv_cmd.index("--python")
    assert uv_cmd[idx + 1] == sys.executable

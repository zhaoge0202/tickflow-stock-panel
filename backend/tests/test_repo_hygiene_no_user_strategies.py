"""仓库卫生: 用户策略源码不得入库 (仓库 PUBLIC, 见 CONTRIBUTING §8)。

用户策略源文件只放数据目录 `data/strategies/custom/` (整个 `data/` 不入库),
研究/优化脚本放 `data/local_scripts/`。本用例扫 `backend/app` 与 `backend/scripts`
的**入库** Python 文件, 命中策略模块特征且不在 `_ALLOWLIST` 内即 fail ——
兜住「策略源码又被提交回仓库」这类回归 (仓库是公开的, 一旦入库即已公开)。

判定特征 (命中任一即视为策略模块):
- `"backtest_defaults":` —— 策略 META 专有字段 (写成 dict key; 排除
  `meta["backtest_defaults"]` 这类读取)
- 顶层 `META = {` 定义 —— 策略模块元数据入口
- ≥5 个不同的 `"custom_*"` 策略 ID —— 私有策略清单 (研究/优化脚本的常见形态)

扫描范围取 `git ls-files` 的「入库文件」而非工作区文件: 本地未提交的临时脚本
不应让用例变红; 非 git 检出 (打包产物/导出目录) 无法枚举入库文件, 跳过。
不含 `backend/tests`: 测试夹具与断言里合法出现策略字面量 (public 夹具策略),
本用例只守策略源码进仓库的两条主路径。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("backend/app", "backend/scripts")

# 命中特征但属公共代码/上游模板, 经审查保留。新增条目必须在 PR 里说明理由;
# 用户策略一律放 data/, 不得靠加白名单留下。
_ALLOWLIST = {
    # 上游研究模板: research_only, 参数由调用方传入, 无私有策略逻辑
    "backend/app/strategy/research/factor_rank_research.py",
    # AI 策略门通用 demo: 内嵌的是合成 demo 策略 (ai_demo/filter 恒真), 无私有逻辑
    "backend/scripts/demo_ai_strategy_gate.py",
}

_BACKTEST_DEFAULTS_KEY = re.compile(r'"backtest_defaults"\s*:')
_TOP_LEVEL_META = re.compile(r"(?m)^META\s*=\s*\{(?!\{)")
_CUSTOM_ID = re.compile(r'"custom_[a-z0-9_]+"')
_MIN_DISTINCT_CUSTOM_IDS = 5

# 上游内置策略种子 (feat(migrate): 内置策略按 ID 种入 custom, 25 个官方模板)。
# 它们是随程序分发的公共资产, 与"用户私有策略出库"是两回事, 整目录跳过。
_EXCLUDED_DIR_PREFIXES = ("backend/app/strategy/seeds/",)


def _tracked_python_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--", *SCAN_DIRS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"非 git 检出, 无法枚举入库文件: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.endswith(".py")]


def _strategy_markers(text: str) -> list[str]:
    markers: list[str] = []
    if _BACKTEST_DEFAULTS_KEY.search(text):
        markers.append('"backtest_defaults"')
    if _TOP_LEVEL_META.search(text):
        markers.append("顶层 META = {")
    custom_ids = set(_CUSTOM_ID.findall(text))
    if len(custom_ids) >= _MIN_DISTINCT_CUSTOM_IDS:
        markers.append(f"{len(custom_ids)} 个 custom_* 策略 ID")
    return markers


def test_no_user_strategy_source_committed_under_backend():
    offenders = {
        relative: markers
        for relative in _tracked_python_files()
        if relative not in _ALLOWLIST
        and not relative.startswith(_EXCLUDED_DIR_PREFIXES)
        and (markers := _strategy_markers((REPO_ROOT / relative).read_text(encoding="utf-8")))
    }

    assert not offenders, (
        "发现疑似用户策略源码被提交入库 (仓库 PUBLIC, 已公开无法撤回):\n"
        + "\n".join(
            f"  - {path}: 命中 {', '.join(markers)}" for path, markers in sorted(offenders.items())
        )
        + "\n用户策略请放 data/: 策略文件 data/strategies/custom/, 研究/优化脚本 "
        "data/local_scripts/ (整个 data/ 不入库, 见 CONTRIBUTING §8)。"
    )

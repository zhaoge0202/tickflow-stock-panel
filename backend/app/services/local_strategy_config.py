"""本地策略情报配置加载 (策略 ID / 竞价阈值), 不入 git。

这些值原本硬编码在 auction_preselect / auction_replay / focus_versions 三个
服务文件里, 属于用户私有策略情报, 公开仓库不应携带。现改从
``data/local_config/auction_strategy.json`` 读取 (整个 data/ 已被 .gitignore 忽略)。

缺失或非法时返回空配置: 各服务据此跳过双刃合专属分支, 不影响主流程。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_FILENAME = "auction_strategy.json"


def _config_file(data_dir: Path) -> Path:
    return Path(data_dir) / "local_config" / _CONFIG_FILENAME


def _str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _str_or_none(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _auction_gap(value) -> dict | None:
    """归一化竞价高开阈值块; 缺必要字段时返回 None (调用方跳过该分支)。"""
    if not isinstance(value, dict):
        return None
    strategy_ids = {sid for sid in _str_list(value.get("strategy_ids"))}
    if not strategy_ids:
        return None
    try:
        default_gap_max = float(value.get("default_gap_max"))
        gap_min = float(value.get("gap_min"))
        auction_min_change = float(value.get("auction_min_change"))
    except (TypeError, ValueError):
        return None
    gap_max_by_strategy: dict[str, float] = {}
    raw_map = value.get("gap_max_by_strategy")
    if isinstance(raw_map, dict):
        for sid, raw in raw_map.items():
            try:
                gap_max_by_strategy[str(sid).strip()] = float(raw)
            except (TypeError, ValueError):
                continue
    return {
        "strategy_ids": frozenset(strategy_ids),
        "default_gap_max": default_gap_max,
        "gap_min": gap_min,
        "auction_min_change": auction_min_change,
        "gap_max_by_strategy": gap_max_by_strategy,
    }


def _empty_config() -> dict:
    return {
        "preselect_strategy_ids": [],
        "dynamic_strategy_ids": [],
        "focus_strategy_ids": [],
        "focus_default_strategy_id": None,
        "auction_gap": None,
    }


def load_auction_config(data_dir: Path) -> dict:
    """读取并归一化本地策略情报配置。

    缺失/非法字段一律落到空值, 调用方据此安全降级 (跳过双刃合专属分支)。
    """
    path = _config_file(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_config()
    except (OSError, ValueError) as exc:
        logger.warning("本地策略情报配置读取失败 (%s): %s", path, exc)
        return _empty_config()
    if not isinstance(raw, dict):
        return _empty_config()
    return {
        "preselect_strategy_ids": _str_list(raw.get("preselect_strategy_ids")),
        "dynamic_strategy_ids": _str_list(raw.get("dynamic_strategy_ids")),
        "focus_strategy_ids": _str_list(raw.get("focus_strategy_ids")),
        "focus_default_strategy_id": _str_or_none(raw.get("focus_default_strategy_id")),
        "auction_gap": _auction_gap(raw.get("auction_gap")),
    }


def focus_strategy_ids(data_dir: Path) -> list[str]:
    """Focus 三版本要处理的策略 ID 列表 (本地配置, 缺失时为空)。"""
    return load_auction_config(data_dir)["focus_strategy_ids"]


def focus_default_strategy_id(data_dir: Path) -> str | None:
    """Focus 三版本的默认策略 ID (本地配置, 缺失时为 None)。"""
    return load_auction_config(data_dir)["focus_default_strategy_id"]

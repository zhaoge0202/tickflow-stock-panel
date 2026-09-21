"""Focus 策略三版本出票与状态标记服务。

管理 Focus 策略（及双刃合策略系）在单个交易日内的三种出票形态：
1. 14:50 尾盘初选版 (Preview): 基于截至 14:50 的分时聚合生成，供盘中提前复盘做功课；
2. 15:35 收盘正式版 (Final): 收盘日K定版后的严格选股结果，严格符合历史回测标准；
3. 15:35 次日竞价预选版 (Preselect): 宽基观察池（Top 5），专供次日 09:23~09:25 集合竞价做确认。

并为每个标的计算跨版本状态标记：
- 【初选确认】: 14:50 选出且收盘定版依然保留（经受住尾盘抛压考验，确定性高）
- 【尾盘淘汰】: 14:50 选出但收盘被剔除（附带淘汰原因，如尾盘冲高回落）
- 【尾盘突击】: 14:50 尚未达标、尾盘最后 10 分钟主力突击拉升达标
- 【竞价预选 Top N】: 命中次日竞价前置观察池
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from app.market_time import DAILY_STRATEGY_READY_TIME, INTRADAY_PREVIEW_READY_TIME, cn_now, cn_today
from app.services.auction_preselect import _preselect_rows, _target_frame
from app.services.screener import ScreenerService
from app.strategy import config as strategy_config
from app.strategy.engine import StrategyDataContext, StrategyEngine

logger = logging.getLogger(__name__)
CN_TZ = ZoneInfo("Asia/Shanghai")

FOCUS_STRATEGY_IDS = frozenset({
    "custom_dual_edge",
    "custom_dual_edge_focus",
    "custom_dual_edge_v3",
})


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _sanitize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import math

    safe = []
    for r in rows:
        item = dict(r)
        for k, v in list(item.items()):
            if isinstance(v, float) and not math.isfinite(v):
                item[k] = None
            elif isinstance(v, (date, datetime)):
                item[k] = v.isoformat()
        safe.append(item)
    return safe


def _full_snapshot_path(data_dir: Path, strategy_id: str, as_of: str) -> Path:
    return data_dir / "user_data" / f"focus_three_versions_{strategy_id}_{as_of}.json"


def _preview_storage_path(data_dir: Path, strategy_id: str, as_of: str) -> Path:
    return data_dir / "user_data" / f"preview_{strategy_id}_{as_of}.json"


def save_preview_snapshot(
    data_dir: Path,
    strategy_id: str,
    as_of: str,
    rows: list[dict[str, Any]],
    total: int | None = None,
) -> None:
    """持久化 14:50 尾盘初选快照。"""
    path = _preview_storage_path(data_dir, strategy_id, as_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "strategy_id": strategy_id,
        "as_of": as_of,
        "created_at": int(cn_now().timestamp() * 1000),
        "total": len(rows) if total is None else total,
        "rows": _sanitize_rows(rows),
    }
    path.write_text(json.dumps(payload, default=_json_default, ensure_ascii=False, indent=2), encoding="utf-8")


def load_preview_snapshot(
    data_dir: Path,
    strategy_id: str,
    as_of: str,
) -> dict[str, Any] | None:
    """读取已持久化的 14:50 尾盘初选快照。"""
    path = _preview_storage_path(data_dir, strategy_id, as_of)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("读取 preview snapshot 失败 %s: %s", path, e)
        return None


def generate_preview_1450_from_context(
    base_ctx: StrategyDataContext,
    engine: StrategyEngine,
    strategy_id: str,
    target_date: date,
    data_dir: Path,
    params: dict,
    overrides: dict,
) -> list[dict[str, Any]]:
    """极速生成 14:50 尾盘初选（复用已有的 base_ctx 历史数据，避免重跑指标计算）。"""
    d_str = target_date.isoformat()
    min_file = data_dir / "kline_minute" / f"date={d_str}" / "part.parquet"

    hist_panel = base_ctx.history
    if hist_panel is None or hist_panel.is_empty():
        return []

    hist_prev = hist_panel.filter(pl.col("date") < target_date)
    day_meta = hist_panel.filter(pl.col("date") == target_date)

    if min_file.exists():
        # 扫描并切出 14:50 前的分钟线
        cutoff_dt = f"{d_str} 14:50:00"
        min_cut = (
            pl.scan_parquet(str(min_file))
            .filter(pl.col("datetime") <= pl.lit(cutoff_dt).str.to_datetime())
            .collect()
        )
        if min_cut.is_empty():
            return []

        agg_1450 = (
            min_cut.sort("datetime")
            .group_by("symbol")
            .agg([
                pl.first("open").alias("open"),
                pl.max("high").alias("high"),
                pl.min("low").alias("low"),
                pl.last("close").alias("close"),
                (pl.sum("volume") * (240.0 / 230.0)).alias("volume"),
                (pl.sum("amount") * (240.0 / 230.0)).alias("amount"),
            ])
            .with_columns(pl.lit(target_date).alias("date"))
        )
    else:
        # 实盘盘中路径: 直接复用当前的 current enriched 快照 (即盘中实时计算的 live enriched)
        if base_ctx.current is None or base_ctx.current.is_empty():
            return []
        agg_1450 = base_ctx.current

    meta_cols = [
        c for c in day_meta.columns
        if c not in ["open", "high", "low", "close", "volume", "amount", "date", "symbol"]
    ]
    day_1450 = agg_1450.join(day_meta.select(["symbol"] + meta_cols), on="symbol", how="left")

    # 核心修复: 09:25 集合竞价决定的真实开盘价已在 day_meta/current 中权威定型。
    # 分钟线第一根 bar 常因数据源延迟记录的是 09:30 连续竞价撮合价而非集合竞价价，
    # 导致盘中估算的高开幅度失真。此处强制优先继承 day_meta 的权威 open (及融合 low/high 极值)。
    canonical_bounds = [c for c in ["open", "high", "low"] if c in day_meta.columns]
    if canonical_bounds:
        bound_df = day_meta.select(["symbol"] + canonical_bounds).rename(
            {c: f"_canonical_{c}" for c in canonical_bounds}
        )
        day_1450 = day_1450.join(bound_df, on="symbol", how="left")
        transforms = []
        if "_canonical_open" in day_1450.columns:
            transforms.append(
                pl.when(pl.col("_canonical_open").is_not_null() & (pl.col("_canonical_open") > 0))
                .then(pl.col("_canonical_open"))
                .otherwise(pl.col("open"))
                .alias("open")
            )
        if "_canonical_low" in day_1450.columns:
            transforms.append(
                pl.when(pl.col("_canonical_low").is_not_null() & (pl.col("_canonical_low") > 0))
                .then(pl.min_horizontal([pl.col("_canonical_low"), pl.col("low")]))
                .otherwise(pl.col("low"))
                .alias("low")
            )
        if "_canonical_high" in day_1450.columns:
            transforms.append(
                pl.when(pl.col("_canonical_high").is_not_null() & (pl.col("_canonical_high") > 0))
                .then(pl.max_horizontal([pl.col("_canonical_high"), pl.col("high")]))
                .otherwise(pl.col("high"))
                .alias("high")
            )
        if transforms:
            day_1450 = day_1450.with_columns(transforms)
        day_1450 = day_1450.drop([f"_canonical_{c}" for c in canonical_bounds], strict=False)

    panel_1450 = pl.concat([hist_prev, day_1450], how="diagonal_relaxed").sort(["symbol", "date"])

    ctx_1450 = StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=target_date,
        current=day_1450,
        history=panel_1450,
    )

    res = engine.run(strategy_id, ctx_1450, params=params, overrides=overrides)
    rows = res.rows or []

    # 存入快照
    save_preview_snapshot(data_dir, strategy_id, d_str, rows, total=res.total)
    return rows


def build_focus_three_versions(
    repo,
    engine: StrategyEngine,
    strategy_id: str = "custom_dual_edge_focus",
    as_of: date | None = None,
    preselect_limit: int = 5,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """极速计算并聚合 Focus 策略的三版本出票结果，并注入状态标记。"""
    target_date = as_of or cn_today()
    d_str = target_date.isoformat()
    data_dir = Path(repo.store.data_dir)
    now = cn_now()

    # 1. 缓存秒读: 如果已经完整生成且不是今天未收盘，直接从磁盘读取返回 (<5ms)
    full_cache_path = _full_snapshot_path(data_dir, strategy_id, d_str)
    is_today_unclosed = bool(target_date == now.date() and now.weekday() < 5 and now.time() < dt_time(15, 0))
    if not force_refresh and not is_today_unclosed and full_cache_path.exists():
        try:
            cached_data = json.loads(full_cache_path.read_text(encoding="utf-8"))
            if cached_data.get("as_of") == d_str:
                return cached_data
        except Exception:
            pass

    strategy_def = engine.get(strategy_id)
    strategy_name = (strategy_def.meta.get("name") if strategy_def else None) or strategy_id

    # 1.1 盘中秒读: 若今天未收盘且 14:50 尾盘初选快照已落盘，直接秒返初选版 (<5ms)
    preview_data = load_preview_snapshot(data_dir, strategy_id, d_str)
    if not force_refresh and is_today_unclosed and preview_data is not None:
        raw_p_rows = preview_data.get("rows") or []
        enriched_p_rows = []
        for r in raw_p_rows:
            row = dict(r)
            row["version_tags"] = [{"type": "preview_seen", "label": "初选入选", "color": "orange"}]
            enriched_p_rows.append(row)
        return {
            "as_of": d_str,
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "current_stage": "preview",
            "is_unclosed": True,
            "versions": {
                "preview": {
                    "label": "14:50 尾盘初选",
                    "time": "14:50",
                    "status": "ready",
                    "total": len(enriched_p_rows),
                    "rows": _sanitize_rows(enriched_p_rows),
                },
                "final": {
                    "label": "收盘正式版",
                    "time": "15:35",
                    "status": "unclosed",
                    "total": 0,
                    "rows": [],
                },
                "preselect": {
                    "label": "次日竞价预选",
                    "time": "15:35",
                    "status": "unclosed",
                    "total": 0,
                    "rows": [],
                },
            },
            "dropped_from_preview": [],
            "summary": {
                "preview_total": len(raw_p_rows),
                "final_total": 0,
                "preselect_total": 0,
                "confirmed_count": 0,
                "dropped_count": 0,
                "late_entrant_count": 0,
            },
        }

    overrides = strategy_config.load_override(data_dir, strategy_id)
    params = dict((overrides or {}).get("params") or {})

    svc = ScreenerService(repo, asset_type="stock")

    # 2. 单次构建通用 Context (核心优化：仅此一次，避免多轮 30s 重复计算)
    try:
        base_ctx = svc.build_strategy_context(
            engine,
            target_date,
            [strategy_id],
            timeframe="1d",
            params_map={strategy_id: params},
            overrides_map={strategy_id: overrides},
        )
        # 如果当天已经收盘 (>=15:00) 且盘后管道尚未生成完整日线数据 (base_ctx.current 样本过小)，
        # 自动利用本地完整 kline_minute 聚合出 15:00 全量数据，确保收盘选股完整准确！
        min_file = data_dir / "kline_minute" / f"date={d_str}" / "part.parquet"
        if not is_today_unclosed and (base_ctx.current is None or len(base_ctx.current) < 1000) and min_file.exists():
            df_m = pl.read_parquet(min_file)
            if not df_m.is_empty():
                agg_1500 = (
                    df_m.sort("datetime")
                    .group_by("symbol")
                    .agg([
                        pl.first("open").alias("open"),
                        pl.max("high").alias("high"),
                        pl.min("low").alias("low"),
                        pl.last("close").alias("close"),
                        pl.sum("volume").alias("volume"),
                        pl.sum("amount").alias("amount"),
                    ])
                    .with_columns(pl.lit(target_date).alias("date"))
                )
                if base_ctx.history is not None:
                    hist_prev = base_ctx.history.filter(pl.col("date") < target_date)
                    day_meta = base_ctx.history.filter(pl.col("date") < target_date).unique(subset=["symbol"], keep="last")
                    meta_cols = [c for c in day_meta.columns if c not in ["open", "high", "low", "close", "volume", "amount", "date", "symbol"]]
                    day_1500 = agg_1500.join(day_meta.select(["symbol"] + meta_cols), on="symbol", how="left")
                    panel_1500 = pl.concat([hist_prev, day_1500], how="diagonal_relaxed").sort(["symbol", "date"])
                    base_ctx = StrategyDataContext(
                        asset_type="stock",
                        timeframe="1d",
                        as_of=target_date,
                        current=day_1500,
                        history=panel_1500,
                    )
    except Exception as e:
        logger.warning("build_strategy_context 失败: %s", e)
        base_ctx = None

    # 3. 运行收盘正式版 (Final)
    final_rows: list[dict[str, Any]] = []
    if base_ctx is not None and not is_today_unclosed:
        try:
            res_final = engine.run(strategy_id, base_ctx, params=params, overrides=overrides)
            final_rows = res_final.rows or []
        except Exception as e:
            logger.warning("运行收盘正式版失败 %s on %s: %s", strategy_id, target_date, e)

    # 4. 运行竞价预选版 (Preselect) — 复用同一个 base_ctx
    preselect_rows: list[dict[str, Any]] = []
    if base_ctx is not None and not is_today_unclosed:
        try:
            hist_prev = base_ctx.history.filter(pl.col("date") < target_date) if base_ctx.history is not None else None
            curr = base_ctx.history.filter(pl.col("date") == target_date) if base_ctx.history is not None else base_ctx.current
            target_frame = _target_frame(curr, hist_prev, target_date)
            p_rows = _preselect_rows(
                target_frame,
                strategy_id=strategy_id,
                strategy=strategy_def,
                params=params,
                overrides=overrides,
                limit=preselect_limit,
            )
            preselect_rows = p_rows or []
        except Exception as e:
            logger.warning("运行竞价预选版失败 %s on %s: %s", strategy_id, target_date, e)

    # 5. 运行 14:50 尾盘初选版 (Preview) — 复用同一个 base_ctx
    preview_data = load_preview_snapshot(data_dir, strategy_id, d_str)
    preview_ready = bool(
        target_date < now.date()
        or (target_date == now.date() and now.time() >= INTRADAY_PREVIEW_READY_TIME)
        or preview_data is not None
    )
    if preview_data is not None:
        preview_rows = preview_data.get("rows") or []
    else:
        if preview_ready and base_ctx is not None:
            preview_rows = generate_preview_1450_from_context(
                base_ctx, engine, strategy_id, target_date, data_dir, params, overrides
            )
        else:
            preview_rows = []

    # 6. 交叉比对与打标
    preview_sym_map = {str(r.get("symbol")): r for r in preview_rows if r.get("symbol")}
    final_sym_map = {str(r.get("symbol")): r for r in final_rows if r.get("symbol")}
    preselect_rank_map = {
        str(r.get("symbol")): idx + 1
        for idx, r in enumerate(preselect_rows)
        if r.get("symbol")
    }

    # (A) 丰富 Final 收盘正式版的标签
    enriched_final_rows = []
    for r in final_rows:
        row = dict(r)
        sym = str(row.get("symbol", ""))
        tags = []
        if sym in preview_sym_map:
            tags.append({"type": "confirmed", "label": "初选确认", "color": "green"})
            row["is_confirmed_from_preview"] = True
        else:
            tags.append({"type": "late_entrant", "label": "尾盘突击", "color": "orange"})
            row["is_late_entrant"] = True

        if sym in preselect_rank_map:
            rank = preselect_rank_map[sym]
            tags.append({"type": "preselect", "label": f"竞价预选 Top {rank}", "color": "blue"})
            row["preselect_rank"] = rank

        row["version_tags"] = tags
        enriched_final_rows.append(row)

    # (B) 丰富 Preselect 预选池的标签
    enriched_preselect_rows = []
    for idx, r in enumerate(preselect_rows):
        row = dict(r)
        sym = str(row.get("symbol", ""))
        tags = [
            {"type": "preselect", "label": f"预选第 {idx + 1} 名", "color": "blue"}
        ]
        if sym in final_sym_map:
            tags.append({"type": "final_selected", "label": "收盘正式入选", "color": "green"})
        if sym in preview_sym_map:
            tags.append({"type": "preview_seen", "label": "14:50初选在列", "color": "purple"})
        row["version_tags"] = tags
        enriched_preselect_rows.append(row)

    # (C) 提取 14:50 选出但在收盘被淘汰的标的 (仅在收盘定版后对比计算)
    dropped_rows = []
    if not is_today_unclosed:
        for sym, r_prev in preview_sym_map.items():
            if sym not in final_sym_map:
                c_prev = float(r_prev.get("close") or 0)
                h_prev = float(r_prev.get("high") or 0)
                final_row = None
                if base_ctx is not None and base_ctx.current is not None:
                    f_df = base_ctx.current.filter(pl.col("symbol") == sym)
                    if not f_df.is_empty():
                        final_row = f_df.to_dicts()[0]

                c_final = float(final_row.get("close", c_prev)) if final_row else c_prev
                h_final = float(final_row.get("high", h_prev)) if final_row else h_prev
                is_lu = bool(final_row.get("signal_limit_up", False)) if final_row else False

                reason = "尾盘其他条件过滤淘汰"
                if is_lu:
                    reason = "尾盘偷袭封死涨停板 (策略排他)"
                elif h_final > 0 and (c_final / h_final) < 0.98:
                    reason = f"尾盘冲高回落，上影线超标 (收盘/最高={c_final/h_final:.3f} < 0.98)"
                elif float(r_prev.get("change_pct", 0) or 0) > 0.085:
                    reason = "涨幅过高 (>8.5%超限)"
                elif float(r_prev.get("open_gap", 0) or 0) > 0.035:
                    reason = "早盘高开超标 (>3.5%)"
                else:
                    reason = "竞价高开幅度或分时防守未达标"

                dropped_rows.append({
                    "symbol": sym,
                    "name": r_prev.get("name") or sym,
                    "preview_close": c_prev,
                    "preview_change_pct": r_prev.get("change_pct"),
                    "preview_score": r_prev.get("score"),
                    "reason": reason,
                    "version_tags": [
                        {"type": "dropped", "label": "尾盘变脸淘汰", "color": "red"}
                    ]
                })

    # (D) 丰富 Preview 版自身的标签
    enriched_preview_rows = []
    for r in preview_rows:
        row = dict(r)
        sym = str(row.get("symbol", ""))
        tags = []
        if not is_today_unclosed:
            if sym in final_sym_map:
                tags.append({"type": "confirmed", "label": "最终保留", "color": "green"})
            else:
                tags.append({"type": "dropped", "label": "收盘已淘汰", "color": "red"})
        else:
            tags.append({"type": "preview_seen", "label": "初选入选", "color": "orange"})
        row["version_tags"] = tags
        enriched_preview_rows.append(row)

    # 当前阶段判断
    if is_today_unclosed:
        current_stage = "preview" if preview_ready else "waiting_preview"
    else:
        current_stage = "final"

    result_payload = {
        "as_of": d_str,
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "current_stage": current_stage,
        "is_unclosed": is_today_unclosed,
        "versions": {
            "preview": {
                "label": "14:50 尾盘初选",
                "time": "14:50",
                "status": "ready" if preview_ready else "pending",
                "total": len(enriched_preview_rows),
                "rows": _sanitize_rows(enriched_preview_rows),
            },
            "final": {
                "label": "收盘正式版",
                "time": "15:35",
                "status": "ready" if not is_today_unclosed else "unclosed",
                "total": len(enriched_final_rows),
                "rows": _sanitize_rows(enriched_final_rows),
            },
            "preselect": {
                "label": "次日竞价预选",
                "time": "15:35",
                "status": "ready" if not is_today_unclosed else "unclosed",
                "total": len(enriched_preselect_rows),
                "rows": _sanitize_rows(enriched_preselect_rows),
            },
        },
        "dropped_from_preview": _sanitize_rows(dropped_rows),
        "summary": {
            "preview_total": len(preview_rows),
            "final_total": len(final_rows),
            "preselect_total": len(preselect_rows),
            "confirmed_count": len(set(preview_sym_map.keys()) & set(final_sym_map.keys())) if not is_today_unclosed else 0,
            "dropped_count": len(dropped_rows) if not is_today_unclosed else 0,
            "late_entrant_count": len(set(final_sym_map.keys()) - set(preview_sym_map.keys())) if not is_today_unclosed else 0,
        },
    }

    # 盘后历史结果持久化保存，供后续直接毫秒级秒开
    if not is_today_unclosed:
        try:
            full_cache_path.write_text(
                json.dumps(result_payload, default=_json_default, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("持久化三版本快照失败: %s", e)

    return result_payload

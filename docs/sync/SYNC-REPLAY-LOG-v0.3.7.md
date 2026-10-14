# v0.3.7 同步重放日志（任务板 #3 / 方案 A）

- 分支: `sync/upstream-v0.3.7`（起点 = 上游 tag `v0.3.7` = `ec8307eb`, 2026-10-14 15:11）
- 本地包基线: `c16aef96` → `main`(b7da9e27), 差异 = 220 个文件（98 新增 / 122 修改）
- 上游区间: `c16aef96..v0.3.7` = 1055 提交 / 617 路径（含 26 处重命名、9 处删除）
- 重叠判定: 本地改过 且 上游也改过 = 82 个；其中 6 个是「页面搬家」；余 76 个进 3-way；40 个干净、36 个冲突
- 本文件未提交（untracked），仅供 #4 复审用
- 已归档到 `docs/sync/`（#5 后置修复轮）; 下方「回归后置修复（#5）」为本轮增量

## 批次 a：138 个上游未动文件，内容直放（4 个提交）

### 根目录/脚本/文档/配置（8）
- M .dockerignore
- M .env.example
- M .gitignore
- M docker-compose.yml
- A docs/zhihu/plan/tdx-data-enrichment-design.md
- A packaging/fix_quote_tick_event_ts.py
- A reports/focus_backtest_2026-01-01_to_2026-09-15.docx
- A scripts/audit_remote_data.py

### 后端 app（52）
- A backend/app/api/alert_outcomes.py
- A backend/app/api/decision.py
- M backend/app/api/financials.py
- A backend/app/api/manual_positions.py
- A backend/app/api/market_breadth.py
- A backend/app/api/quote_ticks.py
- A backend/app/api/replay.py
- M backend/app/api/routes.py
- A backend/app/api/sector_flow.py
- A backend/app/api/signal_frame.py
- A backend/app/api/strategy_history.py
- A backend/app/api/strategy_purchase_marks.py
- A backend/app/api/trade_ticks.py
- M backend/app/market_time.py
- M backend/app/parquet.py
- A backend/app/plugins/tdxapi/__init__.py
- A backend/app/plugins/tdxapi/plugin.yaml
- A backend/app/plugins/tdxapi/provider.py
- A backend/app/services/alert_outcome.py
- A backend/app/services/auction_confirmation.py
- A backend/app/services/auction_preselect.py
- A backend/app/services/auction_replay.py
- A backend/app/services/decision_journal.py
- A backend/app/services/decision_queue.py
- A backend/app/services/equity_premium.py
- M backend/app/services/financial_analyzer.py
- A backend/app/services/focus_versions.py
- M backend/app/services/fs_utils.py
- M backend/app/services/instrument_sync.py
- A backend/app/services/intraday_replay.py
- A backend/app/services/manual_positions.py
- A backend/app/services/market_breadth.py
- M backend/app/services/market_recap.py
- M backend/app/services/minute_refresh.py
- A backend/app/services/quote_snapshot_ingest.py
- A backend/app/services/quote_snapshot_mysql.py
- A backend/app/services/quote_tick_backfill.py
- A backend/app/services/quote_tick_store.py
- A backend/app/services/sector_flow.py
- M backend/app/services/sector_monitor.py
- A backend/app/services/signal_frame.py
- M backend/app/services/strategy_cache.py
- A backend/app/services/strategy_date.py
- A backend/app/services/strategy_history.py
- A backend/app/services/strategy_purchase_marks.py
- A backend/app/services/symbols.py
- A backend/app/services/trade_tick_ingest.py
- A backend/app/services/trade_tick_mysql.py
- M backend/app/services/webhook_adapter.py
- A backend/app/strategy/builtin/adaptive_relative_momentum.py
- M backend/app/strategy/monitor_rules.py
- M backend/app/strategy/prompts/strategy-builder-step1.md

### 后端其它(脚本/lock)（4）
- A backend/scripts/backfill_auction_results.py
- A backend/scripts/optimize_adaptive_relative_momentum.py
- A backend/scripts/optimize_custom_strategies.py
- A backend/scripts/research_custom_strategies.py

### 后端测试（49）
- M backend/tests/backtest/test_engine_portfolio.py
- M backend/tests/backtest/test_mining_runtime.py
- A backend/tests/test_adaptive_relative_momentum.py
- M backend/tests/test_atomic_parquet_writes.py
- M backend/tests/test_atomic_write_retry.py
- A backend/tests/test_auction_confirmation.py
- A backend/tests/test_auction_replay.py
- M backend/tests/test_backtest_etf.py
- A backend/tests/test_custom_candidate_strategies.py
- A backend/tests/test_custom_dual_edge_auction_result.py
- A backend/tests/test_daily_pipeline.py
- A backend/tests/test_decision_queue.py
- A backend/tests/test_depth_service_tdxapi.py
- A backend/tests/test_equity_premium.py
- A backend/tests/test_financial_custom_provider.py
- A backend/tests/test_focus_versions.py
- A backend/tests/test_index_sync_etf_provider.py
- A backend/tests/test_instrument_sync.py
- A backend/tests/test_kline_daily_live_fill.py
- A backend/tests/test_kline_etf_api.py
- A backend/tests/test_kline_minute_api.py
- M backend/tests/test_live_enriched_metadata.py
- A backend/tests/test_market_breadth.py
- M backend/tests/test_minute_range_api.py
- M backend/tests/test_minute_refresh.py
- A backend/tests/test_quote_service_tdxapi_path.py
- A backend/tests/test_quote_snapshot_ingest.py
- A backend/tests/test_quote_snapshot_mysql.py
- A backend/tests/test_quote_tick_store.py
- A backend/tests/test_realtime_decision_replay.py
- A backend/tests/test_realtime_dedup.py
- A backend/tests/test_realtime_status_optimization.py
- M backend/tests/test_repository_index.py
- A backend/tests/test_rps_rotation.py
- A backend/tests/test_screener_history_cache.py
- A backend/tests/test_screener_preselect.py
- A backend/tests/test_sector_flow_api.py
- A backend/tests/test_sector_flow_replay_api.py
- M backend/tests/test_strategy_cache.py
- A backend/tests/test_strategy_date.py
- A backend/tests/test_strategy_history.py
- M backend/tests/test_strategy_monitor_events.py
- A backend/tests/test_strategy_purchase_marks.py
- A backend/tests/test_symbol_normalization.py
- A backend/tests/test_tdxapi_provider.py
- A backend/tests/test_trade_tick_ingest.py
- A backend/tests/test_trade_tick_mysql.py
- A backend/tests/test_trade_ticks_api.py
- A backend/tests/test_weekend_daily_flush_guard.py

### 前端（25）
- A frontend/pnpm-workspace.yaml
- M frontend/src/components/SealedBadge.tsx
- A frontend/src/components/SectorFlowBubbles.tsx
- A frontend/src/components/SectorFlowTrendChart.tsx
- M frontend/src/components/StockMultiDayIntradayChart.tsx
- A frontend/src/components/StockTradeTicksPanel.tsx
- M frontend/src/components/data/DepthConfigCard.tsx
- A frontend/src/components/data/FocusPipelineHealthCard.tsx
- M frontend/src/components/screener/ScreenerFilter.tsx
- M frontend/src/components/stock-analysis/AnalysisKChart.tsx
- A frontend/src/components/stop-loss/StopLossPiPHost.tsx
- A frontend/src/lib/data-source-utils.ts
- A frontend/src/lib/dynamicStopLoss.ts
- M frontend/src/lib/monitorBadge.ts
- M frontend/src/lib/screener-columns.ts
- A frontend/src/lib/stopLossStore.ts
- A frontend/src/lib/taskNotifications.ts
- M frontend/src/lib/useQuoteStream.ts
- M frontend/src/lib/useSharedMutations.ts
- A frontend/src/pages/Decision.tsx
- A frontend/src/pages/SectorFlow.tsx
- A frontend/src/pages/Trading.tsx
- M frontend/src/pages/backtest/charts/useECharts.ts
- A frontend/tests/quote-stream.test.cjs
- M frontend/tsconfig.json

## 批次 b-1：40 个重叠但 3-way 干净的文件

- Dockerfile
- backend/app/api/data.py
- backend/app/api/intraday.py
- backend/app/api/monitor_rules.py
- backend/app/api/settings.py
- backend/app/backtest/engine.py
- backend/app/backtest/matrix.py
- backend/app/backtest/strategy.py
- backend/app/config.py
- backend/app/enriched_generation.py
- backend/app/services/financial_sync.py
- backend/app/services/market_overview_builder.py
- backend/app/services/preferences.py
- backend/app/strategy/engine.py
- backend/app/strategy/prompts/strategy-guide.md
- backend/pyproject.toml
- backend/tests/backtest/test_dependencies.py
- backend/tests/test_abnormal_moves.py
- backend/tests/test_screener_cache_api.py
- backend/tests/test_screener_etf.py
- backend/tests/test_strategy_realtime_refresh.py
- backend/tests/test_strategy_registry.py
- backend/uv.lock
- docs/configuration.md
- frontend/src/components/AlertToast.tsx
- frontend/src/components/StockIntradayChart.tsx
- frontend/src/components/StockPanel.tsx
- frontend/src/components/monitor/RuleEditor.tsx
- frontend/src/components/screener/ScreenerTable.tsx
- frontend/src/components/screener/StrategyCard.tsx
- frontend/src/components/screener/StrategySettingsDialog.tsx
- frontend/src/lib/intraday-chart.ts
- frontend/src/lib/stock-table.ts
- frontend/src/lib/storage.ts
- frontend/src/lib/useSharedQueries.ts
- frontend/src/pages/Data.tsx
- frontend/src/pages/Indices.tsx
- frontend/src/pages/Watchlist.tsx
- frontend/src/pages/backtest/StrategyBacktest.tsx
- frontend/src/pages/settings/Monitoring.tsx

## 批次 b-2：36 个手工冲突文件（逐文件结论）

- `README.md` — 1 处。保留本地 tdx-api 说明; 删掉上游已移除的 stock-sdk/INCLUDE_STOCKSDK 构建参数说明
- `backend/app/api/indices.py` — 1 处。上游指数日K实时蜡烛注入 + 本地 source 文案 index_daily
- `backend/app/api/kline.py` — 5 处。上游实时蜡烛注入/时区注释 + 本地 normalize_symbol 与批量路径; 另修 _fetch_minute_or_502 补 *, raw_basis 形参(自动合并给调用点传了该参数但签名没有)
- `backend/app/api/pipeline.py` — 2 处。上游 _submit_job 任务化结构 + 本地 include_minute(long_running 计时)
- `backend/app/api/screener.py` — 1 处。上游按日期维度 ext 合并 + 本地 _resolve_screener_date(盘中只允许已完成交易日); 该函数加 getattr 兜底 latest_date()
- `backend/app/api/watchlist.py` — 2 处(导入)。并集去重: UTC/date/datetime/dt_time + CN_TZ/cn_now/cn_today + polars_*; 保留本地 normalize_symbol
- `backend/app/indicators/pipeline.py` — 1 处。import 取并集: date/datetime + timedelta as _timedelta
- `backend/app/jobs/daily_pipeline.py` — 2 处。导入并集 + 保留本地 _daily_batch_end_date; 只留 datetime as _dt 避免重复
- `backend/app/main.py` — 导入与路由注册取并集: 本地 decision/manual_positions/quote_ticks/signal_frame/market_breadth/alert_outcomes/replay/strategy_history/strategy_purchase_marks/trade_ticks + 上游 events/task_registry
- `backend/app/services/depth_service.py` — 3 处。上游 capability_gate(depth5) 生效源口径 + 本地 tdxapi 五档源判定/补救路径全保留
- `backend/app/services/index_sync.py` — 3 处。ETF 日K = 本地 _sanitize_etf_daily 清洗 + 上游「读本地全历史重算 enriched」; 能力门控回到本地位置(TickFlow 分支前), 保住 tdxapi 免 batch 能力
- `backend/app/services/kline_sync.py` — 4 处。除权因子合并统一走上游 _merge_adj_factor_store(按 ex_factor 判定变化); 保留本地 _log_cap_unavailable / SDK deadline 守护 / MinuteFetchError; import 并集
- `backend/app/services/quote_service.py` — 11 处。采上游「监控评估移出取数锁 + _evaluate_lock + 快照返回」结构; 叠加本地 watchlist 模式分发与 _fetch_watchlist_quotes、enriched single-flight、_is_trade_price_record/_realtime_records_frame、索引日K解除 has_asset_rules 门控、_build_quote_extra 去重(保序)
- `backend/app/services/rps_rotation.py` — 2 处。_latest_enriched_date = 上游内存缓存 + 本地「缓存冷 → 仓库最新日 → SQL」兜底; 取涨跌幅走上游 _change_pct_range 并把本地 close 窄读并进去
- `backend/app/services/screener.py` — 1 处。历史缓存淘汰 = 本地 _HISTORY_CACHE_MAX_ENTRIES 上限 + 上游 TTL 两段淘汰; 时间戳仍用 time.monotonic
- `backend/app/strategy/monitor.py` — 1 处。_SIGNAL_CN 去掉 22 条内置信号(上游已把内置信号迁到用户自定义命名, _signal_label 会补)
- `backend/app/tickflow/repository.py` — 3 处。_build_live_agg_from_parquet 用本地 symbols 分批签名并补回上游「多读 60 自然日 + _live_agg_window_start」; get_enriched_history 用上游实现(缓存基建在上游侧仍活跃)
- `backend/tests/backtest/test_matrix_strategy.py` — 2 处。基准集改上游 tests/fixtures/strategies(25 个 matrix 策略)
- `backend/tests/test_enriched_generation.py` — 1 处。上游 #417 两个用例 + 本地 auction_result 用例都留
- `backend/tests/test_watchlist_enriched_join.py` — 2 处。导入并集 + _FakeRepo 形参并集(本地 prev5_volume_avg + 上游 history_span/hist_df/history_raises)
- `frontend/src/components/DimensionMembersDialog.tsx` — 1 处。留上游薄壳(lazy+Suspense); 本地 2 行改动(QK.marketSnapshot() 函数调用)移到新家 DimensionMembersDialogContent.tsx
- `frontend/src/components/EChartsIntraday.tsx` — 3 处。保留本地「点击锁定分钟」(chartClickHandlerRef/lockedInfoIdxRef); 换日/移出取上游 infoIdx=-1 口径
- `frontend/src/components/Layout.tsx` — 1 处。侧栏取上游品牌图标核心 8 项 + 本地追加 决策台(/decision)、板块强度(/sector-flow)
- `frontend/src/components/StockDailyKChart.tsx` — 3 处。上游 ChartPill/showAddedMark + 本地压力支撑位开关(buildKeyPriceLines/showKeyLevelToggle)共存
- `frontend/src/components/StockPreviewDialog.tsx` — 1 处。留上游薄壳; 本地 handleRefresh 增补(kline-minute/trade-ticks/trade-tick-persist-status)移到新家 StockPreviewDialogContent.tsx
- `frontend/src/components/stock-table/primitives.tsx` — 1 处。上游 added_at/pct_since_added + 本地竞价确认 12 个 case 共存
- `frontend/src/lib/api.ts` — 1 处。screenerRunAll 取上游新签名(assetType, timeframe, summary_only), 保留本地 screenerCached; 调用点已同步(Screener.tsx)
- `frontend/src/lib/backtestTask.ts` — 1 处。import 并集(taskNotifications + taskRegistry)
- `frontend/src/lib/optimizerTask.ts` — 1 处。import 并集(taskNotifications + taskRegistry)
- `frontend/src/lib/queryKeys.ts` — 1 处。SSE_INVALIDATE_PREFIXES = 上游列表 + 本地 decision/abnormal/market-breadth/sector-flow
- `frontend/src/lib/signals.ts` — 1 处。取上游: 内置信号清单已迁 /api/custom-signals 用户定义, 前端不再内置(本地 4 条标签随之失效, 见待确认)
- `frontend/src/lib/strategyMonitorEvents.ts` — 1 处。采用本地注释/文案「交易买卖信号」(与下方数组和后端回填的 buy_signal/sell_signal 一致)
- `frontend/src/pages/Dashboard.tsx` — 3 处。采上游 widget 结构(旧单体 ~700 行由上游搬到 widgets.tsx/registry.tsx), 本地 EquityPremium 横幅与其辅助函数接回网格上方
- `frontend/src/pages/Screener.tsx` — 3 处。上游结构/图标 + 本地 stopLoss/focus 版本覆盖逻辑; screenerRunAll 调用点对齐上游签名
- `frontend/src/pages/settings/MenuSettings.tsx` — 1 处。上游核心页清单 + 本地 /sector-flow 一条
- `frontend/src/router.tsx` — 4 处。采上游扩展路由结构(Monitor/ConceptAnalysis/IndustryAnalysis/StockAnalysis/Review/Signals/Lots/LimitUpLadder 已迁 custom/* 扩展, 不再硬编码 import/route/CORE_ROUTE_PATHS); 保留本地独有页面 decision/trading/sector-flow

### 新增/修改的「新家」文件（不在冲突清单里，但属同一批工作）
- `frontend/src/components/DimensionMembersDialogContent.tsx` — 本地 QK.marketSnapshot()/api.marketSnapshot() 函数调用迁入
- `frontend/src/components/StockPreviewDialogContent.tsx` — 本地 handleRefresh 增补迁入
- `frontend/src/components/dashboard/widgets.tsx` — MonitorWidget 新家, 智能轮询迁入
- `frontend/src/components/dashboard/registry.tsx` — KpiCell 文案按本地口径「换手 / 5日放量」

## 批次 c：6 个搬家页面（pages/*.tsx → custom/*/extension.tsx）

- `frontend/src/pages/AbnormalMoves.tsx` → custom/abnormal/extension.tsx; 冲突 3 处: 轮询 = 本地 useSmartPollingInterval + 上游非交易时段降频兜底, 上游 placeholderData 保留
- `frontend/src/pages/ConceptAnalysis.tsx` → custom/concept-analysis/extension.tsx; 干净合并, 本地改动全部搬入
- `frontend/src/pages/Financials.tsx` → custom/financials/extension.tsx; 干净合并
- `frontend/src/pages/IndustryAnalysis.tsx` → custom/industry-analysis/extension.tsx; 干净合并
- `frontend/src/pages/LimitUpLadder.tsx` → custom/limit-ladder/extension.tsx; 干净合并
- `frontend/src/pages/Monitor.tsx` → custom/monitor/extension.tsx; 冲突 2 处: 智能轮询 + 非交易时段降频 + 上游 placeholderData/后台不拉取

## 提交清单（v0.3.7..HEAD）

- 49220149 fix(sync): 补齐三处上游语义被本地实现顶掉的地方(回归测试暴露)
- 45692961 test(sync): 合并冲突测试 + README(4 个文件)
- 31a7d405 feat(sync): 手工合并前端组件/页面冲突(9 个文件)
- c2f20b1c feat(sync): 手工合并后端 API/任务/服务层冲突(9 个文件)
- 007d89e3 fix(sync): 修掉合并引入的两处回归 + 测试断言对齐上游去内置化
- ebd4fe37 feat(sync): 6 个搬家页面移植到 custom/*/extension.tsx + 新家补丁
- a1c01474 feat(sync): 手工合并前端核心冲突(路由/API/queryKeys/侧栏)
- fbef9bfb feat(sync): 手工合并后端核心服务冲突(核心 12 个中的 7 个)
- 30e68e77 feat(sync): 重放与上游 1055 提交无冲突的本地改动(3-way 干净)
- a9377a0f feat(sync): 重放前端模块(v0.3.7 树上, 上游未动文件)
- 08cb4916 test(sync): 重放后端测试(v0.3.7 树上, 上游未动文件)
- bad0453b feat(sync): 重放后端模块(v0.3.7 树上, 上游未动文件)
- 2d0772fc chore(sync): 重放基础配置与运维脚本(v0.3.7 树上, 上游未动文件)

## 验证

- `git show --check` 每个提交: 无 whitespace 告警（仅 tdxapi/__init__.py 保留本地文件自带的行尾空行）
- 后端导入冒烟: `from app.main import app` OK（376 条路由, 本地+上游路由都在）
- 前端类型检查 `npx tsc -b`: 只剩 3 个「node_modules 未装上游新依赖」错误（react-grid-layout / @tanstack/query-sync-storage-persister / @tanstack/react-query-persist-client），需一次 pnpm install; 代码级 0 类型错误
- 后端全量 pytest 三方对比（同一 venv / 同一批测试）:
  - 合并树: 15 failed / 其余通过
  - main（临时 worktree）: 23 failed, 其中 12 个与合并树相同 = 既有问题
  - 纯 v0.3.7（临时 worktree）: test_openapi_contract、test_tdxquant_probe 同样红 = 上游自带/环境问题
  - 结论: 合并树没有「只属于本次合并」的失败; 过程中发现并修掉的 6 组回归见两个 fix(sync) 提交

## 待确认清单（给 #4 复审）

- ① api.ts `screenerRunAll` 采用上游签名 (asOf, strategyIds, assetType, timeframe), 本地 ext_columns 实参已删（backend /run_all 三个版本都不读 ext_columns, 行为等价); Screener.tsx 调用点已同步
- ② 去内置化(上游): 25 个公共策略从 `app/strategy/builtin/` 迁到用户数据 `data/strategies/custom/`, 内置目录只剩兼容垫片。本地新增的 `adaptive_relative_momentum.py` 仍在 builtin 目录 → 引擎不再扫描该目录, 属「死文件」(其单测仍绿, 因为直接 import)。需决定: 迁到新机制 or 保留
- ③ 前端内置信号标签: 本地 signals.ts 的 4 条(signal_auction / signal_fenqi / signal_volume_contraction_reversal / signal_oscillation_reversal)与 _SIGNAL_CN 的 22 条内置信号在合并后都不再兜底, 中文名全靠 /api/custom-signals 用户定义; 若老 data 目录未迁移这些定义, 告警文案会显示原始列名
- ④ 侧栏/菜单: /decision、/sector-flow 未加入上游基准 `lib/navOrder.DEFAULT_NAV_PATHS`(该文件有 19 项断言单测), 未保存过排序的环境里这两页会排在已知页之后; ARCH_CLASS 也未登记 → 架构徽章落到默认 ext
- ⑤ 压支开关 a11y: 改用上游 ChartPill 后丢了本地 role=switch / aria-checked（与同排另两个 pill 一致）
- ⑥ EChartsIntraday 口径: 未锁定/移出/换日回到「-1 = 无选中(汇总)」, 本地旧行为是回到最后一分钟; 如需保留旧观感要改回 data.length-1 并同步 infoIdxRef 初值
- ⑦ depth 能力口径: 上游 is_available()(capability_gate) 与本地 capability_status()[available] 仍不同源, 五档切到 fuyao/tdxquant 插件时可能不一致
- ⑧ 索引日K source: 采用上游实时蜡烛注入 + 本地 index_daily 文案（上游原为 index_enriched, 前端仅展示无分支）
- ⑨ 上游自带红用例(v0.3.7 也红, 本轮未动): test_openapi_contract（新增 DELETE /api/paper/account、GET /api/paper/rounds 未更新 Tier A 快照）、test_tdxquant_probe x2（环境缺 tdxaidata）
- ⑩ 既有红用例(main 也红, 非本轮引入): test_kline_minute_api x4、test_quote_service_tdxapi_path x3、test_weekend_daily_flush_guard x2、test_realtime_dedup x1、test_decision_queue x1、test_enriched_incremental_memory x1
- ⑪ 前端 node_modules 需要一次 pnpm install（上游 v0.3.7 新增 react-grid-layout 等 3 个依赖）
- ⑫ 本分支不能直接 merge 回 main: 历史互不相交(main 与 v0.3.7 无共同祖先), 最终落地方式需 PM 决策（建议: 以本分支为新主干, 或把本地增量 squash 再落一次）

## 下一步建议

1. ⭐ 主干 A：#4 先做「#4 清单 + pnpm install + 前端 tsc/build」, 跑通后再放行
2. ⭐ 主干 B：用本分支起一次后端+前端, 走一遍冷启动(首启向导/数据源/实时行情/选股/监控/模拟盘), 重点看 ①②③ 三条去内置化影响
3. 合并回 main 的方式请 PM 决策(第 ⑫ 条); 本轮红线「不合并回 main」已遵守
4. 未摘 GH#475(b22a9700): 本轮没动它, 回归通过后按原计划处理
5. 若要把 /decision、/sector-flow 纳入导航基准与架构分类, 建议单独小 PR(含 navOrder.test.ts 断言同步)

## 回归后置修复（#5, 依据 #4 报告）

| 项 | 结论 |
|---|---|
| M-2 4 条本地信号中文名 | 走「等效迁移」而非种子: `signal_auction` / `signal_fenqi` / `signal_volume_contraction_reversal` / `signal_oscillation_reversal` 由 `custom/*.py` 矩阵策略直接产出(`entry_signal_ids`), 不是 DSL 信号; 种子要求非空 `conditions` 且会被 enriched 管道计算成列, 会被下游 `_build_signal_mask` 读到 ⇒ 有回测行为风险。改为在展示层保留中文名: `backend/app/strategy/monitor.py: _SIGNAL_CN` + `frontend/src/lib/signals.ts: STRATEGY_SIGNAL_LABELS`(用户定义仍优先) |
| M-1 导航沉底/徽章错位 | `lib/navOrder.DEFAULT_NAV_PATHS` 补到 21 项, `/decision` 第 3 位、`/sector-flow` 第 9 位(与迁移前 Layout nav 常量同序); `MenuSettings.ARCH_CLASS` 两条登记为 core; `BUILTIN_PAGES` 补 `/decision` 行; `navOrder.test.ts` 断言同步 |
| L-1 压支 pill a11y | `ChartPill` 增加可选 `role` / `ariaChecked`, 压支 pill 恢复 `role="switch"` + `aria-checked`(另由 `title` 补 `aria-label`) |
| L-2 测试写真实 preferences.json | `test_quote_service_boot_check.py` 加 autouse fixture: `settings.data_dir → tmp_path` + `preferences._invalidate_cache()`; 实证 = 探针目录零文件、真实 `data/user_data/preferences.json` mtime 未变 |
| GH#475 (b22a9700) | 干净摘录(cherry-pick 无冲突), 同一提交; 另有一处本仓适配: `test_strategy_safety_validation.py::test_existing_strategies_in_repo_all_pass` 改为与加载层同口径(同目录 `_*.py` 辅助模块放行), 否则会误判本仓 `_market_regime` / `_auction_result_fields` 的合法导入 |

验证: 前端 `npx tsc -b` 干净 + `npx vitest run` 146 passed(navOrder 8); 后端相关用例全绿
(`test_strategy_safety_validation` 44 / `test_quote_service_boot_check` 4+1xfail / 策略与监控批 80)。
`uv.lock` 按 #4 结论不动; `data/` 未清删; 未合并 main。

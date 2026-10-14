# crypto 线重放日志（任务板 #8：feat/crypto-asset 重放到新 main）

- 重放分支: `feat/crypto-asset`（旧 tip `e62d4be3` = tag `crypto-20261014`；新 tip 见本文件所在提交）
- 新主干: `main` = `f444b49b`（方案 A 同步后的扶正主干）
- 旧参照点: `b7da9e27`（旧 main）。注意 crypto 分支实际分叉自 `e73699e6` = `b7da9e27^`
- 手法: 独立 worktree（不碰 main 工作树 / `data/`）+ `git rebase --onto main b7da9e27`（4 提交逐个 3-way）
- 改动规模: 25 个文件（10 新增 / 15 修改），+2424 / -96

## 0. 差集核对：实际是 25 个文件，不是 27

`git diff b7da9e27 feat/crypto-asset` 会列出 27 个文件，但其中
`backend/app/services/screener.py` 与 `backend/tests/test_screener_history_cache.py`
的差异来自**旧 main 自身的提交**（`fix(选股缓存): 复用大历史窗口避免重复指标计算`），
crypto 4 个提交都没碰过这两个文件 —— 根因是 crypto 分叉自 `b7da9e27^`。
重放到新主干后这两个文件的差异自然消失（新主干已含该提交），符合预期。

## 1. 冲突与处理结论（现树重算，非沿用旧测算）

重放只产生 2 个冲突文件、13 处冲突。

### backend/app/api/kline.py（3 处，另清 1 处重复赋值）

1. **import 块**：crypto 侧旧 import 里有 3 行与新主干重复/子集
   （`compute_enriched`、`market_time`、`price_limits`）。结论：只补入 crypto 真正新增的
   `app.market_rules`（7 个符号），其余一律沿用新主干（含 `compute_enriched_single`）。
2. **`get_daily` 的 end 计算**：crypto 的 `contract_today(CRYPTO, cn_now())` 保留；
   非 crypto 分支改用新主干的 `cn_today()`（crypto 侧旧写法 `date.today()` 已被新主干淘汰，
   新主干是「北京今天」而不是服务器本地日期）。`asset_type = repo.resolve_asset_type(symbol)`
   上移到 end 之前（crypto 侧的结构），原位置的重复赋值由 3-way 自动去掉。
3. **`get_minute` 入口**：新主干在函数入口无条件读
   `request.app.state.capabilities` 与 `minute_adjust.minute_basis_is_raw(repo.store.data_dir)`
   （v0.3.7 分钟原始口径迁移）。结论：保留新主干这两行（新主干行为不丢），
   crypto 早退块位置不变（仍先于 A 股本地优先/实时拉取、`_fetch_minute_or_502`），
   crypto 注释同步改为「不按 capabilities/raw_basis 分支」；crypto 侧遗留的
   A 股分支重复 `capset = ...` 赋值删除（新主干已在入口赋值）。

### frontend/src/components/EChartsIntraday.tsx（10 处）

文件两侧都大改（新主干侧含 v0.3.7 的 `dailySummary`/日K信息条/`no_limit` 退化；crypto 侧含
数据驱动时轴、`priceDigits`、`formatIntradayVolume`、无涨跌停分支）。

| 冲突点 | 结论 |
| --- | --- |
| import / Props / 解构 | 两侧并集：`dailySummary`（新主干）+ `assetType`（crypto）都保留 |
| 涨跌停线门控 | 合并为 `showLimitLines && !isCrypto && !priceLimit?.no_limit`：crypto 无涨跌停价（后端 `price_limit=null`）不画线；新主干针对注册制新股的 `no_limit` 退化处理保留 |
| 自适应/涨跌停按钮门控 | 沿用 crypto 版 `showLimitLines && !isCrypto`（crypto 隐藏），`no_limit` 仍显示按钮（与 A 股一致，不改新主干行为） |
| 自适应 Y 轴 | 采用**新主干**「不做涨跌停带上限钳制」版本（issue: C沈鼓 分时被夹在 ±10%）。crypto 侧旧的 `Math.min(maxDiff, limitDiff)` 钳制对 crypto 本就不生效（`limitLines=false`），crypto 行为不变 |
| 涨跌停模式 maxDiff | 采用新主干 `max(limitDiff, maxDiff, monitoredDiff)` |
| `fullDayToDataIdx` → `axisIdxToDataIdx` | 采用 crypto 的泛化改名（crypto 轴下标=数据下标，A 股=全天 242 槽映射），新主干新增的 else 分支重置行一并改名 |
| 点击选中 | crypto 的 `axisIndexFromValue` + 轴→数据映射，合并保留新主干的 `dataIdx < 0` 守卫 |
| 信息条 | 新主干的 `ohlc` / 日K（前复权）/分时汇总标签 + crypto 的 `priceDigits` / `formatIntradayVolume`（A 股分支输出与原来一致：`toFixed(2)` / `toFixed(0)`） |

### 重放后修复（提交 `bac437db`，与冲突分开、便于 1:1 核对）

`backend/tests/test_kline_crypto_api.py`：新主干 `get_minute` 读
`app.state.capabilities` / `repo.store.data_dir`，桩需跟上（否则 6 条 crypto 用例 AttributeError）；
A 股分支 `price_limit` 响应新增 `no_limit` 字段，断言同步。
桩给不存在的 data_dir = 未迁移口径（`raw_basis=False`），与存量 A 股库一致。

## 2. 验证结果

环境：独立 worktree（无 `data/`、无本地样本），`uv sync --frozen --extra dev`；main 基线与重放结果
在同一形态的工作树里各跑一遍。

| 验证 | 基线（main f444b49b） | 重放结果 | 结论 |
| --- | --- | --- | --- |
| crypto 定向测试 4 文件 | — | `62 passed` | ✅ |
| 后端全量 `pytest tests -q -p no:randomly` | 19 failed / 3006 passed / 7 skipped / 1 xfailed | 19 failed / 3068 passed / 7 skipped / 1 xfailed | ✅ 失败集合逐条相同（comm 差集为空），+62 = crypto 新增用例 |
| 前端 `pnpm vitest run` | 146 passed，3 个文件无用例（collect 失败） | 146 passed，同样 3 个文件 | ✅ 0 新增 |
| 前端 `pnpm build`（含 `tsc -b`） | 成功 | 成功 | ✅ |
| `ruff check`（改动文件） | 共享文件 50 条 | 51 条 | ⚠️ 差 1 条 = crypto 新增代码的 BLE001（见待确认 1） |

注：任务书基线写「13 条既有红」，本次在**无 `data/` 的全新工作树**测得 19 条；多出的 6 条
（`test_quote_service_tdxapi_path` / `test_weekend_daily_flush_guard` / `test_realtime_dedup`
等）在 main 基线上同样失败，属既有红，与本次重放无关。

## 3. 待确认（未硬编，交由合回评审决定）

1. **3 处 `BLE001` 未加 `# noqa: BLE001`**：`app/api/kline.py:563`（`_crypto_prev_close`）、
   `app/services/crypto_sync.py:116`、`app/data_providers/okx/provider.py:324`。
   三处都有日志/错误返回（不是静默吞异常），只是缺 CONTRIBUTING §9 要求的 noqa 标注。
   本次重放未夹带该清理，保持与旧分支 1:1 可核对。
2. **「UTC 合约日」注释遗留**：`app/api/kline.py` 2 处（`_crypto_minute_frame` docstring、
   `get_minute_range` 会话归属）、`app/tickflow/repository.py` 1 处（`append_crypto_daily`）。
   实际口径自 `99946202` 起是**北京自然日**，注释与代码不符。
3. **crypto 并入 A 股自选搜索框**：`frontend/src/pages/Watchlist.tsx` 的
   `SEARCH_ASSET_TYPES` 现为 `stock,etf,index,crypto`（搜索结果带「合约」标签）。
   这是 P2 展示链路的产品口径选择，本次照 crypto 线原样保留。

## 4. 结论

**具备合回 main 的条件**：冲突全部按语义解决（新主干行为 + crypto 定制都在），
定向与全量回归 0 新增失败，前端 build/vitest 与基线一致。
建议合回前顺手处理待确认 1、2（各几行），3 由产品口径确认。

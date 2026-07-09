# TDX 数据补全设计方案

本文是 `realtime-decision-design.md` 的专项补充,目标是把 tdx-api
已经能提供、但当前系统尚未充分使用的数据,有节制地接入到盘中决策台。

设计边界保持不变:

- 不做自动下单。
- 不直连券商交易接口。
- 不把 TDX 当成毫秒级盘口队列或高频撮合源。
- 优先服务 `/decision` 的人工决策、提醒解释和盘中回放。

---

## 0. 实施状态

更新时间: 2026-07-09

本轮已完成:

- [x] 第一批: 盘口事实层。
  - `TDXAPIProvider._quote_row()` 已输出完整五档、内外盘、现量、涨速和轻量衍生指标。
  - `quote_tick_store._normalize_record()` 已把 QuoteTick v2 字段保存为顶层列,并保留
    `bid1` / `ask1` 兼容字段。
  - 已补 `test_tdxapi_provider.py`、`test_quote_tick_store.py` 覆盖。
- [x] 第二批: SignalFrame 盘口信号。
  - 已增加 `microstructure`、五档 `order_book`、盘口不平衡、买卖墙、内外盘、
    涨速、封单强度等解释字段。
  - 已把 `depth_bid_dominant`、`depth_ask_dominant`、`outside_disk_dominant`、
    `wide_spread`、`thin_liquidity`、`tdx_snapshot_stale` 等标签接入
    `active_signals` / `risk_flags` / `reason_text`。
  - 已补 `test_realtime_decision_replay.py`、`test_decision_queue.py` 覆盖。
- [x] 第三批: 决策台 UI。
  - `/decision` 队列卡已展示轻量盘口标签。
  - 详情面板已展示五档盘口、盘口厚度、内外盘、现量和涨速。
  - 盘口字段缺失时展示降级状态。
- [x] 第四批轻量闭环: 市场广度与核心指数环境。
  - `TDXAPIProvider.get_market_breadth()` 已接入 `/api/market-stats`。
  - 已新增 `market_breadth.py` 内存缓存 + `data/market_breadth` Parquet 落盘。
  - 已新增 `/api/market-breadth/latest`。
  - `/decision` 顶部已展示市场温度、上涨/下跌家数和核心指数涨跌幅。
  - `SignalFrame.market_context` 已接入市场温度、涨跌家数、核心指数涨跌幅。
  - 弱市下追涨类信号会追加 `market_headwind` / `market_breadth_weak` 风险标签。
- [x] 第五批轻量闭环: TDX ETF / 核心指数维表补全入口。
  - `TDXAPIProvider.get_instruments("etf")` 已接入 `/api/etf` 和 `/api/etf-codes`。
  - `TDXAPIProvider.get_instruments("index")` 已补核心指数元数据和实时快照。
  - `index_sync` 在启用 `tdxapi` 时会把 ETF / 核心指数写入现有维表存储。
  - `/api/kline/instruments/search` 已支持按需搜索 ETF / 指数,名称批查合并股票、
    ETF 和指数维表。
  - 已决策: ETF / 指数维表不完全切到 TDX;继续以现有维表为主,TDX 作为补充源。
- [x] 第六批轻量闭环: 历史回放补强。
  - `TDXAPIProvider.get_trade_history_full()` 已接入 `/api/trade-history/full`。
  - 回放在本地 `quote_ticks` 与 `/api/minute-trade-all` 均无窗口数据时,会兜底读取
    `tdxapi_trade_history_minute_precision`。
  - 历史分笔仍按分钟精度标注,不伪装成真实秒级 `quote_ticks`。
- [x] 最小可行版本。
  - 已完成“价格 + 盘口解释提醒”的最小闭环。

仍保留为后续观察:

- ETF / 指数维表继续以现有维表为主、TDX 为补充;后续只在现有源不稳定时再评估完全切换。

---

## 1. 背景

当前项目已经接入 `tdxapi` 内置数据源。

主项目中,`backend/app/plugins/tdxapi/provider.py` 声明并实现了四类能力:

```text
daily
minute
realtime
trade_ticks
```

这些能力已经被用于:

- 日 K / 分钟 K 同步与页面查询。
- 实时快照刷新。
- `quote_ticks` 秒级事实层追加。
- `/decision` 决策队列和 `SignalFrame`。
- 分笔成交查询、摘要和可选 MySQL 持久化。

但 tdx-api sidecar 暴露的数据比主系统当前消费的更宽。

当前主要缺口不是“拿不到”,而是:

- Provider 归一化时只保留了标准字段。
- `quote_ticks` 只存一档盘口和少量集合竞价字段。
- `SignalFrame` 还没有系统化使用盘口厚度、内外盘、涨速、市场广度。
- ETF / 指数 / 多周期 K / 交易日 / 市场统计等接口没有成为一等数据契约。

---

## 2. 目标

### 2.1 一句话目标

把 TDX 能稳定提供的盘口与盘中结构数据,转成可解释、可回放、可提醒的
决策信号,而不是把原始接口直接堆到前端。

### 2.2 本阶段要做

- 扩展 TDX 实时快照字段,保留完整五档盘口和盘口衍生指标。
- 扩展 `quote_ticks`,让它能承载盘口、内外盘、现量、涨速等事实。
- 扩展 `SignalFrame`,输出盘口强弱、封单质量、主动买卖强度、集合竞价强度。
- 给 `/decision` 增加可读的盘口解释和风险标签。
- 增加市场广度、指数和 ETF 数据的后续接入计划。
- 明确分阶段实施顺序、验收标准和风险。

### 2.3 本阶段不做

- 不做毫秒级逐笔、逐单队列重建。
- 不做全市场高频盘口存储。
- 不用 sidecar 的批量任务接口替代主系统现有 Parquet 主存储。
- 不把 TDX `trade_ticks` 文案描述成真正秒级逐笔;它当前更接近分钟精度分笔。
- 不引入 Kafka、Redis Stream 等重型链路。

---

## 3. 当前状态盘点

### 3.1 已实际使用

```text
tdx-api sidecar
  → TDXAPIProvider
      → get_daily        → kline_daily
      → get_minute       → kline_minute / 分时图
      → get_realtime     → QuoteService / quote_ticks / Decision
      → get_trade_ticks  → trade_ticks API / SignalFrame 摘要
```

关键现状:

- `tdxapi` 插件只声明 `daily`、`minute`、`realtime`、`trade_ticks`。
- `QuoteService` 仅在实时数据源为 `tdxapi` 时追加 `quote_ticks`。
- `quote_ticks` 当前保留:
  - 最新价、昨收、开高低、成交量、成交额。
  - 买一、卖一、买一量、卖一量。
  - 集合竞价参考价、匹配量、未匹配方向和未匹配量。
- `SignalFrame` 当前主要基于 `quote_ticks` 聚合短周期价格、成交额、关键价位、
  手动持仓和分笔摘要。

### 3.2 已拿到但使用不足

TDX 实时快照里已经包含更多字段:

```text
BuyLevel[0..4]
SellLevel[0..4]
InsideDish
OuterDisc
Intuition
Rate
Active1
Active2
```

当前主系统只使用了:

```text
BuyLevel[0] → bid1 / bid1_vol
SellLevel[0] → ask1 / ask1_vol
BuyLevel[1] / SellLevel[1] → 仅用于集合竞价未匹配量判断
```

因此,买二到买五、卖二到卖五、内盘、外盘、现量、涨速、活跃度都没有进入
一等字段,也没有进入稳定信号。

### 3.3 sidecar 有但主系统未建模

tdx-api sidecar 还提供:

- `/api/index`、`/api/index/all`: 指数 K 线和全量指数历史。
- `/api/market-stats`、`/api/market-count`: 市场统计和证券数量。
- `/api/stock-codes`、`/api/etf-codes`、`/api/etf`: 股票 / ETF 列表。
- `/api/kline-all`: 多周期 K 线,包含分钟、小时、日、周、月、季、年。
- `/api/trade-history`、`/api/trade-history/full`: 历史分笔分页与截断查询。
- `/api/workday`、`/api/workday/range`: 交易日判断和交易日范围。
- `/api/search`、`/api/stock-info`: 搜索和单票综合信息。
- `/api/income`: 按若干交易日偏移计算收益区间。

这些接口有价值,但不应该一次性全部接入。应按 `/decision` 的收益优先级分批。

---

## 4. 数据补全优先级

### P0: 完整五档盘口

最高优先级。

原因:

- 直接服务盘中人工决策。
- 可用于涨停封单、跌停翘板、盘口压力、集合竞价强弱。
- 当前系统已经有买一/卖一和集合竞价字段,扩展成本可控。

需要补齐:

```text
bid1_price / bid1_vol ... bid5_price / bid5_vol
ask1_price / ask1_vol ... ask5_price / ask5_vol
spread
spread_pct
bid_depth_vol
ask_depth_vol
bid_depth_amount
ask_depth_amount
depth_imbalance
best_bid_amount
best_ask_amount
limit_seal_amount
```

### P0: 内外盘、现量、涨速

同样高优先级。

原因:

- 能补足“价格动了,但为什么动”的解释。
- 可作为短周期异动提醒的重要依据。
- 与当前 `SignalFrame` 的 `amount_1m`、`amount_ratio_5m` 互补。

需要补齐:

```text
current_volume
inside_volume
outside_volume
outside_inside_ratio
active_net_volume
speed_rate
active_score
```

其中:

- `current_volume` 来自 `Intuition`。
- `inside_volume` 来自 `InsideDish`。
- `outside_volume` 来自 `OuterDisc`。
- `speed_rate` 来自 `Rate`。
- `active_score` 第一版可由 `Active1/Active2` 透传,不强行解释。

### P1: 市场广度与指数状态

原因:

- 决策台不能只看单票,还要知道市场环境。
- 大盘弱时,同样的突破信号要降级。
- 大盘强时,题材票和 ETF 的提醒可以更积极。

需要补齐:

```text
market_breadth_snapshot
index_state_snapshot
```

来源:

- `/api/market-stats`
- `/api/market-count`
- `/api/index`
- 指数 K 线中的 `UpCount` / `DownCount`

第一版不做复杂市场模型,只输出:

```text
up_count
down_count
flat_count
up_down_ratio
market_temperature
major_index_change_pct
major_index_intraday_trend
```

### P1: ETF 与指数一等维表

原因:

- 当前用户已经有手动 ETF 持仓和 ETF 实时补拉需求。
- ETF 不应该长期靠手动补丁式识别。
- `/decision` 搜索、持仓、提醒都需要 ETF 名称和交易所准确。

需要补齐:

```text
instruments_etf_from_tdx
index_instruments_from_tdx
```

第一版目标:

- 支持 ETF 搜索。
- 支持 ETF 手动持仓名称显示。
- 支持 ETF 实时补拉池。
- 支持核心指数用 TDX 补全。

### P2: 多周期 K 线

原因:

- TDX 支持 `minute5`、`minute15`、`minute30`、`hour`、`week`、`month` 等周期。
- 当前主系统主要消费 1 分钟和日线。
- 多周期可以提升决策解释,但不是最先需要落地的。

建议:

- 第一阶段不额外落库存储多周期原始 K。
- 先在 `SignalFrame` 从 `quote_ticks` 聚合 5s/1m/3m/5m/15m。
- 需要更长窗口时,再按需接 `minute5`、`minute15`、`hour`。

### P2: 历史分笔扩展

原因:

- 当前已有今日 live 和可选 MySQL 持久化。
- `/api/trade-history/full` 可用于历史回放补数据。
- 但 TDX 分笔语义是分钟精度,不能当成真实秒级逐笔。

建议:

- 仅用于回放和摘要。
- 不用于精确排队、撤单或盘口队列判断。
- UI 文案统一使用“分笔成交”或“分钟精度分笔”。

### P3: 搜索、综合信息、交易日、收益区间

这些接口价值中等。

建议:

- `/api/workday/range` 可作为交易日兜底。
- `/api/search` 可作为标的搜索备用源。
- `/api/stock-info` 不建议直接接入主链路,因为它把 quote/kline/minute 打包,
  与主系统现有分层不完全一致。
- `/api/income` 可参考,但系统已有告警收益追踪,不应重复建设。

---

## 5. 目标架构

### 5.1 总体链路

```text
tdx-api sidecar
  → TDXAPIProvider
      → realtime 原始快照
          → QuoteSnapshotNormalizer
              → QuoteTick v2
              → MicrostructureMetrics
              → quote_ticks Parquet + ring buffer
              → SignalFrame
              → DecisionQueue
              → /decision

      → market-stats / index
          → MarketBreadthSnapshot
          → SignalFrame.market_context
          → /decision 顶部市场环境

      → etf-codes / index/all
          → instruments_etf / instruments_index 补全
          → 搜索 / 手动持仓 / 实时补拉池
```

### 5.2 模块边界

```text
backend/app/plugins/tdxapi/provider.py
  负责从 sidecar 拉数据,并做轻量字段归一化。

backend/app/services/quote_tick_store.py
  负责保存事实层,不负责复杂信号判断。

backend/app/services/signal_frame.py
  负责把 QuoteTick / 分笔 / 持仓 / 关键价位 / 市场环境合成解释快照。

backend/app/services/decision_queue.py
  负责把提醒、持仓和 SignalFrame 合并成用户可处理的队列。

frontend/src/pages/Decision.tsx
  负责展示队列、详情、盘口解释和人工处理动作。
```

原则:

- Provider 不直接写业务判断。
- Store 不直接生成买卖建议。
- SignalFrame 只输出解释和风险标签,不下单。
- DecisionQueue 只排序和聚合,不重新计算底层盘口指标。

---

## 6. 数据契约设计

### 6.1 QuoteTick v2

在现有 `quote_ticks` 基础上扩展字段。

基础字段继续保留:

```text
symbol
name
source
event_ts
ingest_ts
trade_date
hour
last_price
prev_close
open
high
low
volume
amount
market_phase
price_type
raw
```

新增盘口字段:

```text
bid1_price bid1_vol
bid2_price bid2_vol
bid3_price bid3_vol
bid4_price bid4_vol
bid5_price bid5_vol

ask1_price ask1_vol
ask2_price ask2_vol
ask3_price ask3_vol
ask4_price ask4_vol
ask5_price ask5_vol
```

兼容字段:

```text
bid1
ask1
bid1_vol
ask1_vol
```

说明:

- 旧字段 `bid1` / `ask1` 暂时保留,避免破坏已有前端和测试。
- 新字段统一带 `_price`,便于和 `bid1_vol` 成对理解。
- 后续可在迁移完成后再决定是否移除旧别名。

新增盘中特征字段:

```text
current_volume
inside_volume
outside_volume
speed_rate
active1
active2
```

新增衍生字段:

```text
spread
spread_pct
bid_depth_vol
ask_depth_vol
bid_depth_amount
ask_depth_amount
depth_imbalance
outside_inside_ratio
active_net_volume
```

集合竞价字段继续保留:

```text
auction_price
auction_matched_volume
auction_unmatched_side
auction_unmatched_volume
auction_change_pct
```

新增集合竞价衍生字段:

```text
auction_unmatched_ratio
auction_pressure_score
```

### 6.2 MarketBreadthSnapshot

新增轻量事实层,第一版可以只保留内存和当天 JSONL/Parquet。

字段:

```text
event_ts
ingest_ts
source
up_count
down_count
flat_count
total_count
up_down_ratio
market_temperature
major_indices
raw
```

`major_indices` 可以先用 JSON 字段:

```json
[
  {
    "symbol": "000001.SH",
    "name": "上证指数",
    "last_price": 3200.12,
    "change_pct": 0.006,
    "trend_15m": "up"
  }
]
```

### 6.3 SignalFrame 扩展

新增盘口解释字段:

```text
spread_pct
depth_imbalance
bid_depth_amount
ask_depth_amount
seal_strength
sell_wall_distance
buy_wall_distance
outside_inside_ratio
active_net_volume
speed_rate
microstructure_score
```

新增市场环境字段:

```text
market_temperature
market_risk_level
major_index_change_pct
market_context_text
```

新增信号标签:

```text
depth_bid_dominant
depth_ask_dominant
seal_strengthening
seal_weakening
sell_wall_nearby
buy_wall_support
outside_disk_dominant
speed_up
auction_buy_pressure
auction_sell_pressure
market_tailwind
market_headwind
```

新增风险标签:

```text
thin_liquidity
wide_spread
ask_wall_pressure
weak_seal
market_breadth_weak
tdx_snapshot_stale
minute_precision_trade_ticks
```

### 6.4 DecisionItem 展示字段

`DecisionItem` 不需要暴露全部盘口字段。

队列卡片只展示:

```text
盘口: 买盘强 / 卖压重 / 封单强 / 封单弱
资金: 外盘占优 / 内盘占优 / 现量放大
环境: 市场顺风 / 市场逆风
质量: TDX 快照新鲜 / 快照滞后
```

详情面板再展示:

- 五档盘口。
- 盘口厚度和不平衡度。
- 内外盘与现量。
- 分笔摘要。
- 5s/1m/3m/5m/15m 聚合。
- 集合竞价解释。

---

## 7. 信号设计

### 7.1 盘口不平衡

输入:

```text
bid_depth_amount
ask_depth_amount
```

计算:

```text
depth_imbalance =
  (bid_depth_amount - ask_depth_amount)
  / max(bid_depth_amount + ask_depth_amount, epsilon)
```

解释:

- `> 0.35`: 买盘明显强。
- `< -0.35`: 卖压明显强。
- 绝对值过小: 盘口均衡,不单独触发。

### 7.2 封单强度

输入:

```text
last_price
limit_up
limit_down
bid1_price / bid1_vol
ask1_price / ask1_vol
amount
float_shares
```

第一版只判断涨停方向:

```text
is_limit_up_area = abs(last_price - limit_up) / limit_up <= 0.001
seal_amount = bid1_price * bid1_vol
seal_amount_ratio = seal_amount / max(amount, epsilon)
```

信号:

- `seal_strengthening`: 封单金额连续增强。
- `seal_weakening`: 封单金额连续下降。
- `weak_seal`: 涨停附近但封单金额不足。

说明:

- 这里不承诺是真实排队量,只是 TDX 快照下系统看到的一档封单事实。
- 午间、集合竞价、停牌等非连续竞价时段要降低或关闭该信号。

### 7.3 买卖墙距离

输入:

```text
bid1..bid5
ask1..ask5
last_price
```

输出:

```text
nearest_buy_wall_price
nearest_buy_wall_distance
nearest_sell_wall_price
nearest_sell_wall_distance
```

规则:

- 单档挂单金额显著高于五档均值时视为墙。
- 卖墙离当前价很近时,追涨提醒降级。
- 买墙离当前价很近时,回踩观察提醒升级。

### 7.4 内外盘强弱

输入:

```text
inside_volume
outside_volume
current_volume
```

输出:

```text
outside_inside_ratio
active_net_volume
```

信号:

- `outside_disk_dominant`: 外盘明显大于内盘。
- `inside_disk_dominant`: 内盘明显大于外盘。
- `current_volume_spike`: 现量相对最近窗口放大。

注意:

- TDX sidecar 文档和源码对内外盘字段有“不一定和东财完全一致”的提示。
- 第一版只把它作为辅助解释,不要单独作为强买卖信号。

### 7.5 涨速与异动

输入:

```text
speed_rate
ret_1m
ret_3m
amount_1m
amount_ratio_5m
```

信号:

- `speed_up`: 涨速走强且短周期成交额放大。
- `speed_up_without_depth`: 价格快速上行但买盘厚度不足,提示追高风险。
- `speed_down`: 下跌加速且内盘占优。

### 7.6 集合竞价压力

沿用当前已识别的:

```text
auction_price
auction_matched_volume
auction_unmatched_side
auction_unmatched_volume
auction_change_pct
```

新增:

```text
auction_unmatched_ratio =
  auction_unmatched_volume
  / max(auction_matched_volume + auction_unmatched_volume, epsilon)
```

信号:

- `auction_buy_pressure`: 未匹配买量占比较高。
- `auction_sell_pressure`: 未匹配卖量占比较高。
- `auction_strength`: 参考价涨幅和匹配量同时较强。

---

## 8. 后端实施计划

### 阶段一: 实时快照字段补齐

目标:

- 不改变用户工作流。
- 先让 TDX 快照的原始能力进入标准事实层。

改动:

1. 扩展 `TDXAPIProvider._quote_row()`。
2. 新增 `_level_fields()` 替代当前只取一档的 `_best_level_fields()`。
3. 透传 `InsideDish`、`OuterDisc`、`Intuition`、`Rate`、`Active1`、`Active2`。
4. 在 Provider 侧计算轻量衍生字段:
   - `spread`
   - `spread_pct`
   - `bid_depth_amount`
   - `ask_depth_amount`
   - `depth_imbalance`
   - `outside_inside_ratio`
5. 扩展 `quote_tick_store._normalize_record()` 保存新增字段。

验收:

- `tdxapi` 实时记录中能看到完整五档。
- `quote_ticks/latest` 能返回新增字段。
- 旧字段 `bid1` / `ask1` 不破坏。
- 单元测试覆盖缺档、空盘口、集合竞价三类边界。

### 阶段二: SignalFrame 接入盘口信号

目标:

- 让 `/decision` 的解释更像“为什么现在要看这只票”。

改动:

1. 在 `signal_frame.py` 读取新增盘口字段。
2. 新增盘口指标计算函数:
   - `_microstructure_metrics()`
   - `_depth_signals()`
   - `_auction_signals()`
   - `_liquidity_risk_flags()`
3. 更新 `reason_text` 拼接:
   - 放量突破 + 买盘强。
   - 涨停附近 + 封单增强。
   - 接近压力 + 卖墙明显。
   - 涨速快 + 盘口薄。
4. 决策优先级加入 `microstructure_score`,但权重不超过价格/持仓/告警主信号。

验收:

- `signal-frame/detail/{symbol}` 能返回盘口指标。
- `/decision` 队列原因里能出现盘口解释。
- 盘口字段缺失时不报错,只降级为 `unknown`。

### 阶段三: 前端决策台展示

目标:

- 队列卡片只显示关键信息,详情面板显示完整盘口。

改动:

1. `Decision.tsx` 队列卡新增轻量标签:
   - 买盘强
   - 卖压重
   - 封单强
   - 封单弱
   - 外盘占优
   - 快照滞后
2. 详情面板新增盘口区:
   - 五档买卖盘。
   - 盘口厚度。
   - 内外盘。
   - 涨速/现量。
3. 数据质量差时显示明确状态:
   - TDX 未连接。
   - 快照超过 15 秒。
   - 盘口字段缺失。

验收:

- 不增加操作复杂度。
- 文案不诱导自动交易。
- 移动端/窄屏不挤压主决策按钮。

### 阶段四: 市场广度与指数环境

状态: [x] 已完成

目标:

- 让单票提醒知道市场顺风还是逆风。

改动:

1. `TDXAPIProvider` 增加市场统计读取方法。
2. 新增 `market_breadth.py` 内存缓存和 `data/market_breadth` Parquet 落盘。
3. 新增 `/api/market-breadth/latest`。
4. `SignalFrame` 增加 `market_context`。
5. `/decision` 顶部展示市场温度。

验收:

- 能看到上涨/下跌家数。
- 能看到核心指数涨跌幅。
- 市场弱时,追涨类提醒有风险提示。

### 阶段五: ETF / 指数维表补全

目标:

- ETF 和核心指数成为一等标的,不再依赖零散补拉。

改动:

1. 从 `/api/etf-codes`、`/api/etf` 构建 ETF 维表。
2. 从 `/api/index/all` 或既有指数接口补核心指数元数据。
3. 与现有 `instruments_etf`、`instruments_index` 存储兼容。
4. 搜索接口支持股票 + ETF + 指数。
5. 手动持仓和实时补拉池复用该维表。

验收:

- ETF 手动持仓名称准确。
- ETF 搜索可用。
- 核心指数实时补拉不依赖硬编码兜底。

### 阶段六: 历史回放补强

状态: [x] 已完成

目标:

- 用历史分笔和历史 quote_ticks 验证提醒质量。

改动:

1. 对历史日期,优先读本地 `quote_ticks`。
2. 本地窗口缺失且 `/api/minute-trade-all` 也无窗口数据时,兜底用
   `/api/trade-history/full` 补分笔摘要。
3. 回放结果标注数据来源和精度:
   - `quote_ticks`
   - `tdxapi_trade_history_minute_precision`
4. 不把历史分笔伪装成真实秒级行情。

验收:

- 回放页面能区分真实采样 quote_ticks 和事后补的分笔摘要。
- 规则表现汇总中包含数据质量说明。

---

## 9. API 草案

### 9.1 quote_ticks 扩展

现有接口继续保留:

```http
GET /api/quote-ticks/latest?symbols=002491.SZ
GET /api/quote-ticks/bars?symbol=002491.SZ&freq=5s
GET /api/quote-ticks/quality
```

响应新增字段:

```json
{
  "symbol": "002491.SZ",
  "last_price": 10.23,
  "bid1_price": 10.22,
  "bid1_vol": 120000,
  "ask1_price": 10.23,
  "ask1_vol": 90000,
  "bid_depth_amount": 3600000,
  "ask_depth_amount": 2800000,
  "depth_imbalance": 0.125,
  "outside_inside_ratio": 1.42,
  "speed_rate": 0.8
}
```

### 9.2 SignalFrame 扩展

现有接口继续保留:

```http
GET /api/signal-frame/latest?symbols=002491.SZ
GET /api/signal-frame/detail/002491.SZ
```

新增字段:

```json
{
  "microstructure": {
    "spread_pct": 0.0009,
    "depth_imbalance": 0.42,
    "outside_inside_ratio": 1.56,
    "speed_rate": 0.72,
    "seal_strength": 0.31
  },
  "active_signals": [
    "depth_bid_dominant",
    "outside_disk_dominant"
  ],
  "risk_flags": [
    "sell_wall_nearby"
  ],
  "reason_text": "短周期放量上行,买盘厚度占优,但上方卖墙较近"
}
```

### 9.3 市场广度

新增:

```http
GET /api/market-breadth/latest
```

响应:

```json
{
  "source": "tdxapi",
  "event_ts": 1783587600000,
  "up_count": 3200,
  "down_count": 1800,
  "flat_count": 300,
  "up_down_ratio": 1.78,
  "market_temperature": "warm",
  "major_indices": []
}
```

---

## 10. 前端体验设计

### 10.1 队列卡片

队列卡片只显示决策必要信息:

```text
代码 / 名称 / 当前价 / 涨跌幅
触发原因
盘口标签
持仓风险
人工动作按钮
```

盘口标签示例:

```text
买盘强
卖压重
封单增强
外盘占优
涨速快
快照滞后
```

### 10.2 详情面板

详情面板展示更多解释:

```text
价格与关键位
五档盘口
内外盘与现量
分笔摘要
短周期 bars
市场环境
人工处理历史
```

### 10.3 数据质量展示

需要明确提示:

- TDX 未启用。
- sidecar 不可用。
- 快照超过阈值。
- 盘口字段缺失。
- 分笔数据是分钟精度。

这些提示是为了防止用户误把滞后数据当作实时信号。

---

## 11. 存储策略

### 11.1 quote_ticks 字段变宽

扩展五档盘口后,`quote_ticks` 单行会变宽。

控制策略:

- 只对实时监控池、手动持仓、核心指数、必要 ETF 写入。
- 不做全市场盘口高频落盘。
- 保留 ring buffer,减少读 Parquet 频率。
- 盘后可做 compaction,合并小 parquet 文件。

### 11.2 raw 字段策略

`raw` 只用于调试和后续排错。

原则:

- 一等业务字段必须显式列化。
- 不依赖前端解析 `raw`。
- `raw` 可以保留不稳定字段,但不能作为主要契约。

### 11.3 市场广度存储

本轮已采用:

```text
data/market_breadth/date=YYYY-MM-DD/part-*.parquet
```

`market_breadth.py` 仍保留短 TTL 内存缓存,但 API 拉新成功后会同步写入上述
Parquet 分区; sidecar 临时不可用时可回退读取最近落盘快照。

---

## 12. 测试计划

### 12.1 单元测试

新增或扩展:

- `test_tdxapi_provider.py`
  - 五档盘口映射。
  - 内外盘字段映射。
  - 空盘口 / 缺档兼容。
  - 集合竞价字段不回归。
- `test_quote_tick_store.py`
  - QuoteTick v2 字段归一化。
  - 旧字段兼容。
  - Parquet 读写字段缺失兼容。
- `test_realtime_decision_replay.py`
  - SignalFrame 输出盘口信号。
  - 回放时盘口字段缺失能降级。
- `test_decision_queue.py`
  - 盘口信号进入原因和风险标签。

### 12.2 接口验证

本地 sidecar 可用时验证:

```bash
curl http://127.0.0.1:8080/api/health
curl -X POST http://127.0.0.1:8080/api/batch-quote \
  -H 'Content-Type: application/json' \
  -d '{"codes":["002491"]}'
curl http://127.0.0.1:3011/api/quote-ticks/latest?symbols=002491.SZ
curl http://127.0.0.1:3011/api/signal-frame/detail/002491.SZ
```

### 12.3 前端验证

需要用 Playwright 或浏览器验证:

- `/decision` 队列不卡顿。
- 详情面板五档盘口不挤压按钮。
- 数据缺失时展示降级状态。
- 移动宽度下文字不溢出。

---

## 13. 风险与处理

### 13.1 TDX 快照不是毫秒级实时

风险:

- 用户误以为盘口是毫秒级。

处理:

- UI 显示数据新鲜度。
- `SignalFrame` 增加 `tdx_snapshot_stale` 风险标签。
- 文案使用“快照”“分笔”,避免“逐单”“毫秒”。

### 13.2 内外盘语义不稳定

风险:

- `InsideDish` / `OuterDisc` 与其他行情源口径不同。

处理:

- 只作为辅助解释。
- 不作为单独强信号。
- 在测试样本中保留原始值和衍生值。

### 13.3 字段变宽导致存储增加

风险:

- `quote_ticks` 文件变多、变大。

处理:

- 限制写入股票池。
- 控制刷新间隔。
- 后续做盘后 compaction。

### 13.4 ETF / 指数代码归一化错误

风险:

- ETF、指数、股票代码前缀混淆。

处理:

- 维表补全前先统一 symbol normalizer。
- ETF / 指数使用单独 asset_type。
- 手动持仓保存时做二次规范化。

### 13.5 回放数据来源混杂

风险:

- quote_ticks 真实采样和事后 trade_history 补数据混在一起,导致误判。

处理:

- 回放结果必须标注 `tick_source`。
- 分笔历史只做摘要,不还原秒级 quote_ticks。

---

## 14. 分阶段交付清单

### 第一批: 盘口事实层

状态: [x] 已完成

后端:

- [x] 扩展 `TDXAPIProvider._quote_row()`。
- [x] 扩展 `quote_tick_store._normalize_record()`。
- [x] 补单元测试。

验收:

- [x] `/api/quote-ticks/latest` 返回完整五档和内外盘字段。
- [x] `/decision` 现有功能不回归。

### 第二批: SignalFrame 盘口信号

状态: [x] 已完成

后端:

- [x] 增加盘口衍生指标。
- [x] 增加 `active_signals` 和 `risk_flags`。
- [x] 更新 `reason_text`。

验收:

- [x] `/api/signal-frame/detail/{symbol}` 出现盘口解释。
- [x] 决策队列排序不因字段缺失异常。

### 第三批: 决策台 UI

状态: [x] 已完成

前端:

- [x] 队列卡新增轻量标签。
- [x] 详情面板新增五档盘口区域。
- [x] 数据质量状态可见。

验收:

- [x] 桌面和移动宽度下不溢出。
- [x] 人工动作按钮仍然清晰。

### 第四批: 市场广度与 ETF / 指数

状态: [x] 已完成: 现有维表为主,TDX 作为补充源

后端:

- [x] 增加市场广度读取和缓存。
- [x] `market_breadth` 落盘到 `data/market_breadth`。
- [x] `SignalFrame.market_context` 接入市场温度、涨跌家数和核心指数涨跌幅。
- [x] 弱市下追涨类提醒追加风险提示。
- [x] 增加 ETF / 指数维表补全。
- [x] 名称批查合并股票、ETF 和指数维表。

前端:

- [x] `/decision` 顶部展示市场环境。
- [x] ETF 搜索和持仓名称准确。

验收:

- [x] 市场弱时风险提示生效。
- [x] ETF 手动持仓不再依赖裸代码兜底。
- [x] ETF / 指数搜索返回正确名称和资产类型。

### 补充批: 历史回放补强

状态: [x] 已完成

后端:

- [x] `TDXAPIProvider.get_trade_history_full()` 接入 `/api/trade-history/full`。
- [x] 回放在本地 `quote_ticks` 与 `/api/minute-trade-all` 均无窗口数据时,兜底使用
  `tdxapi_trade_history_minute_precision`。
- [x] 历史分笔转换后的回放数据保留分钟精度来源标识。

验收:

- [x] 回放结果能区分 `quote_ticks` 与事后补的分钟精度历史分笔。
- [x] 不把历史分笔伪装成真实秒级行情。

---

## 15. 最小可行版本

如果只做一个最小闭环,建议先做:

1. `TDXAPIProvider` 输出完整五档、内外盘、现量、涨速。
2. `quote_tick_store` 保存这些字段。
3. `SignalFrame` 计算:
   - `depth_imbalance`
   - `outside_inside_ratio`
   - `spread_pct`
   - `speed_rate`
4. `/decision` 详情面板展示五档盘口。
5. 队列卡只增加 2-3 个标签:
   - 买盘强
   - 卖压重
   - 快照滞后

这批完成后,系统就能从“价格提醒”升级到“价格 + 盘口解释提醒”。

---

## 16. 后续决策点

实现第一批后,再决定:

- `market_breadth` 已决策并实现: 以内存短缓存 + `data/market_breadth` Parquet
  落盘并存。
- ETF / 指数维表已决策: 不完全切到 TDX,继续以现有维表为主、TDX 为补充。
- 是否新增单独的 `depth_snapshots` 存储,而不是继续把盘口放在 `quote_ticks`。
- 是否将 5/15/30 分钟 TDX K 线接成独立多周期上下文。

默认建议:

- 第一版先放在 `quote_ticks`。
- 不新增 `depth_snapshots` 表。
- 不全市场写盘口。
- 等 `/decision` 盘口解释稳定后再拆表。

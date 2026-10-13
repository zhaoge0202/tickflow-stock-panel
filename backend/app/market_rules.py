"""资产类别交易规则档案 — A 股专有假设的单一权威。

背景 (CONTRIBUTING §3 / §12): 「资产类型」在本项目里同时承担业务分类与存储路由
两个职责, 而 A 股专有规则长期以常数或 if 的形式散落在 indicators / backtest /
quote_service / market_time / 前端里 —— 交易日历、09:30-15:00 时段、涨跌停、复权、
100 股整手、252 交易日年化、换手率依赖股本、停牌删行。非 A 股资产 (永续合约)
接入后, 这些假设会把永续合约按 A 股口径处理, 产出「页面能渲染但数字全错」的结果。

本模块是这些规则的单一权威: 规则消费点读这里, 不在调用处散落
if asset_type == "crypto"。未知资产类别 fail-closed 抛错, 不静默回退 A 股规则
(repository.resolve_asset_type 的 fail-open 正是要避免的反面教材)。

日归属口径 (永续合约): 采用**北京自然日**, 与项目内分钟时间戳契约 (北京墙钟 naive)
和全站 CN_TZ 基准同一口径, 无需任何时区换算。这不是随意选的 —— 实际使用的 OKX
官方日线就以 UTC+8 零点切日 (实测 1D 的 ts = UTC 16:00), 因此「口径与交易所一致」
在这里恰好等于「与项目契约一致」。

选源约束: 同一资产类别只能有一个日归属口径。币安/Gate 的日线以 UTC 零点切日
(北京 08:00), 若要接入必须先统一口径, 否则同一张 kline_crypto_daily 会混两种
日期口径 (页面能渲染但日期错位, 属静默错误)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import Literal

from app.market_time import CN_TZ, cn_now

CRYPTO = "crypto"
CN_ASSET_TYPES: tuple[str, ...] = ("stock", "etf", "index")

# A 股连续竞价 240 分钟 / 24 小时品种 1440 分钟
_CN_SESSION_MINUTES = 240
_FULL_DAY_MINUTES = 1440


@dataclass(frozen=True)
class MarketRules:
    """一个资产类别的交易规则档案。"""

    asset_type: str
    # 卖出/平仓的交收约束: 1 = 当日买入不可当日卖出 (T+1), 0 = 可当日平仓
    t_plus: int
    # 是否存在交易所涨跌停价 (决定 signal_limit_up/down 是否可算)
    price_limit: bool
    # 是否有除权除息与复权因子 (无复权时跳过 _apply_adj_factor)
    adjust: bool
    # 整手规模 (股/手): 100 表示按手取整; None 表示允许小数仓位
    lot_size: int | None
    # 是否有股本维表 (换手率 = volume * 10000 / float_shares 依赖它)
    share_capital: bool
    # 是否适用停牌过滤 (filter_halt_days 会删除 volume==0 & amount==0 的行)
    halt_filter: bool
    # 是否 24x7 连续交易 (不受交易时段与交易日历门控)
    session_24x7: bool
    # 交易日历口径: cn = A 股交易日探针, none = 无休市概念
    calendar: Literal["cn", "none"]
    # 年化/夏普折算用的每自然年交易日数
    annual_days: int
    # 回测基准标的 (无合适基准时为 None, 不得默默套用 A 股指数)
    default_benchmark: str | None


def _cn_rules(asset_type: str) -> MarketRules:
    return MarketRules(
        asset_type=asset_type,
        t_plus=1,
        price_limit=True,
        adjust=True,
        lot_size=100,
        share_capital=True,
        halt_filter=True,
        session_24x7=False,
        calendar="cn",
        annual_days=252,
        default_benchmark="000001.SH",
    )


def _crypto_rules() -> MarketRules:
    return MarketRules(
        asset_type=CRYPTO,
        t_plus=0,
        price_limit=False,
        adjust=False,
        lot_size=None,
        share_capital=False,
        halt_filter=False,
        session_24x7=True,
        calendar="none",
        annual_days=365,
        default_benchmark=None,
    )


_RULES: dict[str, MarketRules] = {
    **{asset_type: _cn_rules(asset_type) for asset_type in CN_ASSET_TYPES},
    CRYPTO: _crypto_rules(),
}


def get_rules(asset_type: str | None) -> MarketRules:
    """按资产类别取规则档案。

    未知类别抛 ValueError (fail-closed): 静默回退 A 股规则会让新资产吃满
    涨跌停/整手/T+1 等 A 股约束并产出错误结果。空值按 stock 处理 (历史值口径)。
    """
    key = str(asset_type or "stock").strip().lower()
    rules = _RULES.get(key)
    if rules is None:
        raise ValueError(f"unknown asset_type for market rules: {asset_type!r}")
    return rules


def is_crypto(asset_type: str | None) -> bool:
    """是否 24x7 永续合约类资产 (规则档案里唯一非 A 股口径)。"""
    return str(asset_type or "").strip().lower() == CRYPTO


def _as_cn_wallclock(dt: datetime) -> datetime:
    """按项目契约把时间归一到北京墙钟 naive (带 tz 的入参先换算)。"""
    if dt.tzinfo is not None:
        return dt.astimezone(CN_TZ).replace(tzinfo=None)
    return dt


def to_contract_date(dt: datetime, asset_type: str = "stock") -> date:
    """北京墙钟 datetime → 该资产类别的「日」归属 (两类都是北京自然日)。

    函数保留资产类别参数: 日归属是规则档案的一部分, 调用点不该假定与 stock 相同
    (历史上 crypto 曾用过 UTC 零点切日的交易所口径, 见模块 docstring 的选源约束)。
    """
    get_rules(asset_type)  # fail-closed: 未知类别直接抛错, 不静默按北京日算
    return _as_cn_wallclock(dt).date()


def contract_today(asset_type: str = "stock", now: datetime | None = None) -> date:
    """当前所属的「日」归属 (两类资产都是北京自然日, 口径见模块 docstring)。"""
    return to_contract_date(now or cn_now(), asset_type)


def contract_day_start(d: date, asset_type: str = "stock") -> datetime:
    """「日」D 在北京墙钟中的起点 (naive): 北京 00:00。"""
    get_rules(asset_type)
    return datetime.combine(d, dt_time(0, 0))


def contract_day_end(d: date, asset_type: str = "stock") -> datetime:
    """「日」D 在北京墙钟中的终点 (开区间上界, naive): 次日北京 00:00。"""
    return contract_day_start(d + timedelta(days=1), asset_type)


def contract_minutes_per_day(asset_type: str = "stock") -> int:
    """单个「日」的分钟数, 供完整性与进度折算使用。"""
    return _FULL_DAY_MINUTES if get_rules(asset_type).session_24x7 else _CN_SESSION_MINUTES


def is_trading_session(now: datetime | None = None, asset_type: str = "stock") -> bool:
    """当前是否处于该资产类别的连续交易时段 (24x7 品种恒真)。"""
    if get_rules(asset_type).session_24x7:
        return True
    from app.market_time import in_continuous_session

    return in_continuous_session(now)


def is_trading_day_for(asset_type: str = "stock", now: datetime | None = None) -> bool | None:
    """该资产类别当前是否交易日 (24x7 品种恒 True, A 股走交易日探针)。"""
    if get_rules(asset_type).calendar == "none":
        return True
    from app.services import trading_day

    return trading_day.is_trading_day(now)


def elapsed_minutes(now: datetime | None = None, asset_type: str = "stock") -> float:
    """当日已交易分钟数 (量比与进度折算用)。

    A 股复用 market_time 的两段式折算; 24x7 品种按当日已过分钟数,
    并对齐到 A 股的"非交易日视作全天"语义边界之外: 这里返回真实值,
    调用方据 is_trading_session 决定是否使用。
    """
    rules = get_rules(asset_type)
    local = _as_cn_wallclock(now or cn_now())
    if not rules.session_24x7:
        from app.market_time import trading_minutes_elapsed_from_dt

        return trading_minutes_elapsed_from_dt(local)
    start = contract_day_start(to_contract_date(local, asset_type), asset_type)
    minutes = (local - start).total_seconds() / 60.0
    return max(0.0, min(float(_FULL_DAY_MINUTES), minutes))

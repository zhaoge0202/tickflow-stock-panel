import json
from pathlib import Path
import polars as pl

data_dir = Path("E:/stock/data")

print("=== 1. 2026-09-22 kline_daily 采样 ===")
daily_path = data_dir / "kline_daily" / "date=2026-09-22" / "part.parquet"
if daily_path.exists():
    df_daily = pl.read_parquet(daily_path)
    print("行数:", len(df_daily))
    print("前10只股票:", df_daily["symbol"].to_list()[:10])
else:
    print("kline_daily date=2026-09-22 不存在")

print("\n=== 2. 2026-09-22 kline_minute 采样 ===")
min_path = data_dir / "kline_minute" / "date=2026-09-22" / "part.parquet"
if min_path.exists():
    df_min = pl.read_parquet(min_path)
    print("分钟线行数:", len(df_min))
    print("覆盖股票数:", df_min["symbol"].n_unique())
    print("时间范围:", df_min["datetime"].min(), "~", df_min["datetime"].max())
else:
    print("kline_minute date=2026-09-22 不存在")

print("\n=== 3. 2026-09-22 enriched 状态 ===")
enriched_path = data_dir / "kline_daily_enriched" / "date=2026-09-22" / "part.parquet"
if enriched_path.exists():
    df_enr = pl.read_parquet(enriched_path)
    print("enriched行数:", len(df_enr))
    print("enriched字段数:", len(df_enr.columns))
else:
    print("enriched 不存在")

print("\n=== 4. 实时行情与同步配置 (preferences.json) ===")
pref_path = data_dir / "user_data" / "preferences.json"
if pref_path.exists():
    pref = json.loads(pref_path.read_text(encoding="utf-8"))
    print("实时行情开关 (quote_realtime_enabled):", pref.get("quote_realtime_enabled"))
    print("实时数据源 (quote_provider):", pref.get("quote_provider"))
    print("数据源列表:", pref.get("providers", {}))
    print("行情间隔 (quote_interval):", pref.get("quote_interval"))
    print("全量分钟刷新 (minute_refresh_enabled):", pref.get("minute_refresh_enabled"))
    print("自动跑策略 (screener_auto_run):", pref.get("screener_auto_run"))

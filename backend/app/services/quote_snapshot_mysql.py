"""最新行情快照 MySQL 热缓存。

每个 symbol 只保留 event_ts 最大的一条规范化行情记录。该模块只负责连接、
DDL、upsert 和查询，具体写入时机由行情服务集成层决定。
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.trade_tick_mysql import MySQLConfig, parse_mysql_url

CN_TZ = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS quote_latest (
  symbol VARCHAR(32) NOT NULL,
  trade_date DATE NOT NULL,
  event_ts BIGINT NOT NULL COMMENT '行情事件时间, Unix 毫秒',
  source VARCHAR(32) NOT NULL,
  payload JSON NOT NULL COMMENT '完整规范化行情记录',
  ingested_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
    ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (symbol),
  KEY idx_trade_date_event_ts (trade_date, event_ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
""".strip()


# MySQL 按从左到右计算更新表达式，因此 event_ts 必须最后赋值，前面的条件才能和旧值比较。
UPSERT_SQL = """
INSERT INTO quote_latest (
  symbol, trade_date, event_ts, source, payload, ingested_at
) VALUES (
  %(symbol)s, %(trade_date)s, %(event_ts)s, %(source)s, %(payload)s, %(ingested_at)s
)
ON DUPLICATE KEY UPDATE
  trade_date = IF(VALUES(event_ts) >= event_ts, VALUES(trade_date), trade_date),
  source = IF(VALUES(event_ts) >= event_ts, VALUES(source), source),
  payload = IF(VALUES(event_ts) >= event_ts, VALUES(payload), payload),
  ingested_at = IF(VALUES(event_ts) >= event_ts, VALUES(ingested_at), ingested_at),
  event_ts = GREATEST(event_ts, VALUES(event_ts));
""".strip()


class QuoteSnapshotMySQLStore:
    def __init__(self, url: str | None = None) -> None:
        self._url = url

    @property
    def url(self) -> str:
        return self._url if self._url is not None else settings.trade_ticks_mysql_url

    def configured(self) -> bool:
        return bool(self.url.strip())

    def enabled(self) -> bool:
        return bool(settings.quote_snapshot_mysql_enabled and self.configured())

    def config(self) -> MySQLConfig:
        return parse_mysql_url(self.url)

    def connect(self, with_database: bool = True):
        import pymysql

        cfg = self.config()
        kwargs = {
            "host": cfg.host,
            "port": cfg.port,
            "user": cfg.user,
            "password": cfg.password,
            "charset": cfg.charset,
            "autocommit": True,
            "cursorclass": pymysql.cursors.DictCursor,
            "connect_timeout": 2,
            "read_timeout": 5,
            "write_timeout": 5,
        }
        if with_database:
            kwargs["database"] = cfg.database
        return pymysql.connect(**kwargs)

    def ensure_schema(self, create_database: bool = False) -> dict[str, Any]:
        """创建快照表；按需先创建 URL 中指定的数据库。"""
        cfg = self.config()
        if create_database:
            with self.connect(with_database=False) as conn, conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS {_quote_ident(cfg.database)} "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
        with self.connect(with_database=True) as conn, conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        return {"database": cfg.database, "table": "quote_latest"}

    def health(self) -> dict[str, Any]:
        if not self.configured():
            return {
                "configured": False,
                "enabled": False,
                "ok": False,
                "table_ready": False,
                "message": "未配置 MySQL URL",
            }
        try:
            with self.connect(with_database=True) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_name = 'quote_latest'"
                )
                row = cur.fetchone() or {}
            return {
                "configured": True,
                "enabled": self.enabled(),
                "ok": True,
                "table_ready": int(row.get("n") or 0) > 0,
                "database": self.config().database,
                "table": "quote_latest",
            }
        except Exception as exc:
            return {
                "configured": True,
                "enabled": self.enabled(),
                "ok": False,
                "table_ready": False,
                "database": _safe_database(self.url),
                "table": "quote_latest",
                "message": str(exc),
            }

    def upsert(
        self,
        records: dict[str, Any] | list[dict[str, Any]],
        batch_size: int = 1000,
    ) -> int:
        """写入每个 symbol 的最新快照，返回本轮去重后的 symbol 数。"""
        if isinstance(records, dict):
            records = [records]
        if not records:
            return 0

        latest_by_symbol: dict[str, dict[str, Any]] = {}
        for record in records:
            try:
                row = _to_mysql_row(record)
            except (KeyError, TypeError, ValueError) as exc:
                logger.debug("跳过无效最新行情快照: %s", exc)
                continue
            current = latest_by_symbol.get(row["symbol"])
            if current is None or row["event_ts"] >= current["event_ts"]:
                latest_by_symbol[row["symbol"]] = row

        payload = list(latest_by_symbol.values())
        if not payload:
            return 0
        chunk_size = max(1, int(batch_size or 1000))
        with self.connect(with_database=True) as conn, conn.cursor() as cur:
            for start in range(0, len(payload), chunk_size):
                cur.executemany(UPSERT_SQL, payload[start:start + chunk_size])
        return len(payload)

    def list(
        self,
        symbols: list[str] | None = None,
        trade_date: date | str | None = None,
    ) -> list[dict[str, Any]]:
        """按 symbols 和/或交易日读取最新快照。"""
        normalized_symbols: list[str] | None = None
        if symbols is not None:
            normalized_symbols = sorted({str(symbol).strip().upper() for symbol in symbols if symbol})
            if not normalized_symbols:
                return []

        conditions: list[str] = []
        params: list[Any] = []
        if normalized_symbols is not None:
            placeholders = ", ".join(["%s"] * len(normalized_symbols))
            conditions.append(f"symbol IN ({placeholders})")
            params.extend(normalized_symbols)
        if trade_date is not None:
            conditions.append("trade_date = %s")
            params.append(_date(trade_date))

        where_sql = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            "SELECT symbol, trade_date, event_ts, source, payload "
            f"FROM quote_latest{where_sql} ORDER BY symbol ASC"
        )
        with self.connect(with_database=True) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [_from_mysql_row(row) for row in cur.fetchall()]


def _to_mysql_row(record: dict[str, Any]) -> dict[str, Any]:
    symbol = str(record.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("行情快照 symbol 不能为空")

    event_ts = _event_ts_ms(
        record.get("event_ts") if record.get("event_ts") is not None else record.get("timestamp")
    )
    if event_ts is None:
        raise ValueError(f"行情快照缺少有效 event_ts: {symbol}")

    trade_day = (
        _date(record["trade_date"])
        if record.get("trade_date") is not None
        else datetime.fromtimestamp(event_ts / 1000, tz=CN_TZ).date()
    )
    source = str(record.get("source") or "tdxapi")
    normalized = dict(record)
    normalized.update(
        {
            "symbol": symbol,
            "trade_date": trade_day.isoformat(),
            "event_ts": event_ts,
            "source": source,
        }
    )
    return {
        "symbol": symbol,
        "trade_date": trade_day,
        "event_ts": event_ts,
        "source": source,
        "payload": json.dumps(
            _json_safe(normalized),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ),
        "ingested_at": datetime.now(),
    }


def _from_mysql_row(row: dict[str, Any]) -> dict[str, Any]:
    raw_payload = row.get("payload")
    if isinstance(raw_payload, dict):
        payload = dict(raw_payload)
    elif isinstance(raw_payload, (str, bytes, bytearray)):
        payload = json.loads(raw_payload)
    else:
        payload = {}
    payload.update(
        {
            "symbol": str(row.get("symbol") or "").upper(),
            "trade_date": _date(row.get("trade_date")).isoformat(),
            "event_ts": int(row.get("event_ts") or 0),
            "source": str(row.get("source") or payload.get("source") or ""),
        }
    )
    return payload


def _event_ts_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt_value = value if value.tzinfo else value.replace(tzinfo=CN_TZ)
        return int(dt_value.timestamp() * 1000)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt_value = datetime.fromisoformat(text)
            if dt_value.tzinfo is None:
                dt_value = dt_value.replace(tzinfo=CN_TZ)
            return int(dt_value.timestamp() * 1000)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    if abs(numeric) < 10_000_000_000:
        numeric *= 1000
    return int(numeric)


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise ValueError(f"非法 MySQL 标识符: {name}")
    return f"`{name}`"


def _safe_database(url: str) -> str | None:
    try:
        return parse_mysql_url(url).database
    except Exception:
        return None


quote_snapshot_mysql_store = QuoteSnapshotMySQLStore()

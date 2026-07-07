"""TDX 逐笔成交 MySQL 存储。

该模块只负责 MySQL 连接、DDL 和批量 upsert; 实时拉取与异步调度放在
trade_tick_ingest.py, 避免存储层和行情源耦合。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app.config import settings

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trade_ticks (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  symbol VARCHAR(16) NOT NULL,
  trade_date DATE NOT NULL,
  trade_time DATETIME(3) NOT NULL,
  seq_in_day INT NOT NULL,
  price DECIMAL(12, 4) NOT NULL,
  volume INT NOT NULL COMMENT '成交量, 单位: 手',
  amount DECIMAL(20, 4) NOT NULL COMMENT '成交额, 单位: 元',
  side VARCHAR(16) NOT NULL,
  side_label VARCHAR(16) NOT NULL,
  order_count INT NULL,
  raw_status INT NULL,
  source VARCHAR(32) NOT NULL DEFAULT 'tdxapi',
  ingested_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
    ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_symbol_date_seq (symbol, trade_date, seq_in_day),
  KEY idx_symbol_time (symbol, trade_time),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
""".strip()


UPSERT_SQL = """
INSERT INTO trade_ticks (
  symbol, trade_date, trade_time, seq_in_day,
  price, volume, amount, side, side_label,
  order_count, raw_status, source, ingested_at
) VALUES (
  %(symbol)s, %(trade_date)s, %(trade_time)s, %(seq_in_day)s,
  %(price)s, %(volume)s, %(amount)s, %(side)s, %(side_label)s,
  %(order_count)s, %(raw_status)s, %(source)s, %(ingested_at)s
)
ON DUPLICATE KEY UPDATE
  trade_time = VALUES(trade_time),
  price = VALUES(price),
  volume = VALUES(volume),
  amount = VALUES(amount),
  side = VALUES(side),
  side_label = VALUES(side_label),
  order_count = VALUES(order_count),
  raw_status = VALUES(raw_status),
  source = VALUES(source),
  ingested_at = VALUES(ingested_at),
  updated_at = CURRENT_TIMESTAMP(3);
""".strip()


@dataclass(frozen=True)
class MySQLConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"


class TradeTickMySQLStore:
    def __init__(self, url: str | None = None) -> None:
        self._url = url

    @property
    def url(self) -> str:
        return self._url if self._url is not None else settings.trade_ticks_mysql_url

    def configured(self) -> bool:
        return bool(self.url.strip())

    def enabled(self) -> bool:
        return bool(settings.trade_ticks_persist_enabled and self.configured())

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
        }
        if with_database:
            kwargs["database"] = cfg.database
        return pymysql.connect(**kwargs)

    def ensure_schema(self, create_database: bool = False) -> dict[str, Any]:
        """创建库表。create_database=True 时会先 CREATE DATABASE IF NOT EXISTS。"""
        cfg = self.config()
        if create_database:
            with self.connect(with_database=False) as conn, conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS {_quote_ident(cfg.database)} "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
        with self.connect(with_database=True) as conn, conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        return {"database": cfg.database, "table": "trade_ticks"}

    def health(self) -> dict[str, Any]:
        if not self.configured():
            return {"configured": False, "enabled": False, "ok": False, "message": "未配置 MySQL URL"}
        try:
            with self.connect(with_database=True) as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'trade_ticks'")
                row = cur.fetchone() or {}
                table_ready = int(row.get("n") or 0) > 0
            return {
                "configured": True,
                "enabled": self.enabled(),
                "ok": True,
                "table_ready": table_ready,
                "database": self.config().database,
            }
        except Exception as e:
            return {
                "configured": True,
                "enabled": self.enabled(),
                "ok": False,
                "table_ready": False,
                "database": _safe_database(self.url),
                "message": str(e),
            }

    def upsert_ticks(self, rows: list[dict[str, Any]], batch_size: int = 1000) -> int:
        if not rows:
            return 0
        payload = [_to_mysql_row(row) for row in rows]
        written = 0
        with self.connect(with_database=True) as conn, conn.cursor() as cur:
            for start in range(0, len(payload), batch_size):
                chunk = payload[start:start + batch_size]
                cur.executemany(UPSERT_SQL, chunk)
                written += len(chunk)
        return written

    def list_ticks(
        self,
        symbol: str,
        trade_date: date,
        limit: int = 500,
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 500), 5000))
        direction = "ASC" if order == "asc" else "DESC"
        sql = f"""
            SELECT symbol, trade_date, trade_time, seq_in_day, price, volume, amount,
                   side, side_label, order_count, raw_status, source, ingested_at, updated_at
            FROM trade_ticks
            WHERE symbol = %s AND trade_date = %s
            ORDER BY seq_in_day {direction}
            LIMIT %s
        """
        with self.connect(with_database=True) as conn, conn.cursor() as cur:
            cur.execute(sql, [symbol, trade_date, limit])
            return [_from_mysql_row(row) for row in cur.fetchall()]

    def day_status(self, symbol: str, trade_date: date) -> dict[str, Any]:
        sql = """
            SELECT COUNT(*) AS row_count, MAX(ingested_at) AS last_ingested_at,
                   MAX(updated_at) AS last_updated_at
            FROM trade_ticks
            WHERE symbol = %s AND trade_date = %s
        """
        with self.connect(with_database=True) as conn, conn.cursor() as cur:
            cur.execute(sql, [symbol, trade_date])
            row = cur.fetchone() or {}
        return {
            "symbol": symbol,
            "date": trade_date.isoformat(),
            "rows": int(row.get("row_count") or 0),
            "last_ingested_at": _iso(row.get("last_ingested_at")),
            "last_updated_at": _iso(row.get("last_updated_at")),
        }


def parse_mysql_url(url: str) -> MySQLConfig:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"mysql", "mysql+pymysql"}:
        raise ValueError("TRADE_TICKS_MYSQL_URL 必须以 mysql:// 或 mysql+pymysql:// 开头")
    database = parsed.path.lstrip("/")
    if not database:
        raise ValueError("TRADE_TICKS_MYSQL_URL 必须包含数据库名")
    query = parse_qs(parsed.query)
    charset = (query.get("charset") or ["utf8mb4"])[0]
    return MySQLConfig(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=database,
        charset=charset,
    )


def _quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise ValueError(f"非法 MySQL 标识符: {name}")
    return f"`{name}`"


def _safe_database(url: str) -> str | None:
    try:
        return parse_mysql_url(url).database
    except Exception:
        return None


def _to_mysql_row(row: dict[str, Any]) -> dict[str, Any]:
    trade_dt = _date(row.get("trade_date"))
    trade_time = _datetime(row.get("datetime") or row.get("trade_time"))
    return {
        "symbol": str(row["symbol"]),
        "trade_date": trade_dt,
        "trade_time": trade_time,
        "seq_in_day": int(row["seq_in_day"]),
        "price": Decimal(str(row.get("price") or 0)),
        "volume": int(row.get("volume") or 0),
        "amount": Decimal(str(row.get("amount") or 0)),
        "side": str(row.get("side") or "unknown"),
        "side_label": str(row.get("side_label") or "未知"),
        "order_count": _int_or_none(row.get("order_count")),
        "raw_status": _int_or_none(row.get("raw_status")),
        "source": str(row.get("source") or "tdxapi"),
        "ingested_at": datetime.now(),
    }


def _from_mysql_row(row: dict[str, Any]) -> dict[str, Any]:
    trade_time = row.get("trade_time")
    return {
        "symbol": row.get("symbol"),
        "trade_date": _iso(row.get("trade_date")),
        "datetime": _iso(trade_time),
        "seq_in_day": row.get("seq_in_day"),
        "price": _float(row.get("price")),
        "volume": row.get("volume"),
        "amount": _float(row.get("amount")),
        "side": row.get("side"),
        "side_label": row.get("side_label"),
        "order_count": row.get("order_count"),
        "raw_status": row.get("raw_status"),
        "source": row.get("source"),
        "ingested_at": _iso(row.get("ingested_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).replace(tzinfo=None)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


trade_tick_mysql_store = TradeTickMySQLStore()

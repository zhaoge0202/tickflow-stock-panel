from __future__ import annotations

from datetime import date, datetime, time as dt_time
from unittest.mock import MagicMock
import pytest

from app.market_time import DAILY_STRATEGY_READY_TIME, INTRADAY_PREVIEW_READY_TIME, CN_TZ
from app.services.strategy_date import reject_intraday_strategy_date
from app.services.focus_versions import build_focus_three_versions


def test_reject_intraday_strategy_date_with_preview():
    # 模拟工作日 14:40 (未到 14:50)
    now_1440 = datetime(2026, 9, 11, 14, 40, tzinfo=CN_TZ)
    # 正式版应拒绝
    with pytest.raises(ValueError, match="盘中 2026-09-11 尚未收盘"):
        reject_intraday_strategy_date(date(2026, 9, 11), now=now_1440, allow_preview=False)
    # preview 也应拒绝 (因为未到 14:50)
    with pytest.raises(ValueError, match="盘中 2026-09-11 尚未收盘"):
        reject_intraday_strategy_date(date(2026, 9, 11), now=now_1440, allow_preview=True)

    # 模拟工作日 14:55 (已达 14:50，未达 15:30)
    now_1455 = datetime(2026, 9, 11, 14, 55, tzinfo=CN_TZ)
    # 正式版仍应拒绝
    with pytest.raises(ValueError, match="盘中 2026-09-11 尚未收盘"):
        reject_intraday_strategy_date(date(2026, 9, 11), now=now_1455, allow_preview=False)
    # 但 preview 模式应被允许放行 (无异常)
    reject_intraday_strategy_date(date(2026, 9, 11), now=now_1455, allow_preview=True)

    # 模拟工作日 15:35 (收盘已定版)
    now_1535 = datetime(2026, 9, 11, 15, 35, tzinfo=CN_TZ)
    # 无论是否 allow_preview 均放行
    reject_intraday_strategy_date(date(2026, 9, 11), now=now_1535, allow_preview=False)
    reject_intraday_strategy_date(date(2026, 9, 11), now=now_1535, allow_preview=True)


def test_focus_three_versions_structure():
    mock_repo = MagicMock()
    mock_store = MagicMock()
    mock_store.data_dir = "/tmp"
    mock_repo.store = mock_store

    mock_engine = MagicMock()
    mock_strat = MagicMock()
    mock_strat.meta = {"name": "双刃合-Focus"}
    mock_engine.get.return_value = mock_strat

    # 验证结构
    payload = build_focus_three_versions(
        mock_repo,
        mock_engine,
        strategy_id="custom_dual_edge_focus",
        as_of=date(2026, 9, 7),
    )
    assert "as_of" in payload
    assert "versions" in payload
    assert "preview" in payload["versions"]
    assert "final" in payload["versions"]
    assert "preselect" in payload["versions"]
    assert "dropped_from_preview" in payload
    assert "summary" in payload

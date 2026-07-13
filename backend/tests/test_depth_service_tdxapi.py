from __future__ import annotations

from types import SimpleNamespace

from app.services.depth_service import DepthService


def test_call_tdx_depth_batch_converts_realtime_rows(monkeypatch):
    class FakeTDXProvider:
        def get_realtime(self, symbols):
            assert symbols == ["002491.SZ"]
            return [{
                "symbol": "002491.SZ",
                "ask1_vol": 0,
                "ask2_vol": 12,
                "ask3_vol": None,
                "ask4_vol": 0,
                "ask5_vol": 8,
                "bid1_vol": 345,
                "bid2_vol": 20,
                "bid3_vol": None,
                "bid4_vol": 1,
                "bid5_vol": 0,
                "timestamp": 1710000000000,
            }]

    monkeypatch.setattr(
        "app.data_providers.custom.get_provider",
        lambda name: FakeTDXProvider(),
    )

    data = DepthService()._call_tdx_depth_batch(["002491.SZ"])

    assert data["002491.SZ"]["ask_volumes"] == [0, 12, None, 0, 8]
    assert data["002491.SZ"]["bid_volumes"] == [345, 20, None, 1, 0]
    assert data["002491.SZ"]["timestamp"] == 1710000000000
    assert data["002491.SZ"]["source"] == "tdxapi"


def test_depth_source_prefers_tdxapi_when_realtime_source_available(monkeypatch):
    svc = DepthService()
    svc.set_app_state(SimpleNamespace(capabilities=SimpleNamespace(has=lambda cap: False)))
    monkeypatch.setattr(DepthService, "_can_use_tdx_depth", staticmethod(lambda: True))

    assert svc.capability_status() == {
        "available": True,
        "source": "tdxapi",
        "requires": None,
    }


def test_depth_source_falls_back_to_tickflow_capability(monkeypatch):
    svc = DepthService()
    svc.set_app_state(SimpleNamespace(capabilities=SimpleNamespace(has=lambda cap: True)))
    monkeypatch.setattr(DepthService, "_can_use_tdx_depth", staticmethod(lambda: False))

    assert svc.capability_status()["source"] == "tickflow"

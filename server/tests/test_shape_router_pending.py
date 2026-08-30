# -*- coding: utf-8 -*-
"""
缺失周期按需建库协议测试。

创建时间：2026-08-29
作用：验证形态接口在所需缓存未完整时返回 HTTP 202 / DATA_BUILDING，且给出缺失品类。
使用方式：cd server && python -m pytest tests/test_shape_router_pending.py -q

修改时间：2026-08-30
修改作用：验证匹配 API 在参数校验阶段拒绝已停用的期货品类。

修改时间：2026-08-30
修改作用：验证行情失败冷却立即返回 DATA_UNAVAILABLE，避免硬件端长时间空等。

修改时间：2026-08-30
修改作用：验证 all 模式在 A 股缺失时仍立即使用已就绪 Crypto 数据执行匹配。

修改时间：2026-08-30
修改作用：验证路由把实际缺失品类传给按需下载服务，单选 A 股不会误排队 Crypto。
"""
from __future__ import annotations

import json
import importlib

import pytest
from pydantic import ValidationError

shape_router = importlib.import_module("routers.shape_router")


class _Manager:
    def __init__(self, buckets):
        self._buckets = buckets

    def buckets_status(self):
        return dict(self._buckets)


class _Service:
    def __init__(self):
        self.queued = []

    def get_config(self):
        return {"stock": {"enabled": True}, "crypto": {"enabled": True}}

    def ensure_timeframe(self, timeframe, categories=None, user_requested=False):
        self.queued.append((timeframe, categories, user_requested))
        return {
            "timeframe": timeframe,
            "added": True,
            "state": "refresh_queued",
            "message": f"{timeframe} market data queued",
        }

    def status(self):
        return {"state": "refresh_queued", "message": "后台建库", "progress": 0.1}


def test_all_category_waits_when_no_enabled_category_is_ready(monkeypatch):
    service = _Service()
    monkeypatch.setattr(shape_router, "index_manager", _Manager({"stock_15m": 0, "crypto_15m": 0}))
    monkeypatch.setattr(shape_router, "market_data_service", service)
    request = shape_router.ShapeMatchRequest(
        points=[(0.0, 0.2), (0.5, 0.8), (1.0, 0.3)],
        category="all",
        timeframe="15m",
    )

    response = shape_router.api_shape_match(request, None)
    payload = json.loads(response.body)

    assert response.status_code == 202
    assert payload["code"] == 1001
    assert payload["msg"] == "DATA_BUILDING"
    assert payload["data"]["missing_categories"] == ["stock", "crypto"]
    assert service.queued == [("15m", ["stock", "crypto"], True)]


def test_futures_category_is_rejected():
    with pytest.raises(ValidationError):
        shape_router.ShapeMatchRequest(
            points=[(0.0, 0.2), (0.5, 0.8), (1.0, 0.3)],
            category="futures",
            timeframe="1d",
        )


def test_backoff_returns_data_unavailable_immediately(monkeypatch):
    class BackoffService(_Service):
        def ensure_timeframe(self, timeframe, categories=None, user_requested=False):
            return {
                "timeframe": timeframe,
                "added": False,
                "queued": False,
                "state": "retry_backoff",
                "message": "行情源暂时不可用，约 600 秒后自动重试",
                "retry_after_seconds": 600,
            }

    service = BackoffService()
    monkeypatch.setattr(shape_router, "index_manager", _Manager({"stock_1d": 0, "crypto_1d": 0}))
    monkeypatch.setattr(shape_router, "market_data_service", service)
    request = shape_router.ShapeMatchRequest(
        points=[(0.0, 0.2), (0.5, 0.8), (1.0, 0.3)],
        category="all",
        timeframe="1d",
    )

    response = shape_router.api_shape_match(request, None)
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["code"] == 1002
    assert payload["msg"] == "DATA_UNAVAILABLE"
    assert payload["data"]["retry_after_seconds"] == 600


def test_all_category_searches_ready_market_when_other_market_is_missing(monkeypatch):
    class PartialManager(_Manager):
        last_diagnostics = {}

        def __init__(self, buckets):
            super().__init__(buckets)
            self.searched = False

        def search(self, **_kwargs):
            self.searched = True
            return [], 0

    class PartialService(_Service):
        def ensure_timeframe(self, timeframe, categories=None, user_requested=False):
            self.queued.append((timeframe, categories, user_requested))
            return {
                "timeframe": timeframe,
                "queued": False,
                "state": "retry_backoff",
                "retry_after_seconds": 600,
            }

    manager = PartialManager({"stock_1d": 0, "crypto_1d": 300})
    service = PartialService()
    monkeypatch.setattr(shape_router, "index_manager", manager)
    monkeypatch.setattr(shape_router, "market_data_service", service)
    request = shape_router.ShapeMatchRequest(
        points=[(0.0, 0.2), (0.5, 0.8), (1.0, 0.3)],
        category="all",
        timeframe="1d",
    )

    response = shape_router.api_shape_match(request, None)

    assert response.code == 0
    assert manager.searched is True
    assert response.data["debug"]["partial"] is True
    assert response.data["debug"]["missing_categories"] == ["stock"]
    assert service.queued == [("1d", ["stock"], True)]


def test_stock_only_request_queues_only_stock(monkeypatch):
    service = _Service()
    monkeypatch.setattr(shape_router, "index_manager", _Manager({"stock_1d": 0, "crypto_1d": 300}))
    monkeypatch.setattr(shape_router, "market_data_service", service)
    request = shape_router.ShapeMatchRequest(
        points=[(0.0, 0.2), (0.5, 0.8), (1.0, 0.3)],
        category="stock",
        timeframe="1d",
    )

    response = shape_router.api_shape_match(request, None)
    payload = json.loads(response.body)

    assert response.status_code == 202
    assert payload["data"]["message"].isascii()
    assert service.queued == [("1d", ["stock"], True)]

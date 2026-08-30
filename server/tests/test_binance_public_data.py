# -*- coding: utf-8 -*-
"""
Binance Public Crypto 客户端测试。

创建时间：2026-08-28
作用：用纯本地假响应验证 USDT 现货选池、最新已收盘 K 线过滤和 429 阻断，
      测试过程不会访问 Binance 网络，也不会消耗真实请求权重。
使用方式：cd server && python -m pytest tests/test_binance_public_data.py -q

修改时间：2026-08-29
修改作用：验证服务端 60m 周期正确映射到 Binance 官方 1h interval，且正常成功请求不产生逐标的日志。

修改时间：2026-08-29
修改作用：验证 count=0 选择全部交易对，以及 Crypto 可按成交笔数排序。
"""
from __future__ import annotations

import time

import pytest
import requests

from core.binance_public_data import BinancePublicClient, BinancePublicError, BinanceRateLimitError


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {"X-MBX-USED-WEIGHT-1M": "100"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        return self.responses.pop(0)


def test_select_crypto_pool_uses_trading_quote_volume():
    session = FakeSession([
        FakeResponse({"symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT"},
            {"symbol": "ETHUSDT", "status": "TRADING", "baseAsset": "ETH", "quoteAsset": "USDT"},
            {"symbol": "OLDUSDT", "status": "BREAK", "baseAsset": "OLD", "quoteAsset": "USDT"},
            {"symbol": "USD1USDT", "status": "TRADING", "baseAsset": "USD1", "quoteAsset": "USDT"},
            {"symbol": "BTCUSDC", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDC"},
        ]}),
        FakeResponse([
            {"symbol": "BTCUSDT", "quoteVolume": "200"},
            {"symbol": "ETHUSDT", "quoteVolume": "500"},
            {"symbol": "OLDUSDT", "quoteVolume": "900"},
            {"symbol": "USD1USDT", "quoteVolume": "1000"},
        ]),
    ])
    client = BinancePublicClient(session)
    client.configure(interval_ms=50, weight_limit=1200, retry_count=0)
    pool, warnings = client.select_crypto_pool(2, "market_cap", "top", "USDT")
    assert pool == [
        ("BINANCE:ETHUSDT", "ETH/USDT"),
        ("BINANCE:BTCUSDT", "BTC/USDT"),
    ]
    assert warnings and "quoteVolume" in warnings[0]


def test_select_crypto_pool_zero_means_all_and_supports_trade_count():
    session = FakeSession([
        FakeResponse({"symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT"},
            {"symbol": "ETHUSDT", "status": "TRADING", "baseAsset": "ETH", "quoteAsset": "USDT"},
        ]}),
        FakeResponse([
            {"symbol": "BTCUSDT", "count": 100},
            {"symbol": "ETHUSDT", "count": 500},
        ]),
    ])
    client = BinancePublicClient(session)
    client.configure(interval_ms=50, weight_limit=1200, retry_count=0)
    pool, warnings = client.select_crypto_pool(0, "trade_count", "top", "USDT")
    assert pool == [
        ("BINANCE:ETHUSDT", "ETH/USDT"),
        ("BINANCE:BTCUSDT", "BTC/USDT"),
    ]
    assert warnings == []


def test_fetch_ohlcv_drops_current_unclosed_bar():
    now_ms = int(time.time() * 1000)
    payload = [
        [now_ms - 300000, "10", "12", "9", "11", "1", now_ms - 240000],
        [now_ms - 200000, "11", "13", "10", "12", "1", now_ms - 140000],
        [now_ms - 100000, "12", "14", "11", "13", "1", now_ms + 100000],
    ]
    session = FakeSession([FakeResponse(payload)])
    client = BinancePublicClient(session)
    client.configure(interval_ms=50, weight_limit=1200, retry_count=0)
    logs = []
    result = client.fetch_ohlcv("BINANCE:BTCUSDT", "60m", 10, logs.append)
    assert result is not None
    assert result.shape == (2, 5)
    assert result[-1, 4] == 12.0
    assert session.calls[0][1]["interval"] == "1h"
    assert logs == []  # 正常逐请求/逐 K 线成功信息不再刷屏。


def test_http_429_sets_shared_block_without_long_sleep():
    response = FakeResponse({}, 429, {"Retry-After": "120", "X-MBX-USED-WEIGHT-1M": "6000"})
    client = BinancePublicClient(FakeSession([response]))
    client.configure(interval_ms=50, weight_limit=1200, retry_count=0, max_retry_after_seconds=60)
    with pytest.raises(BinanceRateLimitError):
        client.fetch_ohlcv("BINANCE:BTCUSDT", "1d", 20)
    assert client.limiter.status()["blocked_seconds"] > 110


def test_non_market_data_path_is_rejected_before_http():
    session = FakeSession([])
    client = BinancePublicClient(session)
    with pytest.raises(BinancePublicError, match="非白名单"):
        client._request_json("/api/v3/order", None, 1, None)
    assert session.calls == []

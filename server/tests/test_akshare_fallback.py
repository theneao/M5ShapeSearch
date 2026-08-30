# -*- coding: utf-8 -*-
"""
AKShare A 股故障切换测试。

创建时间：2026-08-30
作用：验证东财全市场排名失败后使用新浪成交量选池，以及日线切换到腾讯端点。
使用方式：cd server && python -m pytest tests/test_akshare_fallback.py -q

修改时间：2026-08-31
修改作用：验证分钟接口首次失败后开启熔断，后续标的不再请求且异常可按批次汇总。
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pandas as pd

from core import akshare_data_builder as builder


def test_stock_pool_falls_back_to_sina_volume(monkeypatch):
    frame = pd.DataFrame({
        "代码": ["sh600001", "sz000002", "bj920003"],
        "名称": ["A", "B", "C"],
        "成交量": [10.0, 30.0, 20.0],
    })
    fake_ak = SimpleNamespace(
        stock_zh_a_spot_em=lambda: (_ for _ in ()).throw(ConnectionError("eastmoney down")),
        stock_zh_a_spot=lambda: frame,
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    selected = builder.select_stock_pool(2, "market_cap", "top")

    assert selected == [("SZ:000002", "B"), ("BJ:920003", "C")]


def test_daily_kline_falls_back_to_tencent(monkeypatch):
    frame = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=20, freq="D"),
        "open": range(10, 30),
        "high": range(11, 31),
        "low": range(9, 29),
        "close": range(10, 30),
    })
    calls = []

    def fake_tx(**kwargs):
        calls.append(kwargs)
        return frame

    fake_ak = SimpleNamespace(stock_zh_a_hist_tx=fake_tx)
    fake_module = SimpleNamespace(get_tqdm=lambda: None)
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    monkeypatch.setattr(builder.importlib, "import_module", lambda _name: fake_module)
    monkeypatch.setattr(
        builder,
        "_eastmoney_unavailable_until",
        {"pool": 0.0, "daily": float("inf"), "minute": 0.0},
    )

    result = builder.fetch_stock_ohlcv("SZ:000001", "1d", 10)

    assert result is not None
    assert result.shape == (10, 5)
    assert calls[0]["symbol"] == "sz000001"
    assert calls[0]["adjust"] == "qfq"


def test_minute_failure_opens_circuit_and_aggregates(monkeypatch):
    calls = []

    def fail_minute(**kwargs):
        calls.append(kwargs)
        raise ConnectionError("minute source down")

    monkeypatch.setitem(
        sys.modules, "akshare", SimpleNamespace(stock_zh_a_hist_min_em=fail_minute)
    )
    monkeypatch.setattr(
        builder,
        "_eastmoney_unavailable_until",
        {"pool": 0.0, "daily": 0.0, "minute": 0.0},
    )
    builder._clear_stock_fetch_failures("30m")

    assert builder.fetch_stock_ohlcv("SH:600001", "30m", 60) is None
    assert builder.fetch_stock_ohlcv("SZ:000002", "30m", 60) is None
    summary = builder._take_stock_fetch_failures("30m")

    assert len(calls) == 1
    assert summary == {"count": 2, "reason": "ConnectionError: minute source down"}

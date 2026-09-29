# -*- coding: utf-8 -*-
"""
AKShare A 股故障切换测试。

创建时间：2026-08-30
作用：验证东财全市场排名失败后使用新浪成交量选池，以及日线切换到腾讯端点。
使用方式：cd server && python -m pytest tests/test_akshare_fallback.py -q

修改时间：2026-08-31
修改作用：验证分钟接口首次失败后开启熔断，后续标的不再请求且异常可按批次汇总。

修改时间：2026-08-31
修改作用：验证东财日线收到显式 timeout，腾讯备用端点使用连接/读取双超时且不执行无界前置探测。

修改时间：2026-08-31
修改作用：验证东财分钟源不可用时切到带硬超时的新浪分钟源，并验证两层分钟源独立熔断。

修改时间：2026-09-15
修改作用：行情数组扩展为固定七列 OHLCV+turnover，并验证备用源缺失成交额时保持 NaN。

修改时间：2026-09-30
修改作用：验证行业板块 AKShare 请求受代理影响时，使用禁用环境代理且带硬超时的同源直连重试。
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pandas as pd

from core import akshare_data_builder as builder


def test_sector_list_retries_direct_without_environment_proxy(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(
            stock_board_industry_name_em=lambda: (_ for _ in ()).throw(
                ConnectionError("proxy down")
            )
        ),
    )
    calls = []

    class FakeResponse:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "data": {
                    "diff": [
                        {"f12": "BK0002", "f14": "软件开发"},
                        {"f12": "BK0001", "f14": "银行"},
                    ]
                }
            }

    class FakeSession:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            calls.append((url, kwargs, self.trust_env))
            return FakeResponse()

    import requests
    monkeypatch.setattr(requests, "Session", FakeSession)

    assert builder.list_stock_sectors() == ["软件开发", "银行"]
    assert calls[0][0].startswith("https://17.push2.eastmoney.com/")
    assert calls[0][1]["timeout"] == (4, 10)
    assert calls[0][2] is False


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


def test_sector_filter_is_applied_to_sina_fallback_pool(monkeypatch):
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
    monkeypatch.setattr(builder, "_stock_sector_codes", lambda _ak, _sector: {"000002"})

    selected = builder.select_stock_pool(10, "volume", "top", sector="软件开发")

    assert selected == [("SZ:000002", "B")]


def test_daily_kline_falls_back_to_tencent(monkeypatch):
    frame = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=20, freq="D"),
        "open": range(10, 30),
        "high": range(11, 31),
        "low": range(9, 29),
        "close": range(10, 30),
    })
    calls = []

    class FakeResponse:
        text = "v=" + __import__("json").dumps({
            "data": {
                "sz000001": {
                    "qfqday": [
                        [row.date.strftime("%Y-%m-%d"), str(row.open), str(row.close),
                         str(row.high), str(row.low), "100"]
                        for row in frame.itertuples(index=False)
                    ]
                }
            }
        })

        @staticmethod
        def raise_for_status():
            return None

    def fake_get(_url, **kwargs):
        calls.append(kwargs)
        return FakeResponse()

    fake_ak = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(
        builder,
        "_source_unavailable_until",
        {
            "eastmoney_pool": 0.0,
            "eastmoney_daily": float("inf"),
            "eastmoney_minute": 0.0,
            "tencent_daily": 0.0,
            "sina_minute": 0.0,
        },
    )

    result = builder.fetch_stock_ohlcv("SZ:000001", "1d", 10)

    assert result is not None
    assert result.shape == (10, 7)
    assert result[:, 5].tolist() == [100.0] * 10
    assert result[:, 6].size == 10
    assert __import__("numpy").isnan(result[:, 6]).all()
    assert calls[0]["timeout"] == (4, 8)
    assert calls[0]["params"]["param"].startswith("sz000001,day,")


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
        "_source_unavailable_until",
        {
            "eastmoney_pool": 0.0,
            "eastmoney_daily": 0.0,
            "eastmoney_minute": 0.0,
            "tencent_daily": 0.0,
            "sina_minute": float("inf"),
        },
    )
    builder._clear_stock_fetch_failures("30m")

    assert builder.fetch_stock_ohlcv("SH:600001", "30m", 60) is None
    assert builder.fetch_stock_ohlcv("SZ:000002", "30m", 60) is None
    summary = builder._take_stock_fetch_failures("30m")

    assert len(calls) == 1
    assert summary == {"count": 2, "reason": "RuntimeError: Sina minute circuit is open"}


def test_eastmoney_daily_receives_explicit_timeout(monkeypatch):
    calls = []
    frame = pd.DataFrame({
        "日期": pd.date_range("2026-01-01", periods=20, freq="D"),
        "开盘": range(10, 30),
        "最高": range(11, 31),
        "最低": range(9, 29),
        "收盘": range(10, 30),
    })

    def fake_daily(**kwargs):
        calls.append(kwargs)
        return frame

    monkeypatch.setitem(
        sys.modules, "akshare", SimpleNamespace(stock_zh_a_hist=fake_daily)
    )
    monkeypatch.setattr(
        builder,
        "_source_unavailable_until",
        {
            "eastmoney_pool": 0.0,
            "eastmoney_daily": 0.0,
            "eastmoney_minute": 0.0,
            "tencent_daily": 0.0,
            "sina_minute": 0.0,
        },
    )

    result = builder.fetch_stock_ohlcv("SH:600001", "1d", 10)

    assert result is not None
    assert result.shape == (10, 7)
    assert calls[0]["timeout"] == 8


def test_minute_kline_falls_back_to_sina_with_timeout(monkeypatch):
    calls = []
    timestamps = pd.date_range("2026-08-31 09:30:00", periods=8, freq="15min")
    rows = [
        {
            "day": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "open": str(10 + index),
            "high": str(11 + index),
            "low": str(9 + index),
            "close": str(10.5 + index),
            "volume": "100",
        }
        for index, timestamp in enumerate(timestamps)
    ]

    class FakeResponse:
        text = "prefix=(" + __import__("json").dumps(rows) + ");"

        @staticmethod
        def raise_for_status():
            return None

    def fake_get(_url, **kwargs):
        calls.append(kwargs)
        return FakeResponse()

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace())
    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(
        builder,
        "_source_unavailable_until",
        {
            "eastmoney_pool": 0.0,
            "eastmoney_daily": 0.0,
            "eastmoney_minute": float("inf"),
            "tencent_daily": 0.0,
            "sina_minute": 0.0,
        },
    )

    result = builder.fetch_stock_ohlcv("SH:600001", "15m", 6)

    assert result is not None
    assert result.shape == (6, 7)
    assert result[:, 5].tolist() == [100.0] * 6
    assert calls[0]["timeout"] == (4, 8)
    assert calls[0]["params"]["scale"] == "15"

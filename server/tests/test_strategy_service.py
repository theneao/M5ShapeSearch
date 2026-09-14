# -*- coding: utf-8 -*-
"""
策略服务测试。

创建时间：2026-09-15
作用：验证 Sequoia-X 六个预设条件、手绘策略持久化，以及交集/并集的集合语义。
使用方式：cd server && python -m pytest tests/test_strategy_service.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from shape_search.config import CpuSearchConfig
from shape_search.preprocessing import build_market_series
from strategy.strategy_service import StrategyService


def _market(
    symbol: str,
    close,
    *,
    open_=None,
    high=None,
    low=None,
    volume=None,
    turnover=None,
):
    close = np.asarray(close, dtype=float)
    open_ = np.asarray(open_ if open_ is not None else close, dtype=float)
    high = np.asarray(high if high is not None else np.maximum(open_, close), dtype=float)
    low = np.asarray(low if low is not None else np.minimum(open_, close), dtype=float)
    volume = np.asarray(volume, dtype=float) if volume is not None else None
    turnover = np.asarray(turnover, dtype=float) if turnover is not None else None
    return build_market_series(
        series_id=symbol,
        symbol=symbol,
        symbol_name=symbol,
        category="stock",
        timeframe="1d",
        timestamps=np.arange(1, len(close) + 1),
        values=close,
        values_are_prices=True,
        ohlc_values=np.column_stack([open_, high, low, close]),
        volume_values=volume,
        turnover_values=turnover,
    )


def test_all_six_sequoia_presets_match_their_exact_conditions():
    service = StrategyService.__new__(StrategyService)

    turtle_close = np.asarray([9.0] * 20 + [12.0])
    turtle = _market(
        "TURTLE", turtle_close,
        open_=np.asarray([9.0] * 20 + [11.0]),
        high=np.asarray([10.0] * 20 + [12.2]),
        low=np.asarray([8.0] * 20 + [10.8]),
        volume=np.asarray([100.0] * 21),
        turnover=np.asarray([10_000_000.0] * 20 + [110_000_000.0]),
    )
    assert service._evaluate_preset("sequoia_turtle_trade", turtle) == (True, None)

    ma_close = np.asarray([10.0] * 16 + [9.0] * 4 + [20.0])
    ma_volume = np.asarray([100.0] * 20 + [1000.0])
    ma = _market("MA", ma_close, volume=ma_volume)
    assert service._evaluate_preset("sequoia_ma_volume", ma) == (True, None)

    high = np.asarray([10.0] * 30 + [10.0] * 10)
    low = np.asarray([5.0] + [8.5] * 29 + [9.0] * 10)
    flag = _market(
        "FLAG", np.asarray([9.0] * 40), high=high, low=low,
        volume=np.asarray([100.0] * 39 + [10.0]),
    )
    assert service._evaluate_preset("sequoia_high_tight_flag", flag) == (True, None)

    shakeout = _market(
        "SHAKE", [10.0, 11.0, 11.0], open_=[10.0, 10.5, 12.0],
        high=[10.2, 11.2, 12.1], low=[9.8, 10.4, 11.0], volume=[100, 100, 300],
    )
    assert service._evaluate_preset("sequoia_limit_up_shakeout", shakeout) == (True, None)

    up_close = np.asarray([10.0] * 40 + [20.0] * 20 + [18.0])
    up = _market("UP", up_close, volume=np.asarray([100.0] * 60 + [1000.0]))
    assert service._evaluate_preset("sequoia_uptrend_limit_down", up) == (True, None)

    markets = []
    for index in range(10):
        close = np.linspace(10.0, 11.0 + index, 121)
        markets.append(_market(f"RPS{index}", close, high=close * 1.01))
    rps, unavailable = service._evaluate_rps(markets)
    assert unavailable == 0
    assert set(rps) == {"RPS8", "RPS9"}


class _Manager:
    def __init__(self, markets):
        self.config = CpuSearchConfig()
        self._markets = tuple(markets)

    def market_snapshot(self, category="all", timeframe="1d"):
        return tuple(
            item for item in self._markets
            if item.timeframe == timeframe and (category == "all" or item.category == category)
        )

    def search(self, **_kwargs):
        return [
            {"symbol": self._markets[0].symbol, "match_score": 0.88},
            {"symbol": self._markets[1].symbol, "match_score": 0.72},
        ], 2


def test_saved_sketch_and_union_intersection(tmp_path):
    market_a = _market("A", np.linspace(10.0, 20.0, 40), volume=np.ones(40) * 100)
    market_b = _market("B", np.linspace(20.0, 10.0, 40), volume=np.ones(40) * 100)
    service = StrategyService(_Manager([market_a, market_b]), tmp_path)
    saved = service.save_sketch("V shape", [(0, 1), (0.5, 0), (1, 1)], threshold=0.8)

    result = service.screen([saved["id"]], "union", "stock", "1d", 20)
    assert result["total"] == 1
    assert result["list"][0]["symbol"] == "A"

    reloaded = StrategyService(_Manager([market_a, market_b]), tmp_path)
    assert reloaded.catalog()["saved_sketches"][0]["name"] == "V shape"
    assert "points" not in reloaded.catalog(compact=True)["saved_sketches"][0]
    assert reloaded.delete_sketch(saved["id"]) is True
    assert reloaded.catalog()["saved_sketches"] == []


def test_two_strategy_union_and_intersection_use_exact_symbol_sets(tmp_path):
    markets = [
        _market(symbol, np.linspace(10.0, 20.0, 40), volume=np.ones(40) * 100)
        for symbol in ("A", "B", "C")
    ]
    service = StrategyService(_Manager(markets), tmp_path)
    first = service.save_sketch("First", [(0, 0), (0.5, 0.5), (1, 1)])
    second = service.save_sketch("Second", [(0, 1), (0.5, 0.5), (1, 0)])

    def fake_sketch(item, _category, _timeframe, _markets):
        return {"A": 0.9, "B": 0.8} if item["id"] == first["id"] else {"B": 0.7, "C": 0.95}

    service._evaluate_sketch = fake_sketch
    selected = [first["id"], second["id"]]

    intersection = service.screen(selected, "intersection", "stock", "1d", 20)
    union = service.screen(selected, "union", "stock", "1d", 20)

    assert intersection["total"] == 1
    assert intersection["list"][0]["symbol"] == "B"
    assert intersection["list"][0]["coverage"] == 2
    assert {item["symbol"] for item in union["list"]} == {"A", "B", "C"}


def test_preset_rejects_unsupported_market_before_scanning(tmp_path):
    market = _market("A", np.linspace(10.0, 20.0, 40), volume=np.ones(40))
    service = StrategyService(_Manager([market]), tmp_path)

    with pytest.raises(ValueError, match="不支持市场 crypto"):
        service.validate_request(
            ["sequoia_turtle_trade"], "intersection", "crypto", "1d"
        )

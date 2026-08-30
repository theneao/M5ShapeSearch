# -*- coding: utf-8 -*-
"""
CPU-only 连续序列形态搜索测试。

创建时间：2026-08-28
作用：验证 FFT-NCC 正确性、峰谷方向区分、ShapeDTW 边界、完整搜索与 NPZ 重载。
使用方式：cd server && python -m pytest tests/test_cpu_shape_search.py -q

修改时间：2026-08-28
修改作用：增加 OHLC 持久化与硬件详情 K 线查询覆盖。
"""
from __future__ import annotations

import numpy as np

from core.cpu_shape_search_manager import CpuShapeSearchManager
from core.mock_shape_generator import ShapeSample
from core.shape_features import full_pipeline
from shape_search import CpuSearchConfig, CpuShapeSearchEngine
from shape_search.ncc import sliding_ncc
from shape_search.preprocessing import build_market_series, z_normalize
from shape_search.shapedtw import subsequence_shapedtw
from shape_search.turning_points import detect_turning_points, turning_similarity


def _pattern(count: int) -> np.ndarray:
    x = np.linspace(0.0, 1.0, count)
    return np.interp(
        x,
        [0.0, 0.18, 0.43, 0.68, 0.82, 1.0],
        [0.20, 0.88, 0.18, 0.70, 0.32, 0.55],
    )


def _test_config() -> CpuSearchConfig:
    return CpuSearchConfig(
        min_length=40,
        max_length=130,
        scale_ratio=1.25,
        ncc_min_score=0.45,
        top_per_scale_per_series=8,
        before_turning_point=80,
        before_dtw=25,
    ).validate()


def test_sliding_ncc_matches_bruteforce():
    rng = np.random.default_rng(20260828)
    query = rng.normal(size=37)
    series = rng.normal(size=311)
    actual = sliding_ncc(query, series)
    expected = np.asarray([
        np.corrcoef(query, series[start:start + len(query)])[0, 1]
        for start in range(len(series) - len(query) + 1)
    ])
    assert np.max(np.abs(actual - expected)) < 1e-10


def test_turning_points_reject_vertical_inverse():
    cfg = _test_config()
    query = _pattern(96)
    same = query + np.sin(np.linspace(0.0, 3.0, len(query))) * 0.005
    inverse = -query
    query_turns = detect_turning_points(z_normalize(query), cfg.turning_prominence)
    same_score = turning_similarity(
        query_turns,
        detect_turning_points(z_normalize(same), cfg.turning_prominence),
        cfg,
    )
    inverse_score = turning_similarity(
        query_turns,
        detect_turning_points(z_normalize(inverse), cfg.turning_prominence),
        cfg,
    )
    assert same_score > 0.95
    assert same_score > inverse_score + 0.30


def test_subsequence_shapedtw_recovers_embedded_boundary():
    rng = np.random.default_rng(7)
    query = _pattern(80)
    reference = np.concatenate([
        rng.normal(0.0, 0.05, 35),
        query + rng.normal(0.0, 0.01, len(query)),
        rng.normal(0.0, 0.05, 42),
    ])
    distance, start, end, path = subsequence_shapedtw(
        z_normalize(query),
        z_normalize(reference),
        _test_config(),
        expected_length=80,
    )
    assert distance < 0.40
    assert abs(start - 35) <= 8
    assert abs(end - 115) <= 8
    assert len(path) >= len(query)


def test_engine_multiscale_and_inverse_rejection():
    rng = np.random.default_rng(29)
    cfg = _test_config()
    query_y = _pattern(128)
    query = np.column_stack([np.linspace(0.0, 1.0, 128), query_y]).tolist()
    markets = []
    expected_starts = {}
    for index, length in enumerate((73, 112)):
        embedded = _pattern(length) + rng.normal(0.0, 0.015, length)
        log_price = np.cumsum(rng.normal(0.0, 0.025, 360))
        start = 95 + index * 80
        log_price[start:start + length] = embedded + log_price[start] - embedded[0]
        symbol = f"SYM{index}"
        expected_starts[symbol] = start
        markets.append(build_market_series(
            series_id=f"series-{index}", symbol=symbol, symbol_name=symbol,
            category="stock", timeframe="1d", timestamps=np.arange(360),
            values=np.exp(log_price + 5.0), values_are_prices=True, config=cfg,
        ))
    inverse = -_pattern(90)
    inverse_log = np.cumsum(rng.normal(0.0, 0.025, 360))
    inverse_log[140:230] = inverse + inverse_log[140] - inverse[0]
    markets.append(build_market_series(
        series_id="inverse", symbol="INVERSE", symbol_name="inverse",
        category="stock", timeframe="1d", timestamps=np.arange(360),
        values=np.exp(inverse_log + 5.0), values_are_prices=True, config=cfg,
    ))

    results, diagnostics = CpuShapeSearchEngine(cfg).search(
        query, markets, category="stock", timeframe="1d", top_k=5,
    )
    assert results
    assert results[0].symbol != "INVERSE"
    for symbol, expected_start in expected_starts.items():
        assert any(
            result.symbol == symbol
            and abs(int(result.refined_start or 0) - expected_start) < 25
            for result in results
        )
    assert diagnostics["ncc_candidates"] > diagnostics["dtw_candidates"]


def test_manager_persistence_and_explainable_scores(tmp_path):
    rng = np.random.default_rng(31)
    cfg = _test_config()
    embedded = _pattern(88)
    log_price = np.cumsum(rng.normal(0.0, 0.02, 300))
    log_price[120:208] = embedded + log_price[120] - embedded[0]
    close = np.exp(log_price + 5.0)
    normalized = (close - close.min()) / max(float(np.ptp(close)), 1e-12)
    points = np.column_stack([np.linspace(0.0, 1.0, len(close)), normalized]).tolist()
    seq, _stats, vector = full_pipeline(points)
    sample = ShapeSample(
        sample_id=1, category="stock", symbol="TEST:CPU", symbol_name="CPU测试",
        shape_type="feature_match", raw_points=points, seq_128=seq, vector=vector,
        start_ts=0, end_ts=299, close_price=float(close[-1]), change_pct=0.0,
        market_timestamps=list(range(300)), close_series=close.tolist(),
        ohlc_series=np.column_stack([close, close * 1.01, close * 0.99, close]).tolist(),
    )
    manager = CpuShapeSearchManager(str(tmp_path), config=cfg)
    assert manager.build_from_samples([sample], timeframe="1d") == 1
    query_y = _pattern(128)
    query = np.column_stack([np.linspace(0.0, 1.0, 128), query_y]).tolist()
    results, recalled = manager.search(
        category="stock", timeframe="1d", top_k=3, query_points=query,
    )
    assert results and recalled > 0
    assert results[0]["_score_method"] == "cpu_multiscale_shapedtw"
    assert set(results[0]["_score_details"]) >= {
        "price_ncc", "derivative_ncc", "turning_score",
        "shapedtw_distance", "shapedtw_score",
    }
    assert results[0]["_warping_path"]

    reloaded = CpuShapeSearchManager(str(tmp_path), config=cfg)
    assert reloaded.load_all() == ["stock_1d"]
    results_after_reload, _ = reloaded.search(
        category="stock", timeframe="1d", top_k=3, query_points=query,
    )
    assert results_after_reload
    assert results_after_reload[0]["symbol"] == results[0]["symbol"]
    assert abs(results_after_reload[0]["match_score"] - results[0]["match_score"]) < 1e-6
    detail = reloaded.get_kline("TEST:CPU", "1d", limit=80)
    assert detail is not None
    assert detail["ohlc_exact"] is True
    assert len(detail["bars"]) == 80
    assert detail["bars"][-1]["c"] == round(float(close[-1]), 8)

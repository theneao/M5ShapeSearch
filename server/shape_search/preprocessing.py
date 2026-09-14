# -*- coding: utf-8 -*-
"""
手绘与市场连续序列预处理。

创建时间：2026-08-28
作用：完成 x 单调化、128 点重采样、Savitzky-Golay 平滑、Z 标准化和 log-price 转换。
使用方式：preprocess_sketch(points, config)；build_market_series(..., values_are_prices=True)。

修改时间：2026-08-28
修改作用：市场序列构建可附带同长度 OHLC，供详情 API 使用且不进入检索计算。
"""
from __future__ import annotations
from typing import Any, Dict, Iterable, Optional

import numpy as np
from scipy.signal import savgol_filter

from .config import CpuSearchConfig
from .models import MarketSeries, QueryFeatures


EPS = 1e-8


def z_normalize(values: np.ndarray) -> np.ndarray:
    """按整条序列 Z-normalize；常量序列返回全零。"""
    x = np.asarray(values, dtype=np.float64).reshape(-1)
    if len(x) == 0:
        return x.copy()
    std = float(np.std(x))
    if std < EPS:
        return np.zeros_like(x)
    return (x - float(np.mean(x))) / (std + EPS)


def resample_1d(values: np.ndarray, target_n: int) -> np.ndarray:
    """按归一化时间轴线性重采样，避免 FFT resample 的边界振铃。"""
    x = np.asarray(values, dtype=np.float64).reshape(-1)
    target_n = max(2, int(target_n))
    if len(x) < 2:
        raise ValueError("sequence needs at least 2 values")
    if len(x) == target_n:
        return x.copy()
    old_pos = np.linspace(0.0, 1.0, len(x))
    new_pos = np.linspace(0.0, 1.0, target_n)
    return np.interp(new_pos, old_pos, x)


def safe_savgol(
    values: np.ndarray,
    window_length: int = 7,
    polyorder: int = 2,
) -> np.ndarray:
    """在短序列上自动缩小奇数窗口，再调用 SciPy Savitzky-Golay。"""
    x = np.asarray(values, dtype=np.float64).reshape(-1)
    if len(x) < 3:
        return x.copy()
    window = min(max(3, int(window_length)), len(x))
    if window % 2 == 0:
        window -= 1
    order = min(max(1, int(polyorder)), window - 1)
    if window <= order or window < 3:
        return x.copy()
    return np.asarray(
        savgol_filter(x, window_length=window, polyorder=order, mode="interp"),
        dtype=np.float64,
    )


def clean_sketch_points(points: Iterable, y_flip: bool = False) -> np.ndarray:
    """清除非法点、按 x 排序并合并重复/极近 x。"""
    arr = np.asarray(list(points), dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] < 2:
        raise ValueError("sketch points must be an Nx2 array")
    arr = arr[:, :2]
    arr = arr[np.isfinite(arr).all(axis=1)]
    if len(arr) < 3:
        raise ValueError("sketch needs at least 3 valid points")
    arr = arr[np.argsort(arr[:, 0], kind="stable")]

    x_span = max(float(arr[-1, 0] - arr[0, 0]), EPS)
    min_gap = x_span * 1e-5
    merged = []
    group = [arr[0]]
    for point in arr[1:]:
        if float(point[0] - group[-1][0]) <= min_gap:
            group.append(point)
        else:
            merged.append(np.mean(group, axis=0))
            group = [point]
    merged.append(np.mean(group, axis=0))
    cleaned = np.asarray(merged, dtype=np.float64)
    if len(cleaned) < 3 or float(np.ptp(cleaned[:, 0])) < EPS:
        raise ValueError("sketch x coordinates do not form a usable time axis")
    if y_flip:
        cleaned[:, 1] = -cleaned[:, 1]
    return cleaned


def preprocess_sketch(
    points: Iterable,
    config: Optional[CpuSearchConfig] = None,
    y_flip: bool = False,
) -> QueryFeatures:
    """构建规范定义的 QueryFeatures。"""
    cfg = (config or CpuSearchConfig()).validate()
    cleaned = clean_sketch_points(points, y_flip=y_flip)
    x = cleaned[:, 0]
    y = cleaned[:, 1]
    x_new = np.linspace(float(x[0]), float(x[-1]), cfg.query_resample_points)
    raw = np.interp(x_new, x, y)
    smooth = safe_savgol(raw, cfg.smooth_window, cfg.smooth_polyorder)
    normalized = z_normalize(smooth)
    derivative = z_normalize(np.gradient(smooth))

    # 延迟导入避免 turning_points 与预处理形成模块循环。
    from .turning_points import detect_turning_points
    turns = detect_turning_points(normalized, cfg.turning_prominence)
    return QueryFeatures(
        raw=raw,
        smooth=smooth,
        normalized=normalized,
        derivative=derivative,
        turning_points=turns,
    )


def build_market_series(
    *,
    series_id: str,
    symbol: str,
    symbol_name: str,
    category: str,
    timeframe: str,
    timestamps: np.ndarray,
    values: np.ndarray,
    values_are_prices: bool,
    sample_id: int = 0,
    close_price: float = 0.0,
    ohlc_values: Optional[np.ndarray] = None,
    volume_values: Optional[np.ndarray] = None,
    turnover_values: Optional[np.ndarray] = None,
    metadata: Optional[Dict[str, Any]] = None,
    config: Optional[CpuSearchConfig] = None,
) -> MarketSeries:
    """把 close 或兼容的归一化走势转换成连续 log/smooth 市场序列。"""
    cfg = (config or CpuSearchConfig()).validate()
    source = np.asarray(values, dtype=np.float64).reshape(-1)
    ts = np.asarray(timestamps, dtype=np.int64).reshape(-1)
    if len(source) < 3 or len(ts) != len(source):
        raise ValueError("market values/timestamps length mismatch or too short")
    valid = np.isfinite(source) & np.isfinite(ts)
    ohlc = None
    if ohlc_values is not None:
        raw_ohlc = np.asarray(ohlc_values, dtype=np.float64)
        if raw_ohlc.ndim != 2 or raw_ohlc.shape != (len(source), 4):
            raise ValueError("ohlc_values must be an Nx4 array aligned with close values")
        valid &= np.isfinite(raw_ohlc).all(axis=1)
        ohlc = raw_ohlc[valid]
    volume = None
    if volume_values is not None:
        raw_volume = np.asarray(volume_values, dtype=np.float64).reshape(-1)
        if len(raw_volume) != len(source):
            raise ValueError("volume_values must align with source values")
        volume = raw_volume[valid]
    turnover = None
    if turnover_values is not None:
        raw_turnover = np.asarray(turnover_values, dtype=np.float64).reshape(-1)
        if len(raw_turnover) != len(source):
            raise ValueError("turnover_values must align with source values")
        turnover = raw_turnover[valid]
    source = source[valid]
    ts = ts[valid]
    if len(source) < 3:
        raise ValueError("market series has fewer than 3 valid values")
    if values_are_prices:
        log_price = np.log(np.clip(source, EPS, None))
    else:
        # 兼容 MOCK/旧索引中已经去价格量纲的 y 值。
        log_price = source.copy()
    smooth = safe_savgol(log_price, cfg.smooth_window, cfg.smooth_polyorder)
    return MarketSeries(
        series_id=str(series_id),
        symbol=str(symbol),
        symbol_name=str(symbol_name),
        category=str(category),
        timeframe=str(timeframe),
        timestamps=ts,
        source_values=source,
        log_price=log_price,
        smooth_price=smooth,
        sample_id=int(sample_id),
        close_price=float(close_price or source[-1]),
        ohlc_values=ohlc,
        volume_values=volume,
        turnover_values=turnover,
        metadata=dict(metadata or {}),
    )

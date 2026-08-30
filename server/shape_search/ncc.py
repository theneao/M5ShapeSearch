# -*- coding: utf-8 -*-
"""
多尺度 NCC 召回工具。

创建时间：2026-08-28
作用：使用 FFT 卷积与累计和计算滑窗 Price NCC，并提取相互分离的局部峰值。
使用方式：sliding_ncc(query, series)；geometric_lengths(...); select_local_peaks(...)
"""
from __future__ import annotations
from typing import List

import numpy as np
from scipy.signal import find_peaks


EPS = 1e-8


def sliding_ncc(query: np.ndarray, series: np.ndarray) -> np.ndarray:
    """返回每个合法窗口起点的 Pearson/NCC，常量窗口记为 0。"""
    q = np.asarray(query, dtype=np.float64).reshape(-1)
    t = np.asarray(series, dtype=np.float64).reshape(-1)
    m = len(q)
    if m < 2:
        raise ValueError("query length must be >= 2")
    if m > len(t):
        return np.empty(0, dtype=np.float64)

    conv_length = len(t) + m - 1
    fft_length = 1 << max(1, conv_length - 1).bit_length()
    convolution = np.fft.irfft(
        np.fft.rfft(t, fft_length) * np.fft.rfft(q[::-1], fft_length),
        fft_length,
    )[:conv_length]
    dot = convolution[m - 1:len(t)]

    cs = np.concatenate(([0.0], np.cumsum(t, dtype=np.float64)))
    cs2 = np.concatenate(([0.0], np.cumsum(t * t, dtype=np.float64)))
    sums = cs[m:] - cs[:-m]
    sums2 = cs2[m:] - cs2[:-m]
    means = sums / m
    variances = np.maximum(sums2 / m - means * means, 0.0)
    stds = np.sqrt(variances)

    q_mean = float(np.mean(q))
    q_std = float(np.std(q))
    numerator = dot - m * q_mean * means
    denominator = m * q_std * stds
    result = np.zeros_like(numerator)
    valid = denominator > EPS
    result[valid] = numerator[valid] / denominator[valid]
    return np.clip(result, -1.0, 1.0)


def pair_ncc(a: np.ndarray, b: np.ndarray) -> float:
    """两个等长序列的有符号 Pearson 相关系数；绝不取绝对值。"""
    x = np.asarray(a, dtype=np.float64).reshape(-1)
    y = np.asarray(b, dtype=np.float64).reshape(-1)
    if len(x) != len(y) or len(x) < 2:
        raise ValueError("pair_ncc requires equal-length sequences")
    sx = float(np.std(x))
    sy = float(np.std(y))
    if sx < EPS or sy < EPS:
        return 0.0
    return float(np.clip(np.mean((x - x.mean()) * (y - y.mean())) / (sx * sy), -1.0, 1.0))


def geometric_lengths(min_length: int, max_length: int, ratio: float) -> List[int]:
    """生成近似几何尺度，并保证合法上界只出现一次。"""
    lo = max(4, int(min_length))
    hi = max(lo, int(max_length))
    ratio = max(1.05, float(ratio))
    lengths = [lo]
    while lengths[-1] < hi:
        nxt = max(lengths[-1] + 1, int(round(lengths[-1] * ratio)))
        if nxt >= hi:
            if lengths[-1] != hi:
                lengths.append(hi)
            break
        lengths.append(nxt)
    return lengths


def select_local_peaks(
    scores: np.ndarray,
    min_score: float,
    min_distance: int,
    top_k: int,
    ensure_one: bool = True,
) -> np.ndarray:
    """使用 SciPy find_peaks 选择局部高点；必要时保留全局最强候选保证召回。"""
    values = np.asarray(scores, dtype=np.float64).reshape(-1)
    if len(values) == 0:
        return np.empty(0, dtype=np.int64)
    distance = max(1, int(min_distance))
    peaks, _props = find_peaks(values, distance=distance, height=float(min_score))
    # find_peaks 不把端点作为局部峰；端点满足阈值时也参与候选。
    endpoints = []
    if values[0] >= min_score and (len(values) == 1 or values[0] >= values[1]):
        endpoints.append(0)
    if len(values) > 1 and values[-1] >= min_score and values[-1] >= values[-2]:
        endpoints.append(len(values) - 1)
    if endpoints:
        peaks = np.unique(np.concatenate([peaks, np.asarray(endpoints, dtype=np.int64)]))
    if len(peaks) == 0 and ensure_one and np.isfinite(values).any():
        peaks = np.asarray([int(np.nanargmax(values))], dtype=np.int64)
    if len(peaks) == 0:
        return peaks.astype(np.int64)
    order = np.argsort(-values[peaks], kind="stable")
    return peaks[order[:max(1, int(top_k))]].astype(np.int64)


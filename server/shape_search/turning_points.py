# -*- coding: utf-8 -*-
"""
显著峰谷结构特征。

创建时间：2026-08-28
作用：使用 SciPy prominence 检测 Turning Points，并通过序列 DP 计算结构相似度。
使用方式：detect_turning_points(z_series, prominence); turning_similarity(query, candidate, config)
"""
from __future__ import annotations
from typing import List

import numpy as np
from scipy.signal import find_peaks

from .config import CpuSearchConfig
from .models import TurningPoint


def detect_turning_points(values: np.ndarray, prominence: float = 0.25) -> List[TurningPoint]:
    """在无 NaN 的 z-normalized 序列上检测峰和谷，并按时间合并。"""
    x = np.asarray(values, dtype=np.float64).reshape(-1)
    if len(x) < 3 or not np.isfinite(x).all():
        return []
    peaks, peak_props = find_peaks(x, prominence=float(prominence))
    valleys, valley_props = find_peaks(-x, prominence=float(prominence))
    points: List[TurningPoint] = []
    denom = max(len(x) - 1, 1)
    for idx, prom in zip(peaks, peak_props.get("prominences", [])):
        points.append(TurningPoint(int(idx), float(idx / denom), float(x[idx]), 1, float(prom)))
    for idx, prom in zip(valleys, valley_props.get("prominences", [])):
        points.append(TurningPoint(int(idx), float(idx / denom), float(x[idx]), -1, float(prom)))
    points.sort(key=lambda item: item.index)

    # 极短距离内同类型点只保留 prominence 更高者，降低噪声造成的重复峰/谷。
    merged: List[TurningPoint] = []
    min_gap = max(1, int(round(len(x) * 0.02)))
    for point in points:
        if merged and point.kind == merged[-1].kind and point.index - merged[-1].index <= min_gap:
            if point.prominence > merged[-1].prominence:
                merged[-1] = point
        else:
            merged.append(point)
    return merged


def turning_similarity(
    query_points: List[TurningPoint],
    candidate_points: List[TurningPoint],
    config: CpuSearchConfig,
) -> float:
    """对峰谷序列做带插入/删除代价的 DP 匹配，返回 [0,1] 相似度。"""
    q = query_points
    c = candidate_points
    if not q and not c:
        return 1.0
    if not q or not c:
        return float(np.exp(-config.turning_alpha))

    nq, nc = len(q), len(c)
    dp = np.full((nq + 1, nc + 1), np.inf, dtype=np.float64)
    dp[0, 0] = 0.0
    for i in range(1, nq + 1):
        dp[i, 0] = dp[i - 1, 0] + config.turning_gap_penalty
    for j in range(1, nc + 1):
        dp[0, j] = dp[0, j - 1] + config.turning_gap_penalty

    q_prom_scale = max(max(point.prominence for point in q), 1e-8)
    c_prom_scale = max(max(point.prominence for point in c), 1e-8)
    for i in range(1, nq + 1):
        qp = q[i - 1]
        for j in range(1, nc + 1):
            cp = c[j - 1]
            substitution = (
                config.turning_time_weight * abs(qp.time_norm - cp.time_norm)
                + config.turning_height_weight * min(abs(qp.height - cp.height) / 4.0, 1.0)
                + config.turning_prominence_weight * abs(
                    qp.prominence / q_prom_scale - cp.prominence / c_prom_scale
                )
                + (0.0 if qp.kind == cp.kind else config.turning_type_mismatch_penalty)
            )
            dp[i, j] = min(
                dp[i - 1, j - 1] + substitution,
                dp[i - 1, j] + config.turning_gap_penalty,
                dp[i, j - 1] + config.turning_gap_penalty,
            )
    distance = float(dp[nq, nc] / max(nq, nc, 1))
    return float(np.exp(-config.turning_alpha * distance))


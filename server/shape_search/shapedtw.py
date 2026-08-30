# -*- coding: utf-8 -*-
"""
Subsequence ShapeDTW 边界精排。

创建时间：2026-08-28
作用：以局部 level/derivative patch 为描述符，在扩展上下文中自动寻找匹配起止位置并保留 warping path。
使用方式：subsequence_shapedtw(query, reference, config, expected_length=L)
"""
from __future__ import annotations
from typing import List, Optional, Tuple

import numpy as np

from .config import CpuSearchConfig
from .preprocessing import z_normalize


def _patch_matrix(values: np.ndarray, radius: int) -> np.ndarray:
    """为序列每一点构建边缘复制的局部 patch。"""
    x = np.asarray(values, dtype=np.float64).reshape(-1)
    radius = max(1, int(radius))
    padded = np.pad(x, (radius, radius), mode="edge")
    return np.lib.stride_tricks.sliding_window_view(padded, 2 * radius + 1).copy()


def shape_cost_matrix(
    query: np.ndarray,
    reference: np.ndarray,
    config: CpuSearchConfig,
) -> np.ndarray:
    """构建 ShapeDTW 局部描述符代价矩阵。"""
    q = z_normalize(np.asarray(query, dtype=np.float64))
    r = z_normalize(np.asarray(reference, dtype=np.float64))
    qd = z_normalize(np.gradient(q))
    rd = z_normalize(np.gradient(r))
    q_level = _patch_matrix(q, config.descriptor_radius)
    r_level = _patch_matrix(r, config.descriptor_radius)
    q_derivative = _patch_matrix(qd, config.descriptor_radius)
    r_derivative = _patch_matrix(rd, config.descriptor_radius)
    level_cost = np.mean((q_level[:, None, :] - r_level[None, :, :]) ** 2, axis=2)
    derivative_cost = np.mean(
        (q_derivative[:, None, :] - r_derivative[None, :, :]) ** 2,
        axis=2,
    )
    return (
        config.shapedtw_level_weight * level_cost
        + config.shapedtw_derivative_weight * derivative_cost
    )


def subsequence_shapedtw(
    query: np.ndarray,
    reference: np.ndarray,
    config: Optional[CpuSearchConfig] = None,
    expected_length: Optional[int] = None,
) -> Tuple[float, int, int, List[Tuple[int, int]]]:
    """
    返回 (平均路径距离, start, end_exclusive, warping_path)。

    `dp[0, :] = 0` 允许 query 从 reference 任意位置开始；expected_length 与
    warping_ratio 限制最终子段长度，防止 DTW 把复杂走势压缩到极短片段。
    """
    cfg = (config or CpuSearchConfig()).validate()
    q = np.asarray(query, dtype=np.float64).reshape(-1)
    ref = np.asarray(reference, dtype=np.float64).reshape(-1)
    if len(q) < 3 or len(ref) < 3:
        raise ValueError("ShapeDTW query/reference need at least 3 points")

    local_cost = shape_cost_matrix(q, ref, cfg)
    m, n = local_cost.shape
    dp = np.full((m + 1, n + 1), np.inf, dtype=np.float64)
    path_len = np.zeros((m + 1, n + 1), dtype=np.int32)
    start_at = np.zeros((m + 1, n + 1), dtype=np.int32)
    parent = np.full((m + 1, n + 1), -1, dtype=np.int8)
    dp[0, :] = 0.0
    start_at[0, :] = np.arange(n + 1, dtype=np.int32)
    expected = max(3, int(expected_length or min(len(ref), len(q))))

    # 轻微惩罚非对角步，配合最终长度约束限制过度扭曲。
    warp_step_penalty = 0.02
    # 同一反对角线上的单元互不依赖，可一次用 NumPy 计算，避免逐 cell Python 循环。
    for diagonal in range(2, m + n + 1):
        i_values = np.arange(
            max(1, diagonal - n),
            min(m, diagonal - 1) + 1,
            dtype=np.int32,
        )
        if len(i_values) == 0:
            continue
        j_values = diagonal - i_values
        diagonal_costs = dp[i_values - 1, j_values - 1]
        up_costs = dp[i_values - 1, j_values] + warp_step_penalty
        left_costs = dp[i_values, j_values - 1] + warp_step_penalty
        options = np.vstack([diagonal_costs, up_costs, left_costs])
        choices = np.argmin(options, axis=0).astype(np.int8)
        selected_costs = options[choices, np.arange(len(i_values))]
        dp[i_values, j_values] = local_cost[i_values - 1, j_values - 1] + selected_costs

        previous_i = i_values - (choices != 2).astype(np.int32)
        previous_j = j_values - (choices != 1).astype(np.int32)
        path_len[i_values, j_values] = path_len[previous_i, previous_j] + 1
        start_at[i_values, j_values] = start_at[previous_i, previous_j]
        parent[i_values, j_values] = choices

        # 动态起点版 Sakoe-Chiba 约束：相对 query/reference 进度偏差不得超过 warping_ratio。
        reference_progress = (
            j_values - start_at[i_values, j_values]
        ) / max(expected, 1)
        query_progress = i_values / max(m, 1)
        band_slack = max(1.0 / max(m, 1), 1.0 / max(expected, 1))
        outside_band = np.abs(reference_progress - query_progress) > (
            cfg.warping_ratio + band_slack
        )
        if np.any(outside_band):
            invalid_i = i_values[outside_band]
            invalid_j = j_values[outside_band]
            dp[invalid_i, invalid_j] = np.inf
            path_len[invalid_i, invalid_j] = 0
            parent[invalid_i, invalid_j] = -1

    min_duration = max(3, int(round(expected * (1.0 - cfg.warping_ratio))))
    max_duration = min(n, int(round(expected * (1.0 + cfg.warping_ratio))))
    valid_ends = []
    normalized_costs = []
    for j in range(1, n + 1):
        duration = j - int(start_at[m, j])
        if min_duration <= duration <= max_duration and path_len[m, j] > 0:
            valid_ends.append(j)
            normalized_costs.append(float(dp[m, j] / path_len[m, j]))
    if not valid_ends:
        for j in range(1, n + 1):
            if path_len[m, j] > 0:
                valid_ends.append(j)
                normalized_costs.append(float(dp[m, j] / path_len[m, j]))
    best_index = int(np.argmin(normalized_costs))
    best_end = int(valid_ends[best_index])
    distance = float(normalized_costs[best_index])

    path: List[Tuple[int, int]] = []
    i, j = m, best_end
    while i > 0 and j > 0:
        path.append((i - 1, j - 1))
        choice = int(parent[i, j])
        if choice == 0:
            i -= 1
            j -= 1
        elif choice == 1:
            i -= 1
        elif choice == 2:
            j -= 1
        else:
            break
    path.reverse()
    if not path:
        return distance, max(0, best_end - expected), best_end, []
    start = min(point[1] for point in path)
    end_exclusive = max(point[1] for point in path) + 1
    return distance, int(start), int(end_exclusive), path

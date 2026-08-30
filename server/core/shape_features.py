# -*- coding: utf-8 -*-
"""
形态特征提取模块
========================================
与设备端 C++ 代码 100% 对齐的特征提取流水线：
1. 轨迹去抖（3次滑动平均）
2. Ramer-Douglas-Peucker 特征点压缩
3. 等距线性重采样至固定长度 N=128
4. Min-Max 归一化到 [0,1]x[0,1]
5. 提取 16维 统计特征增强
6. 拼接为 303维特征向量并做 L2 归一化

修改记录：
- 2026-08-27：用绝对标尺的多指标形态距离替换候选集内 Min-Max 评分，避免最优候选被强制显示为 100%。
- 2026-08-28：增加批量向量化受限 DTW 精排，在不改变评分公式的前提下降低大候选集延迟。
使用方式：re_rank_by_fused(...) 保持兼容，默认返回校准后的绝对相似度；return_details=True 可取得分项距离。
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import List, Tuple, Optional
import numpy as np


Point = Tuple[float, float]  # (x, y)


# ============================================================
# 1. 三次滑动平均去抖
# ============================================================
def moving_average_filter(points: np.ndarray, window: int = 3) -> np.ndarray:
    """三次滑动平均，去抖触屏噪声。points: shape (N,2) [x,y]"""
    if len(points) < window:
        return points.copy()
    w = np.ones(window) / window
    xs = np.convolve(points[:, 0], w, mode="same")
    ys = np.convolve(points[:, 1], w, mode="same")
    out = np.column_stack([xs, ys])
    # 边界保持原值（卷积边界衰减修复）
    head = window // 2
    out[:head] = points[:head]
    out[-head:] = points[-head:]
    return out


# ============================================================
# 2. Ramer-Douglas-Peucker (RDP) 特征点压缩
# ============================================================
def _perpendicular_distance(pt: np.ndarray, start: np.ndarray, end: np.ndarray) -> float:
    if np.allclose(start, end):
        return float(np.linalg.norm(pt - start))
    line_vec = end - start
    line_len = np.linalg.norm(line_vec)
    line_unitvec = line_vec / line_len
    pt_vec_scaled = (pt - start) / line_len
    t = np.dot(line_unitvec, pt_vec_scaled)
    t = max(0.0, min(1.0, t))
    nearest = start + t * line_vec
    return float(np.linalg.norm(pt - nearest))


def douglas_peucker(points: np.ndarray, epsilon: float = 0.008) -> np.ndarray:
    """RDP 压缩。points 需已归一化 [0,1]，epsilon 默认 0.008。"""
    if len(points) <= 2:
        return points.copy()
    max_dist = 0.0
    index = -1
    end = len(points) - 1
    for i in range(1, end):
        dist = _perpendicular_distance(points[i], points[0], points[end])
        if dist > max_dist:
            max_dist = dist
            index = i
    if max_dist > epsilon:
        left = douglas_peucker(points[: index + 1], epsilon)
        right = douglas_peucker(points[index:], epsilon)
        return np.vstack([left[:-1], right])
    else:
        return np.vstack([points[0], points[end]])


# ============================================================
# 3. 等距线性重采样至固定长度 N
# ============================================================
def resample_fixed_length(points: np.ndarray, target_n: int = 128) -> np.ndarray:
    """沿着路径累计距离等距重采样到 target_n 个点。"""
    if len(points) < 2:
        raise ValueError("points must have >=2 points for resample")
    diffs = np.diff(points, axis=0)
    dists = np.linalg.norm(diffs, axis=1)
    cum_dists = np.concatenate([[0.0], np.cumsum(dists)])
    total = cum_dists[-1]
    if total <= 1e-8:
        return np.tile(points[0], (target_n, 1)).reshape(target_n, 2)
    step = total / (target_n - 1)
    targets = np.arange(target_n) * step
    xs = np.interp(targets, cum_dists, points[:, 0])
    ys = np.interp(targets, cum_dists, points[:, 1])
    return np.column_stack([xs, ys])


# ============================================================
# 4. Min-Max 归一化 [0,1]x[0,1]
# ============================================================
def normalize_minmax(points: np.ndarray, y_flip: bool = True) -> np.ndarray:
    """x,y 分别 min-max 到 [0,1]。y_flip=True 让 y 向上增大（贴合手绘习惯）。"""
    xs = points[:, 0]
    ys = points[:, 1]
    x_min, x_max = xs.min(), xs.max()
    y_min, y_max = ys.min(), ys.max()
    x_r = 1.0 if np.isclose(x_max, x_min) else (x_max - x_min)
    y_r = 1.0 if np.isclose(y_max, y_min) else (y_max - y_min)
    nx = (xs - x_min) / x_r
    ny = (ys - y_min) / y_r
    if y_flip:
        ny = 1.0 - ny
    return np.column_stack([nx, ny])


# ============================================================
# 5. 16维 统计特征
# ============================================================
@dataclass
class ShapeStats16:
    start_slope: float
    end_slope: float
    max_point_idx: int
    min_point_idx: int
    volatility: float
    skewness: float
    kurtosis: float
    peak_count: int
    valley_count: int
    trend_slope: float
    trend_r2: float
    first_half_mean: float
    second_half_mean: float
    max_drawdown: float
    max_runup: float
    total_displacement: float

    def to_list(self) -> List[float]:
        d = asdict(self)
        return [float(d[k]) for k in [
            "start_slope", "end_slope", "max_point_idx", "min_point_idx",
            "volatility", "skewness", "kurtosis", "peak_count", "valley_count",
            "trend_slope", "trend_r2", "first_half_mean", "second_half_mean",
            "max_drawdown", "max_runup", "total_displacement"
        ]]


def extract_16d_stats(seq_128: np.ndarray) -> ShapeStats16:
    """对 128x2 的归一化序列提取 16维统计特征。"""
    ys = seq_128[:, 1]
    N = len(seq_128)  # 128
    assert N >= 10

    # 起点/终点局部斜率（取前/后5点线性回归）
    def _slope(win_y: np.ndarray) -> float:
        t = np.arange(len(win_y), dtype=float)
        if np.std(t) < 1e-6:
            return 0.0
        return float(np.polyfit(t, win_y, 1)[0])

    start_slope = _slope(ys[:6])
    end_slope = _slope(ys[-6:])

    # 最高点/最低点索引
    max_point_idx = int(np.argmax(ys))
    min_point_idx = int(np.argmin(ys))

    # 波动率（标准差）
    volatility = float(np.std(ys))

    # 偏度 / 峰度（手动实现，避免scipy依赖）
    mean = np.mean(ys)
    std = volatility if volatility > 1e-8 else 1.0
    z = (ys - mean) / std
    skewness = float(np.mean(z ** 3))
    kurtosis = float(np.mean(z ** 4) - 3.0)

    # 峰数 / 谷数（>前后两邻）
    peak_count = int(np.sum((ys[1:-1] > ys[:-2]) & (ys[1:-1] > ys[2:])))
    valley_count = int(np.sum((ys[1:-1] < ys[:-2]) & (ys[1:-1] < ys[2:])))

    # 线性回归斜率 + R²
    t = np.arange(N, dtype=float)
    trend_slope, intercept = np.polyfit(t, ys, 1)
    trend_slope = float(trend_slope)
    y_pred = trend_slope * t + intercept
    ss_res = float(np.sum((ys - y_pred) ** 2))
    ss_tot = float(np.sum((ys - np.mean(ys)) ** 2))
    trend_r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-8 else 0.0
    trend_r2 = float(max(0.0, min(1.0, trend_r2)))

    # 前/后半段 y 均值
    half = N // 2
    first_half_mean = float(np.mean(ys[:half]))
    second_half_mean = float(np.mean(ys[half:]))

    # 最大回撤 / 最大涨幅
    running_max = np.maximum.accumulate(ys)
    drawdown = (running_max - ys) / (running_max + 1e-8)
    max_drawdown = float(np.max(drawdown))
    running_min = np.minimum.accumulate(ys)
    runup = (ys - running_min) / (running_min + 1e-8)
    max_runup = float(np.max(runup))

    total_displacement = float(ys[-1] - ys[0])

    # 归一化索引到 [0,127]
    return ShapeStats16(
        start_slope=float(start_slope),
        end_slope=float(end_slope),
        max_point_idx=max_point_idx,
        min_point_idx=min_point_idx,
        volatility=volatility,
        skewness=skewness,
        kurtosis=kurtosis,
        peak_count=peak_count,
        valley_count=valley_count,
        trend_slope=trend_slope,
        trend_r2=trend_r2,
        first_half_mean=first_half_mean,
        second_half_mean=second_half_mean,
        max_drawdown=max_drawdown,
        max_runup=max_runup,
        total_displacement=total_displacement,
    )


# ============================================================
# 6. 打包成 287+16=303 维特征向量（⭐v3最终优化版⭐）
#  A. 128维 : y值序列（已重采样128点，x轴隐含在索引里）
#  B. 127维 : 一阶差分 dy[i] = y[i+1]-y[i]（走势方向）
#  C.  32维 : 16块×2 局部块特征（每块 8个点：均值 + 平均斜率，捕捉三角/横盘/阶梯局部形态）
#  D.  16维 : 全局统计（峰/谷/趋势/回撤等，硬过滤关键依据）
# 合计 = 128+127+32+16 = 303 维
# ============================================================
VECTOR_DIM = 303


_BLOCKS = 16  # 128/8 = 16 块


def _block_features(ys_128: np.ndarray) -> np.ndarray:
    """将128点y序列分16块，每块8个点，提取 [块均值, 块内线性斜率] 共 16×2=32 维。
    解决：三角收敛/发散/横盘震荡/阶梯 这类整体形态相似但局部结构不同的分类问题。"""
    ys = ys_128.astype(np.float32)
    blk = ys.reshape(_BLOCKS, -1)  # (16, 8)
    # 块均值
    means = blk.mean(axis=1).astype(np.float32)  # (16,)
    # 块内线性斜率（polyfit 一阶）
    slopes = np.empty(_BLOCKS, dtype=np.float32)
    t = np.arange(blk.shape[1], dtype=np.float32)
    for i in range(_BLOCKS):
        y = blk[i]
        if np.std(y) < 1e-6:
            slopes[i] = 0.0
        else:
            slopes[i] = np.polyfit(t, y, 1)[0]
    # 合并 16块 × [均值, 斜率] → 32维
    feat = np.empty(_BLOCKS * 2, dtype=np.float32)
    feat[0:2*_BLOCKS:2] = (means - 0.5) * 3.0   # 偶数位: 块均值(中心缩放)
    feat[1:2*_BLOCKS:2] = slopes * 50.0          # 奇数位: 块斜率(放大)
    return feat


def build_303d_vector(seq_128: np.ndarray, stats: ShapeStats16) -> np.ndarray:
    ys = seq_128[:, 1].astype(np.float32)
    dys = np.diff(ys, n=1) * 5.0          # 差分 B
    block32 = _block_features(ys) * 3.0   # ⭐ 3倍平衡局部（三角/横盘）与全局（M/W/V）

    # 全局统计 16 维，手动加权（区分度越高权重越大）
    stat_list = np.array(stats.to_list(), dtype=np.float32)
    stat_scales = np.array([
        18.0, 18.0,   # start/end_mean 类
        3.5,  3.5,    # max_idx/min_idx
        30.0, 6.0,    # ⭐ volatility * 2 放大（横盘 vs 三角 核心区分）
        6.0,          # kurtosis
        50.0, 50.0,   # peak_count / valley_count
        25.0,         # trend_slope
        6.0,          # trend_r2
        7.0, 7.0,     # first/second_half_mean
        10.0, 10.0,   # max_drawdown / max_runup
        15.0,         # total_displacement
    ], dtype=np.float32)
    stat_list[2] /= 127.0
    stat_list[3] /= 127.0
    stat_list[7] = np.clip(stat_list[7] / 3.0, 0, 3.0)
    stat_list[8] = np.clip(stat_list[8] / 3.0, 0, 3.0)
    stat_vec = stat_list * stat_scales * 1.5

    # 拼接总向量 303 维
    vec = np.concatenate([ys, dys, block32, stat_vec]).astype(np.float32)
    norm = np.linalg.norm(vec)
    if norm > 1e-8:
        vec = vec / norm
    return vec


# ============================================================
# 端到端：原始点 → (128序列, 16特征, 303维向量)
# ============================================================
def full_pipeline(
    raw_points: List[Point],
    target_n: int = 128,
    dp_epsilon: float = 0.008,
    y_flip: bool = False,
) -> Tuple[np.ndarray, ShapeStats16, np.ndarray]:
    pts = np.asarray(raw_points, dtype=np.float32).reshape(-1, 2)
    if len(pts) < 2:
        raise ValueError("raw_points need >= 2 points")
    pts = moving_average_filter(pts, 3)
    if pts.max() > 1.01 or pts.min() < -0.01:
        pts = normalize_minmax(pts, y_flip=y_flip)
    pts_dp = douglas_peucker(pts, dp_epsilon)
    if len(pts_dp) < 3:
        pts_dp = pts
    seq_128 = resample_fixed_length(pts_dp, target_n)
    seq_128 = normalize_minmax(seq_128, y_flip=y_flip)
    stats = extract_16d_stats(seq_128)
    vec = build_303d_vector(seq_128, stats)
    return seq_128, stats, vec


# ============================================================
# 精排模块：绝对标尺多指标形态距离
# 策略：
#   A. 64点 Sakoe-Chiba 约束 DTW：允许小幅横向错位
#   B. 同时间点 RMSE：惩罚 DTW 过度拉伸后看似相似
#   C. 导数 DTW：比较涨跌方向、拐点和局部斜率
#   D. Pearson 相关距离：区分同向、无关和反向走势
#   E. 使用固定绝对标尺映射分数，不根据本批候选的最好/最差值做 Min-Max
# ============================================================
def _resample_1d(ys_long: np.ndarray, target_n: int) -> np.ndarray:
    """将一维序列等距重采样到固定长度。"""
    ys = np.asarray(ys_long, dtype=np.float64).ravel()
    if len(ys) == target_n:
        return ys.copy()
    if len(ys) < 2:
        return np.full(target_n, float(ys[0]) if len(ys) else 0.0, dtype=np.float64)
    n = len(ys)
    x_old = np.linspace(0, 1, n)
    x_new = np.linspace(0, 1, target_n)
    return np.interp(x_new, x_old, ys).astype(np.float64)


def constrained_dtw_distance(
    a: np.ndarray,
    b: np.ndarray,
    target_n: int = 64,
    band: int = 6,
) -> float:
    """固定长度、受限窗口的 L1-DTW 平均距离；越小越相似。"""
    ya = _resample_1d(a, target_n)
    yb = _resample_1d(b, target_n)
    n, m = len(ya), len(yb)
    band = max(int(band), abs(n - m))
    diff = np.abs(ya[:, None] - yb[None, :])
    inf = 1e18
    costs = np.full((n + 1, m + 1), inf, dtype=np.float64)
    costs[0, 0] = 0.0
    for i in range(1, n + 1):
        j_lo = max(1, i - band)
        j_hi = min(m, i + band) + 1
        for j in range(j_lo, j_hi):
            costs[i, j] = diff[i - 1, j - 1] + min(
                costs[i - 1, j],
                costs[i, j - 1],
                costs[i - 1, j - 1],
            )
    return float(costs[n, m] / max(n, m))


def classic_dtw_32(a: np.ndarray, b: np.ndarray, band: int = 3) -> float:
    """
    经典 DTW（L1 距离 + Sakoe-Chiba 带约束 band）+ 32 点降采样。
    band=3: 对齐时允许偏移不超过 3 个 32分位 = 约 12 个 128分位，约 9% 容错
    返回：平均每点距离（归一化，越小越相似，范围≈[0,0.5]）
    """
    return constrained_dtw_distance(a, b, target_n=32, band=band)


def calibrated_shape_similarity(
    query_y: np.ndarray,
    candidate_y: np.ndarray,
) -> Tuple[float, dict]:
    """
    计算可跨候选比较的绝对形态相似度。

    分数不是命中概率；只有两条归一化序列几乎完全一致时才接近 1。
    固定标尺基于 y∈[0,1]：DTW 0.35、RMSE 0.50、导数DTW 0.02 分别视为明显差异。
    """
    q = _resample_1d(query_y, 64)
    c = _resample_1d(candidate_y, 64)

    dtw_distance = constrained_dtw_distance(q, c, target_n=64, band=6)
    point_rmse = float(np.sqrt(np.mean((q - c) ** 2)))
    derivative_dtw = constrained_dtw_distance(
        np.gradient(q), np.gradient(c), target_n=64, band=4
    )

    q_std = float(np.std(q))
    c_std = float(np.std(c))
    if q_std < 1e-9 or c_std < 1e-9:
        correlation = 1.0 if point_rmse < 1e-9 else 0.0
    else:
        correlation = float(np.clip(np.corrcoef(q, c)[0, 1], -1.0, 1.0))
    correlation_distance = (1.0 - correlation) / 2.0

    # 各项先除以固定的“明显不相似”尺度，再加权；上限2避免单项完全淹没其它证据。
    dtw_scaled = min(dtw_distance / 0.35, 2.0)
    rmse_scaled = min(point_rmse / 0.50, 2.0)
    derivative_scaled = min(derivative_dtw / 0.02, 2.0)
    absolute_distance = (
        0.38 * dtw_scaled
        + 0.30 * rmse_scaled
        + 0.20 * derivative_scaled
        + 0.12 * correlation_distance
    )
    score = float(np.exp(-1.60 * absolute_distance))

    details = {
        "dtw_distance": float(dtw_distance),
        "point_rmse": float(point_rmse),
        "derivative_dtw": float(derivative_dtw),
        "correlation": float(correlation),
        "absolute_distance": float(absolute_distance),
    }
    return score, details


def _batch_constrained_dtw(
    query: np.ndarray,
    candidates: np.ndarray,
    band: int,
) -> np.ndarray:
    """对一个查询和多个等长候选计算受限 L1-DTW，候选维使用 NumPy 并行。"""
    q = np.asarray(query, dtype=np.float64).reshape(-1)
    c = np.asarray(candidates, dtype=np.float64)
    if c.ndim != 2 or c.shape[1] != len(q):
        raise ValueError("candidates must be a 2D matrix with the same width as query")
    count, length = c.shape
    if count == 0:
        return np.empty(0, dtype=np.float64)

    band = max(int(band), 0)
    diff = np.abs(q[None, :, None] - c[:, None, :])
    costs = np.full((count, length + 1, length + 1), np.inf, dtype=np.float64)
    costs[:, 0, 0] = 0.0
    for i in range(1, length + 1):
        j_lo = max(1, i - band)
        j_hi = min(length, i + band) + 1
        for j in range(j_lo, j_hi):
            previous = np.minimum(
                np.minimum(costs[:, i - 1, j], costs[:, i, j - 1]),
                costs[:, i - 1, j - 1],
            )
            costs[:, i, j] = diff[:, i - 1, j - 1] + previous
    return costs[:, length, length] / max(length, 1)


def calibrated_shape_similarity_batch(
    query_y: np.ndarray,
    candidate_y_list,
) -> Tuple[np.ndarray, List[dict]]:
    """
    批量计算与 calibrated_shape_similarity 相同的绝对多指标分数。

    用法：scores, details = calibrated_shape_similarity_batch(query_y, candidate_y_list)。
    """
    if candidate_y_list is None or len(candidate_y_list) == 0:
        return np.empty(0, dtype=np.float64), []

    q = _resample_1d(query_y, 64)
    candidates = np.vstack([
        _resample_1d(candidate_y, 64)
        for candidate_y in candidate_y_list
    ]).astype(np.float64)

    dtw_distances = _batch_constrained_dtw(q, candidates, band=6)
    point_rmses = np.sqrt(np.mean((candidates - q[None, :]) ** 2, axis=1))
    query_derivative = np.gradient(q)
    candidate_derivatives = np.gradient(candidates, axis=1)
    derivative_dtws = _batch_constrained_dtw(
        query_derivative,
        candidate_derivatives,
        band=4,
    )

    q_centered = q - np.mean(q)
    c_centered = candidates - np.mean(candidates, axis=1, keepdims=True)
    q_norm = float(np.linalg.norm(q_centered))
    c_norms = np.linalg.norm(c_centered, axis=1)
    correlations = np.zeros(len(candidates), dtype=np.float64)
    valid_corr = (q_norm >= 1e-9) & (c_norms >= 1e-9)
    correlations[valid_corr] = (
        c_centered[valid_corr] @ q_centered
    ) / (c_norms[valid_corr] * q_norm)
    both_constant_and_equal = (
        (q_norm < 1e-9)
        & (c_norms < 1e-9)
        & (point_rmses < 1e-9)
    )
    correlations[both_constant_and_equal] = 1.0
    correlations = np.clip(correlations, -1.0, 1.0)

    dtw_scaled = np.minimum(dtw_distances / 0.35, 2.0)
    rmse_scaled = np.minimum(point_rmses / 0.50, 2.0)
    derivative_scaled = np.minimum(derivative_dtws / 0.02, 2.0)
    correlation_distances = (1.0 - correlations) / 2.0
    absolute_distances = (
        0.38 * dtw_scaled
        + 0.30 * rmse_scaled
        + 0.20 * derivative_scaled
        + 0.12 * correlation_distances
    )
    scores = np.exp(-1.60 * absolute_distances)

    details = [
        {
            "dtw_distance": float(dtw_distances[i]),
            "point_rmse": float(point_rmses[i]),
            "derivative_dtw": float(derivative_dtws[i]),
            "correlation": float(correlations[i]),
            "absolute_distance": float(absolute_distances[i]),
        }
        for i in range(len(candidates))
    ]
    return scores, details


def re_rank_by_fused(query_seq_128: np.ndarray,
                     candidate_seq_128_list,
                     query_vec: np.ndarray,
                     candidate_vec_list,
                     alpha: float = 0.72,
                     return_details: bool = False):
    """
    兼容入口：使用绝对标尺多指标距离精排。

    alpha 参数仅为兼容旧调用保留，不再参与计算。FAISS 余弦只负责粗召回，
    不再直接解释成百分比。return_details=True 时返回 (scores, details)。
    """
    n = len(candidate_vec_list)
    cos_sims = np.zeros(n, dtype=np.float64)
    query_vec_flat = np.asarray(query_vec, dtype=np.float64).ravel()
    for i, cv in enumerate(candidate_vec_list):
        cos_sims[i] = float(np.dot(query_vec_flat, np.asarray(cv, dtype=np.float64).ravel()))
    cos_sims = np.clip(cos_sims, -1.0, 1.0)

    if candidate_seq_128_list is None or len(candidate_seq_128_list) == 0:
        fallback = np.clip(cos_sims, 0.0, 1.0)
        return (fallback, []) if return_details else fallback

    qy = query_seq_128[:, 1]
    scores = np.zeros(n, dtype=np.float64)
    details = []
    for i, cs in enumerate(candidate_seq_128_list):
        cy = cs[:, 1]
        scores[i], item = calibrated_shape_similarity(qy, cy)
        item["faiss_cosine"] = float(cos_sims[i])
        details.append(item)

    return (scores, details) if return_details else scores

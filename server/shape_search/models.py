# -*- coding: utf-8 -*-
"""
CPU 形态搜索数据结构。

创建时间：2026-08-28
作用：定义 Query、连续市场序列、候选和峰谷结构，供各算法模块共享。
使用方式：算法模块直接导入对应 dataclass，不使用无结构 dict 传递中间结果。

修改时间：2026-08-28
修改作用：MarketSeries 可携带原始 OHLC，匹配计算仍只使用 close。

修改时间：2026-09-15
修改作用：MarketSeries 增加与 K 线严格对齐的成交量和成交额序列，供 Sequoia-X 预设策略执行真实量价条件。
使用方式：缺少真实字段时保持 None/NaN，策略明确报告不可计算，不用估算值替代。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


@dataclass
class TurningPoint:
    index: int
    time_norm: float
    height: float
    kind: int
    prominence: float


@dataclass
class QueryFeatures:
    raw: np.ndarray
    smooth: np.ndarray
    normalized: np.ndarray
    derivative: np.ndarray
    turning_points: List[TurningPoint]


@dataclass
class MarketSeries:
    series_id: str
    symbol: str
    symbol_name: str
    category: str
    timeframe: str
    timestamps: np.ndarray
    source_values: np.ndarray
    log_price: np.ndarray
    smooth_price: np.ndarray
    sample_id: int = 0
    close_price: float = 0.0
    ohlc_values: Optional[np.ndarray] = None
    volume_values: Optional[np.ndarray] = None
    turnover_values: Optional[np.ndarray] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    series_id: str
    symbol: str
    scale: int
    coarse_start: int
    coarse_end: int
    price_ncc: float
    derivative_ncc: float = 0.0
    pre_score: float = 0.0
    turning_score: float = 0.0
    stage1_score: float = 0.0
    refined_start: Optional[int] = None
    refined_end: Optional[int] = None
    shapedtw_distance: Optional[float] = None
    shapedtw_score: Optional[float] = None
    final_score: Optional[float] = None
    warping_path: List[Tuple[int, int]] = field(default_factory=list)
    supporting_scales: int = 1

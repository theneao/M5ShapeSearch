# -*- coding: utf-8 -*-
"""
CPU 形态搜索参数。

创建时间：2026-08-28
作用：集中保存规格 v0.1 中的查询、候选、ShapeDTW、融合和去重参数。
使用方式：config = CpuSearchConfig(); CpuShapeSearchEngine(config)

修改时间：2026-08-31
修改作用：将转折点/ShapeDTW 精排上限收敛到 240/60，避免 600 标的查询超过硬件 HTTP 超时；
          NCC 全市场召回和四项最终评分不变。
使用方式：沿用 CpuSearchConfig() 默认值即可；需要离线评测时仍可显式覆盖候选上限。
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class CpuSearchConfig:
    """CPU-only 多阶段搜索默认配置；字段可由 UI 或部署配置覆盖。"""

    query_resample_points: int = 128
    smooth_window: int = 7
    smooth_polyorder: int = 2

    min_length: int = 24
    max_length: int = 300
    scale_ratio: float = 1.30

    ncc_min_score: float = 0.55
    top_per_scale_per_series: int = 20
    before_turning_point: int = 240
    before_dtw: int = 60

    turning_prominence: float = 0.25
    turning_time_weight: float = 0.35
    turning_height_weight: float = 0.35
    turning_prominence_weight: float = 0.15
    turning_type_mismatch_penalty: float = 1.0
    turning_gap_penalty: float = 0.60
    turning_alpha: float = 2.0

    context_ratio: float = 0.40
    descriptor_radius: int = 3
    shapedtw_level_weight: float = 0.65
    shapedtw_derivative_weight: float = 0.35
    warping_ratio: float = 0.15
    dtw_similarity_temperature: float = 1.0

    stage1_price_weight: float = 0.55
    stage1_derivative_weight: float = 0.30
    stage1_turning_weight: float = 0.15

    final_price_weight: float = 0.30
    final_derivative_weight: float = 0.20
    final_turning_weight: float = 0.15
    final_shapedtw_weight: float = 0.35

    interval_iou_threshold: float = 0.65
    scale_bonus_eta: float = 0.02
    ensure_one_candidate_per_scale: bool = True

    def validate(self) -> "CpuSearchConfig":
        """规范化边界并验证权重，返回自身以便链式使用。"""
        self.query_resample_points = max(16, int(self.query_resample_points))
        self.smooth_window = max(3, int(self.smooth_window))
        if self.smooth_window % 2 == 0:
            self.smooth_window += 1
        self.smooth_polyorder = max(1, min(int(self.smooth_polyorder), self.smooth_window - 1))
        self.min_length = max(4, int(self.min_length))
        self.max_length = max(self.min_length, int(self.max_length))
        self.scale_ratio = max(1.05, float(self.scale_ratio))
        self.top_per_scale_per_series = max(1, int(self.top_per_scale_per_series))
        self.before_turning_point = max(1, int(self.before_turning_point))
        self.before_dtw = max(1, min(int(self.before_dtw), self.before_turning_point))
        self.descriptor_radius = max(1, int(self.descriptor_radius))
        self.warping_ratio = min(max(float(self.warping_ratio), 0.05), 0.50)
        return self


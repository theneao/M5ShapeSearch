# -*- coding: utf-8 -*-
"""
CPU-only 多阶段连续序列检索引擎。

创建时间：2026-08-28
作用：编排 Multi-scale NCC、Derivative NCC、Turning Point、Subsequence ShapeDTW 与 Interval NMS。
使用方式：engine.search(sketch_points, market_series, category="all", timeframe="1d", top_k=20)
"""
from __future__ import annotations
import math
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

from .config import CpuSearchConfig
from .models import Candidate, MarketSeries, QueryFeatures
from .ncc import geometric_lengths, pair_ncc, select_local_peaks, sliding_ncc
from .preprocessing import preprocess_sketch, resample_1d, z_normalize
from .shapedtw import subsequence_shapedtw
from .turning_points import detect_turning_points, turning_similarity


LogCallback = Callable[[str], None]


def interval_iou(a_start: int, a_end: int, b_start: int, b_end: int) -> float:
    """半开区间 IoU。"""
    intersection = max(0, min(a_end, b_end) - max(a_start, b_start))
    union = max(a_end, b_end) - min(a_start, b_start)
    return float(intersection / union) if union > 0 else 0.0


class CpuShapeSearchEngine:
    """规格 v0.1 的可解释 CPU 检索实现。"""

    def __init__(self, config: Optional[CpuSearchConfig] = None):
        self.config = (config or CpuSearchConfig()).validate()

    def _recall_series(
        self,
        query: QueryFeatures,
        market: MarketSeries,
    ) -> List[Candidate]:
        cfg = self.config
        max_length = min(cfg.max_length, len(market.smooth_price))
        if max_length < cfg.min_length:
            return []
        lengths = geometric_lengths(cfg.min_length, max_length, cfg.scale_ratio)
        candidates: List[Candidate] = []
        for length in lengths:
            query_price = z_normalize(resample_1d(query.normalized, length))
            price_scores = sliding_ncc(query_price, market.smooth_price)
            starts = select_local_peaks(
                price_scores,
                min_score=cfg.ncc_min_score,
                min_distance=max(3, int(length * 0.25)),
                top_k=cfg.top_per_scale_per_series,
                ensure_one=cfg.ensure_one_candidate_per_scale,
            )
            query_derivative = z_normalize(resample_1d(query.derivative, length))
            for start_value in starts:
                start = int(start_value)
                end = start + length
                segment = market.smooth_price[start:end]
                segment_derivative = z_normalize(np.gradient(segment))
                derivative_ncc = pair_ncc(query_derivative, segment_derivative)
                price_ncc = float(price_scores[start])
                pre_score = 0.65 * price_ncc + 0.35 * derivative_ncc
                candidates.append(Candidate(
                    series_id=market.series_id,
                    symbol=market.symbol,
                    scale=length,
                    coarse_start=start,
                    coarse_end=end,
                    price_ncc=price_ncc,
                    derivative_ncc=derivative_ncc,
                    pre_score=float(pre_score),
                ))
        return candidates

    def _apply_turning_stage(
        self,
        query: QueryFeatures,
        candidates: List[Candidate],
        market_by_id: Dict[str, MarketSeries],
    ) -> List[Candidate]:
        cfg = self.config
        candidates.sort(key=lambda item: item.pre_score, reverse=True)
        selected = candidates[:cfg.before_turning_point]
        for candidate in selected:
            market = market_by_id[candidate.series_id]
            segment = z_normalize(
                market.smooth_price[candidate.coarse_start:candidate.coarse_end]
            )
            turns = detect_turning_points(segment, cfg.turning_prominence)
            candidate.turning_score = turning_similarity(
                query.turning_points,
                turns,
                cfg,
            )
            candidate.stage1_score = float(
                cfg.stage1_price_weight * candidate.price_ncc
                + cfg.stage1_derivative_weight * candidate.derivative_ncc
                + cfg.stage1_turning_weight * candidate.turning_score
            )
        selected.sort(key=lambda item: item.stage1_score, reverse=True)
        return selected[:cfg.before_dtw]

    def _apply_shapedtw_stage(
        self,
        query: QueryFeatures,
        candidates: List[Candidate],
        market_by_id: Dict[str, MarketSeries],
    ) -> List[Candidate]:
        cfg = self.config
        completed: List[Candidate] = []
        for candidate in candidates:
            market = market_by_id[candidate.series_id]
            pad = int(round(cfg.context_ratio * candidate.scale))
            left = max(0, candidate.coarse_start - pad)
            right = min(len(market.smooth_price), candidate.coarse_end + pad)
            context = z_normalize(market.smooth_price[left:right])
            try:
                distance, local_start, local_end, path = subsequence_shapedtw(
                    query.normalized,
                    context,
                    cfg,
                    expected_length=candidate.scale,
                )
            except (ValueError, IndexError, FloatingPointError):
                continue
            candidate.refined_start = left + local_start
            candidate.refined_end = left + local_end
            candidate.shapedtw_distance = float(distance)
            candidate.shapedtw_score = float(
                np.exp(-distance / max(cfg.dtw_similarity_temperature, 1e-8))
            )
            candidate.warping_path = [
                (int(q_idx), int(left + ref_idx)) for q_idx, ref_idx in path
            ]
            candidate.final_score = float(np.clip(
                cfg.final_price_weight * candidate.price_ncc
                + cfg.final_derivative_weight * candidate.derivative_ncc
                + cfg.final_turning_weight * candidate.turning_score
                + cfg.final_shapedtw_weight * candidate.shapedtw_score,
                0.0,
                1.0,
            ))
            completed.append(candidate)
        return completed

    def _scale_bonus_and_nms(self, candidates: List[Candidate]) -> List[Candidate]:
        cfg = self.config
        for candidate in candidates:
            if candidate.refined_start is None or candidate.refined_end is None:
                continue
            scales = {
                other.scale
                for other in candidates
                if other.series_id == candidate.series_id
                and other.refined_start is not None
                and other.refined_end is not None
                and interval_iou(
                    candidate.refined_start,
                    candidate.refined_end,
                    other.refined_start,
                    other.refined_end,
                ) > cfg.interval_iou_threshold
            }
            candidate.supporting_scales = max(1, len(scales))
            candidate.final_score = float(min(
                1.0,
                float(candidate.final_score or 0.0)
                + cfg.scale_bonus_eta * math.log1p(candidate.supporting_scales),
            ))

        ordered = sorted(candidates, key=lambda item: float(item.final_score or 0.0), reverse=True)
        kept: List[Candidate] = []
        for candidate in ordered:
            overlaps = any(
                candidate.series_id == existing.series_id
                and interval_iou(
                    int(candidate.refined_start or 0),
                    int(candidate.refined_end or 0),
                    int(existing.refined_start or 0),
                    int(existing.refined_end or 0),
                ) > cfg.interval_iou_threshold
                for existing in kept
            )
            if not overlaps:
                kept.append(candidate)
        return kept

    def search(
        self,
        sketch_points: Iterable,
        market_series: List[MarketSeries],
        *,
        category: str = "all",
        timeframe: str = "1d",
        top_k: int = 20,
        y_flip: bool = False,
        log: Optional[LogCallback] = None,
    ) -> Tuple[List[Candidate], Dict[str, float]]:
        """执行完整 CPU 检索并返回候选及阶段计数/耗时。"""
        started = time.perf_counter()
        query = preprocess_sketch(sketch_points, self.config, y_flip=y_flip)
        selected_market = [
            item for item in market_series
            if item.timeframe == timeframe and (category == "all" or item.category == category)
        ]
        market_by_id = {item.series_id: item for item in selected_market}
        if log:
            log(
                f"[CPU-SEARCH][START] series={len(selected_market)}，timeframe={timeframe}，"
                f"category={category}，scales={geometric_lengths(self.config.min_length, self.config.max_length, self.config.scale_ratio)}"
            )

        recalled: List[Candidate] = []
        for index, market in enumerate(selected_market, start=1):
            recalled.extend(self._recall_series(query, market))
            if log and (index == len(selected_market) or index % 10 == 0):
                log(
                    f"[CPU-SEARCH][NCC] series={index}/{len(selected_market)}，"
                    f"candidates={len(recalled)}"
                )
        after_turning = self._apply_turning_stage(query, recalled, market_by_id)
        after_dtw = self._apply_shapedtw_stage(query, after_turning, market_by_id)
        after_nms = self._scale_bonus_and_nms(after_dtw)
        final = after_nms[:max(1, int(top_k))]
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        diagnostics = {
            "series": float(len(selected_market)),
            "ncc_candidates": float(len(recalled)),
            "turning_candidates": float(min(len(recalled), self.config.before_turning_point)),
            "dtw_candidates": float(len(after_turning)),
            "nms_candidates": float(len(after_nms)),
            "elapsed_ms": float(elapsed_ms),
        }
        if log:
            top_text = (
                f"top1={final[0].symbol} score={float(final[0].final_score or 0.0):.4f}"
                if final else "top1=none"
            )
            log(
                f"[CPU-SEARCH][DONE] NCC={len(recalled)} → Turn={len(after_turning)} → "
                f"ShapeDTW={len(after_dtw)} → NMS/TopK={len(final)}，{top_text}，"
                f"elapsed={elapsed_ms:.1f}ms"
            )
        return final, diagnostics

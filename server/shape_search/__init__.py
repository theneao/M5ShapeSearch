# -*- coding: utf-8 -*-
"""
CPU-only 手绘形态搜索包。

创建时间：2026-08-28
作用：导出连续序列搜索引擎、配置和核心数据结构。
使用方式：from shape_search import CpuSearchConfig, CpuShapeSearchEngine
"""
from .config import CpuSearchConfig
from .engine import CpuShapeSearchEngine
from .models import Candidate, MarketSeries, QueryFeatures, TurningPoint
from .preprocessing import build_market_series, preprocess_sketch

__all__ = [
    "CpuSearchConfig",
    "CpuShapeSearchEngine",
    "Candidate",
    "MarketSeries",
    "QueryFeatures",
    "TurningPoint",
    "build_market_series",
    "preprocess_sketch",
]


# -*- coding: utf-8 -*-
"""
核心模块导出。

修改时间：2026-08-28
修改作用：导出 CPU 连续序列形态搜索管理器，同时保留旧 FAISS 类供兼容测试。
使用方式：新业务代码使用 CpuShapeSearchManager。

修改时间：2026-08-28
修改作用：导出 AKShare + Binance Public 后台预加载/刷新服务。
使用方式：FastAPI lifespan 创建 MarketDataService 并绑定到路由。
"""
from .shape_features import (
    moving_average_filter,
    douglas_peucker,
    resample_fixed_length,
    normalize_minmax,
    extract_16d_stats,
    build_303d_vector,
    full_pipeline,
    ShapeStats16,
    Point,
    VECTOR_DIM,
)
from .mock_shape_generator import (
    ShapeSample,
    generate_samples,
    SHAPE_TYPES,
    SHAPE_CN_NAMES,
)
from .cpu_shape_search_manager import CpuShapeSearchManager, BUCKETS as CPU_BUCKETS
BUCKETS = CPU_BUCKETS
from .real_data_builder import build_dataset_from_symbol_pool
from .market_data_service import MarketDataService, DEFAULT_CONFIG as MARKET_DATA_DEFAULT_CONFIG

__all__ = [
    "moving_average_filter", "douglas_peucker", "resample_fixed_length",
    "normalize_minmax", "extract_16d_stats", "build_303d_vector",
    "full_pipeline", "ShapeStats16", "Point", "VECTOR_DIM",
    "ShapeSample", "generate_samples", "SHAPE_TYPES", "SHAPE_CN_NAMES",
    "FaissIndexManager", "BUCKETS",
    "CpuShapeSearchManager", "CPU_BUCKETS",
    "build_dataset_from_symbol_pool",
    "MarketDataService", "MARKET_DATA_DEFAULT_CONFIG",
]


def __getattr__(name):
    """只在旧测试/旧演示显式请求时加载 FAISS，主服务不再依赖它。"""
    if name == "FaissIndexManager":
        from .faiss_index_manager import FaissIndexManager
        return FaissIndexManager
    raise AttributeError(name)

# -*- coding: utf-8 -*-
"""
FastAPI 服务端主程序
==============================
启动方式：
    1) 开发：  uvicorn main:app --host 0.0.0.0 --port 8000 --reload
    2) 生产：  gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000

修改记录：
- 2026-08-28：主检索链路切换为 CPU Multi-scale NCC + Derivative NCC + Turning Point + Subsequence ShapeDTW。
- 2026-08-28：接入 MarketDataService；启动先加载持久化库，后台按新 K 线周期刷新。
- 2026-08-28：A 股使用 AKShare、Crypto 使用带权重限速的 Binance Public 行情。
使用方式：启动时立即加载 cpu_market_sequences.npz；手绘请求只检索内存，不重新拉取行情。
"""
from __future__ import annotations
import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core import (
    CpuShapeSearchManager,
    MarketDataService,
)
from routers import shape_router, bind_index


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("stock-match-server")


# ============================================================
# 启动时初始化 CPU 连续序列搜索
# ============================================================
def _init_cpu_search() -> tuple[CpuShapeSearchManager, MarketDataService]:
    data_dir = os.environ.get("CPU_SHAPE_DATA_DIR", "./data/cpu_shape_search")
    mgr = CpuShapeSearchManager(data_dir=data_dir)
    market_data = MarketDataService(mgr)
    loaded = market_data.start()
    if loaded:
        log.info(f"✅ 已加载 CPU 连续市场序列：{loaded}，序列数={mgr.buckets_status()}")
    else:
        log.info("🆕 本地无已验证市场缓存，AKShare + Binance Public 首次构建已在后台入队")
    return mgr, market_data


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动
    mgr, market_data = _init_cpu_search()
    bind_index(mgr, market_data)
    app.state.index_mgr = mgr
    app.state.market_data = market_data
    yield
    # 关闭
    market_data.stop()
    log.info("🛑 服务关闭，CPU 连续序列已保存")


# ============================================================
# FastAPI App
# ============================================================
app = FastAPI(
    title="技术形态选股 - 形态匹配服务",
    description="提供手绘形态匹配 / 指标筛选 / K线数据 API",
    version="3.0.0",
    lifespan=lifespan,
)

# CORS：本地开发全开放；生产需限制来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(shape_router)


# ============================================================
# 根路由 + 健康检查
# ============================================================
@app.get("/")
def root():
    mgr: CpuShapeSearchManager = app.state.index_mgr
    return {
        "name": "M5StopWatch 形态匹配云服务",
        "version": "3.0.0",
        "algorithm": "CPU Multi-scale NCC + Turning Point + Subsequence ShapeDTW",
        "series_buckets": mgr.buckets_status() if mgr else {},
        "docs": "/docs",
    }


@app.get("/api/v1/health")
def health():
    mgr: CpuShapeSearchManager = app.state.index_mgr
    buckets = mgr.buckets_status() if mgr else {}
    return {
        "code": 0,
        "data": {
            "server_version": "v3.0.0",
            "search_engine": "cpu_multiscale_shapedtw_v0.1",
            "market_data": app.state.market_data.status(),
            "enabled_categories": sorted({c.split("_", 1)[0] for c, v in buckets.items() if v > 0}),
            "rate_limit_per_min": 100,
            "query_resample_points": mgr.config.query_resample_points,
            "buckets": buckets,
        }
    }


@app.get("/api/v1/shape/debug/generate")
def debug_generate(shape_type: str = "v_rebound", count: int = 1, noise: float = 0.02):
    """调试接口：生成某个形态类型的 raw_points，可直接复制粘贴给匹配接口当测试数据。"""
    from core.mock_shape_generator import _SHAPE_GENS
    if shape_type not in _SHAPE_GENS:
        return {"code":400,"msg":f"shape_type 需在 {list(_SHAPE_GENS.keys())}"}
    results = []
    for _ in range(count):
        import numpy as np
        n = 150
        ys = _SHAPE_GENS[shape_type](n, 0.8, noise)
        ys = np.clip(ys, 0, 1)
        xs = np.linspace(0, 1, n)
        pts = np.column_stack([xs, ys]).tolist()
        results.append({"shape_type": shape_type, "points": [[round(x,5),round(y,5)] for x,y in pts]})
    return {"code":0, "data": {"count": len(results), "shapes": results}}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)

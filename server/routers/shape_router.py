# -*- coding: utf-8 -*-
"""
形态匹配 FastAPI 路由

修改时间：2026-08-28
修改作用：主调用链切换为 CPU 多尺度连续序列搜索，返回粗候选、精修边界和各子分。
使用方式：POST /api/v1/shape/match 传入手绘点，无需客户端预计算向量。

修改时间：2026-08-28
修改作用：匹配响应增加缩略图点，新增硬件详情页 K 线查询接口。

修改时间：2026-08-28
修改作用：增加 compact 硬件响应模式，减少 ESP32 接收和解析无用调试字段的内存占用。

修改时间：2026-08-28
修改作用：增加周线与服务器参数/状态/手动刷新/板块 API，数据未就绪时返回明确 503。
使用方式：管理页调用 /market-data/*；硬件继续调用 /shape/match 和 /market/kline。

修改时间：2026-08-29
修改作用：匹配与详情接口增加 5m/15m/30m/60m，旧客户端的 1h 请求自动映射到 60m。

修改时间：2026-08-29
修改作用：搜索缺失周期不再直接失败；自动按当前服务器参数排队建库并返回 202，客户端可等待或取消。

修改时间：2026-08-30
修改作用：缺失数据若处于失败冷却则立即返回 DATA_UNAVAILABLE，避免客户端长时间空等；真实建库返回建议轮询间隔。

修改时间：2026-08-30
修改作用：all 模式允许使用已就绪品类立即匹配；单个数据源异常时返回部分结果并标记缺失品类。

修改时间：2026-08-30
修改作用：单选 A 股缺失时只排队下载 A 股桶，并允许用户请求有限度越过旧失败退避。
使用方式：匹配请求中的 missing_categories 会原样传给市场数据服务。
"""
from __future__ import annotations
# 修改时间：2026-08-29
# 修改作用：匹配 API 仅接受 all/stock/crypto，彻底停用期货入口。
# 使用方式：客户端 category 使用 all、stock 或 crypto。
import time
from typing import List, Tuple, Any, Optional
from pydantic import BaseModel, Field

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from core import CpuShapeSearchManager, MarketDataService
from core.market_data_service import normalize_timeframe


router = APIRouter(prefix="/api/v1", tags=["形态匹配"])

# 全局 CPU 搜索管理器（由 main.py 启动时初始化）
index_manager: Optional[CpuShapeSearchManager] = None
market_data_service: Optional[MarketDataService] = None


def bind_index(mgr: CpuShapeSearchManager, data_service: Optional[MarketDataService] = None):
    global index_manager, market_data_service
    index_manager = mgr
    market_data_service = data_service


# ============================================================
# 请求 / 响应 Schema
# ============================================================
class ShapeMatchRequest(BaseModel):
    points: List[Tuple[float, float]] = Field(
        ...,
        description="手绘点数组，已归一化 x,y∈[0,1]；未归一化也支持（服务端会自动归一化）",
        min_length=3,
        max_length=2000,
    )
    features_16d: Optional[List[float]] = Field(
        None, description="设备端可选预计算的16维统计特征，当前未使用，服务端重算保证一致性",
    )
    category: str = Field("all", pattern="^(all|crypto|stock)$")
    timeframe: str = Field("1d", pattern="^(5m|15m|30m|60m|1h|4h|1d|1w)$")
    limit: int = Field(10, ge=1, le=50)
    compact: bool = Field(False, description="硬件端紧凑响应：缩略图限 64 点并省略 ShapeDTW 路径")


class ShapeMatchItem(BaseModel):
    sample_id: int
    symbol: str
    name: str
    category: str
    shape_type: str
    shape_cn_name: str
    match_score: float
    current_price: float
    change_pct: float
    window_start_ts: int
    window_end_ts: int
    volume_24h: int = 0
    score_details: dict = Field(default_factory=dict)
    coarse_start: int = 0
    coarse_end: int = 0
    refined_start: int = 0
    refined_end: int = 0
    coarse_scale: int = 0


class ShapeMatchResponse(BaseModel):
    code: int = 0
    msg: str = "ok"
    data: dict


# ============================================================
# 核心接口
# ============================================================
@router.post("/shape/match", response_model=ShapeMatchResponse)
def api_shape_match(req: ShapeMatchRequest, request: Request):
    t0 = time.perf_counter()
    if index_manager is None:
        raise HTTPException(503, "CPU 连续序列搜索尚未初始化")
    if len(req.points) < 3:
        raise HTTPException(400, "手绘点必须 >=3 个")
    timeframe = normalize_timeframe(req.timeframe)
    buckets = index_manager.buckets_status()
    partial_missing_categories: List[str] = []
    if req.category == "all":
        current_config = market_data_service.get_config() if market_data_service else {}
        expected_categories = [
            category for category in ("stock", "crypto")
            if current_config.get(category, {}).get("enabled", True)
        ]
        missing_categories = [
            category for category in expected_categories
            if buckets.get(f"{category}_{timeframe}", 0) <= 0
        ]
        available_categories = [
            category for category in expected_categories
            if buckets.get(f"{category}_{timeframe}", 0) > 0
        ]
        # all 是“搜索所有当前可用市场”，单个行情源失败不应阻塞另一个已就绪市场。
        available = bool(available_categories)
        if available:
            partial_missing_categories = missing_categories
    else:
        current_config = market_data_service.get_config() if market_data_service else {}
        if not current_config.get(req.category, {}).get("enabled", True):
            raise HTTPException(409, f"{req.category} 数据源已在服务器设置中关闭")
        missing_categories = (
            [req.category] if buckets.get(f"{req.category}_{timeframe}", 0) <= 0 else []
        )
        available = not missing_categories
    if not available:
        if market_data_service is None:
            raise HTTPException(503, "市场数据服务尚未初始化")
        queued = market_data_service.ensure_timeframe(
            timeframe, categories=missing_categories, user_requested=True
        )
        detail = market_data_service.status()
        if queued.get("state") == "retry_backoff":
            return JSONResponse(
                status_code=200,
                content={
                    "code": 1002,
                    "msg": "DATA_UNAVAILABLE",
                    "data": {
                        **queued,
                        "category": req.category,
                        "missing_categories": missing_categories,
                        "progress": detail.get("progress", 0.0),
                    },
                },
            )
        return JSONResponse(
            status_code=202,
            content={
                "code": 1001,
                "msg": "DATA_BUILDING",
                "data": {
                    **queued,
                    "category": req.category,
                    "missing_categories": missing_categories,
                    "progress": detail.get("progress", 0.0),
                    "server_status_message": detail.get("message", ""),
                    "status_endpoint": "/api/v1/market-data/status",
                },
            },
        )

    if partial_missing_categories and market_data_service is not None:
        # 非阻塞补齐缺失品类；失败冷却时 ensure_timeframe 不会再次唤醒刷新线程。
        market_data_service.ensure_timeframe(
            timeframe, categories=partial_missing_categories, user_requested=True
        )

    # Query 预处理、NCC 召回和 ShapeDTW 精排由同一 CPU 引擎完成。
    try:
        results, total = index_manager.search(
            category=req.category,
            timeframe=timeframe,
            top_k=req.limit,
            query_points=req.points,
        )
    except Exception as e:
        raise HTTPException(400, f"CPU 形态搜索失败: {e}")
    if not results:
        return ShapeMatchResponse(data={
            "total": 0,
            "query_ms": int((time.perf_counter() - t0) * 1000),
            "list": [],
            "debug": {
                "input_points": len(req.points),
                "algorithm": "cpu_multiscale_shapedtw_v0.1",
                "diagnostics": index_manager.last_diagnostics,
                "partial": bool(partial_missing_categories),
                "missing_categories": partial_missing_categories,
            }
        })

    # 3. 构造响应
    items = []
    for r in results:
        stype = r.get("shape_type", "feature_match")
        preview_points = r.get("raw_points", [])
        if req.compact and len(preview_points) > 64:
            last = len(preview_points) - 1
            preview_points = [preview_points[round(i * last / 63)] for i in range(64)]
        item = {
            "sample_id": r.get("sample_id"),
            "symbol": r.get("symbol"),
            "name": r.get("symbol_name", ""),
            "category": r.get("category"),
            "shape_type": stype,
            "shape_cn_name": "连续形态特征匹配",
            "match_score": r.get("match_score", 0.0),
            "current_price": r.get("close_price", 0.0),
            "change_pct": r.get("change_pct", 0.0),
            "window_start_ts": r.get("start_ts", 0),
            "window_end_ts": r.get("end_ts", 0),
            "volume_24h": 0,
            "score_method": r.get("_score_method"),
            "score_details": r.get("_score_details", {}),
            "coarse_start": r.get("_coarse_start", 0),
            "coarse_end": r.get("_coarse_end", 0),
            "refined_start": r.get("_refined_start", 0),
            "refined_end": r.get("_refined_end", 0),
            "coarse_scale": r.get("_coarse_scale", 0),
            "raw_bars": r.get("_raw_bars", 0),
            "preview_points": preview_points,
        }
        if not req.compact:
            item["warping_path"] = r.get("_warping_path", [])
        items.append(item)

    query_ms = int((time.perf_counter() - t0) * 1000)
    return ShapeMatchResponse(data={
        "total": total,
        "query_ms": query_ms,
        "list": items,
        "algorithm": "cpu_multiscale_shapedtw_v0.1",
        "diagnostics": {} if req.compact else index_manager.last_diagnostics,
        "partial": bool(partial_missing_categories),
        "missing_categories": partial_missing_categories,
    })


# ============================================================
# 辅助接口：返回索引桶状态（调试用）
# ============================================================
@router.get("/shape/index_status")
def api_index_status():
    if index_manager is None:
        return {"code": 0, "data": {"loaded": False}}
    return {"code": 0, "data": {"loaded": True, "buckets": index_manager.buckets_status()}}


@router.get("/market-data/config")
def api_market_data_config():
    if market_data_service is None:
        raise HTTPException(503, "市场数据服务尚未初始化")
    return {"code": 0, "data": market_data_service.get_config()}


@router.put("/market-data/config")
def api_market_data_update(
    payload: dict = Body(...),
    refresh: bool = Query(True, description="保存后是否立即后台刷新"),
):
    if market_data_service is None:
        raise HTTPException(503, "市场数据服务尚未初始化")
    try:
        config = market_data_service.update_config(payload, refresh=refresh)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"无效数据参数: {exc}") from exc
    return {"code": 0, "msg": "saved", "data": config}


@router.get("/market-data/status")
def api_market_data_status():
    if market_data_service is None:
        raise HTTPException(503, "市场数据服务尚未初始化")
    return {"code": 0, "data": market_data_service.status()}


@router.post("/market-data/refresh")
def api_market_data_refresh(force: bool = True):
    if market_data_service is None:
        raise HTTPException(503, "市场数据服务尚未初始化")
    queued = market_data_service.request_refresh(force=force)
    return {
        "code": 0,
        "msg": "refresh queued" if queued else "refresh already running; queued again",
        "data": market_data_service.status(),
    }


@router.get("/market-data/sectors")
def api_market_data_sectors(refresh: bool = False):
    if market_data_service is None:
        raise HTTPException(503, "市场数据服务尚未初始化")
    try:
        sectors = market_data_service.list_sectors(refresh=refresh)
    except Exception as exc:
        raise HTTPException(502, f"AKShare 板块列表获取失败: {exc}") from exc
    return {"code": 0, "data": {"items": sectors}}


@router.get("/market/kline")
def api_market_kline(
    symbol: str,
    tf: str = "1d",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    limit: int = 200,
):
    """供硬件详情页按标的读取 K 线，检索仍完全在服务端执行。"""
    if index_manager is None:
        raise HTTPException(503, "CPU 连续序列搜索尚未初始化")
    tf = normalize_timeframe(tf)
    if tf not in {"5m", "15m", "30m", "60m", "4h", "1d", "1w"}:
        raise HTTPException(400, "tf 仅支持 5m/15m/30m/60m/4h/1d/1w")
    data = index_manager.get_kline(
        symbol=symbol,
        timeframe=tf,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
    )
    if data is None:
        raise HTTPException(404, f"未找到标的: {symbol} ({tf})")
    return {"code": 0, "msg": "ok", "data": data}

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
形态匹配服务 - 简化版交互式演示 UI
========================================
专注于真实数据获取场景，隐藏不必要的参数

修改记录：
- 2026-08-27：增加实时日志；改为纯特征匹配；修复画板白底识别和对比图；增加匹配走势缩略图。
- 2026-08-28：增加独立品类数量、每标的 K 线数和最新窗口自适应放缩；取消简化版 UI 的历史滑窗。
- 2026-08-28：表格显示评分来源，缺失距离不再伪装成 0.000，避免将 FAISS 粗召回分误读为最终相似度。
- 2026-08-28：切换为 CPU Multi-scale NCC + Derivative NCC + Turning Point + Subsequence ShapeDTW，显示粗尺度和精修边界。

启动方式：
    python interactive_demo_simple.py
"""
import json
import os
import sys
import time
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Tuple, List

import numpy as np
import gradio as gr
from gradio.components.plot import PlotData
from PIL import Image, ImageDraw

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import (
    CpuShapeSearchManager,
    build_dataset_from_symbol_pool,
)
from shape_search import CpuSearchConfig
from shape_search.preprocessing import preprocess_sketch

# ============================================================
# 全局状态
# ============================================================
_MGR: Optional[CpuShapeSearchManager] = None
_LAST_DATA_SOURCE: str = "MOCK"
_TMPDIR: Path = Path(tempfile.mkdtemp(prefix="demo_", dir=str(Path(__file__).parent / "tests" / "mock")))


def _sketch_to_points(
    sketch_img: Optional[Image.Image],
    y_flip: bool = True,
    min_points: int = 20
) -> List[List[float]]:
    """将画板图像转换为点序列，并排除透明或纯色背景。"""
    if sketch_img is None:
        return []

    background_img = None
    # 处理 Gradio 返回的可能是字典的情况
    if isinstance(sketch_img, dict):
        # Gradio 的 Sketchpad 可能返回 {'background': Image, 'composite': Image}
        background_img = sketch_img.get("background")
        if sketch_img.get("composite") is not None:
            sketch_img = sketch_img["composite"]
        elif background_img is not None:
            sketch_img = background_img
        else:
            return []

    # 如果不是 PIL Image，尝试转换
    if not isinstance(sketch_img, Image.Image):
        try:
            sketch_img = Image.fromarray(sketch_img)
        except Exception:
            return []

    arr = np.array(sketch_img.convert("RGBA"))
    alpha = arr[..., 3]

    # Gradio 画板可能返回“透明底+笔迹”，也可能返回“纯白不透明底+笔迹”。
    # 旧逻辑只看 alpha，会把整张白色背景识别成笔迹，最终得到 y=0 的水平线。
    mask = None
    if background_img is not None:
        try:
            if not isinstance(background_img, Image.Image):
                background_img = Image.fromarray(background_img)
            bg_arr = np.array(background_img.convert("RGBA").resize(sketch_img.size))
            pixel_diff = np.max(
                np.abs(arr.astype(np.int16) - bg_arr.astype(np.int16)), axis=2
            )
            mask = (alpha > 30) & (pixel_diff > 18)
        except Exception:
            mask = None

    if mask is None or not mask.any():
        if np.mean(alpha < 250) > 0.1:
            # 大部分背景透明时，alpha 足以区分笔迹。
            mask = alpha > 30
        else:
            # 不透明背景：用边缘像素估算背景色，再提取与背景不同的笔迹。
            rgb = arr[..., :3].astype(np.int16)
            border = np.concatenate(
                [rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]], axis=0
            )
            bg_rgb = np.median(border, axis=0)
            color_diff = np.max(np.abs(rgb - bg_rgb), axis=2)
            mask = (alpha > 30) & (color_diff > 24)

    if not mask.any():
        return []

    ys, xs = np.where(mask)
    order = np.argsort(xs)
    xs = xs[order].astype(np.float64)
    ys = ys[order].astype(np.float64)

    bins = np.linspace(xs.min(), xs.max(), 90)
    bin_ids = np.digitize(xs, bins[1:-1])
    uniq_x, uniq_y = [], []
    for b in np.unique(bin_ids):
        sel = bin_ids == b
        uniq_x.append(xs[sel].mean())
        uniq_y.append(np.median(ys[sel]))

    xn = np.array(uniq_x)
    yn = np.array(uniq_y)

    if len(xn) < min_points:
        t = np.linspace(0, 1, min_points)
        tt = np.linspace(0, 1, len(xn)) if len(xn) >= 2 else np.array([0.0, 1.0])
        if len(xn) >= 2:
            xn = np.interp(t, tt, xn)
            yn = np.interp(t, tt, yn)
        else:
            xn = np.linspace(0, 1, min_points)
            yn = np.full(min_points, 0.5)

    xn = (xn - xn.min()) / max(xn.max() - xn.min(), 1e-6)
    yn = (yn - yn.min()) / max(yn.max() - yn.min(), 1e-6)

    if y_flip:
        yn = 1.0 - yn

    pts = np.stack([xn, yn], axis=1)
    print(
        f"[DRAW][OK] 笔迹像素={int(mask.sum())}，提取点={len(pts)}，"
        f"y范围={float(yn.min()):.3f}~{float(yn.max()):.3f}",
        flush=True,
    )
    return pts.tolist()


def _resample_result_curve(points, size: int = 128) -> Optional[np.ndarray]:
    """把匹配窗口点序列归一化并重采样，供主对比图和缩略图复用。"""
    try:
        arr = np.asarray(points, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if arr.ndim != 2 or arr.shape[0] < 2 or arr.shape[1] < 2:
        return None
    arr = arr[np.isfinite(arr[:, 0]) & np.isfinite(arr[:, 1])]
    if len(arr) < 2:
        return None
    arr = arr[np.argsort(arr[:, 0])]
    x = arr[:, 0]
    y = arr[:, 1]
    x = (x - x.min()) / max(float(x.max() - x.min()), 1e-9)
    unique_x, unique_idx = np.unique(x, return_index=True)
    y = y[unique_idx]
    if len(unique_x) < 2:
        return None
    y_range = float(y.max() - y.min())
    if y_range < 1e-9:
        y = np.full_like(y, 0.5)
    else:
        y = (y - y.min()) / y_range
    return np.interp(np.linspace(0.0, 1.0, size), unique_x, y)


def _format_score_metric(score_details: dict, key: str, digits: int = 3) -> str:
    """格式化精排指标；未计算时显示—，不冒充完美距离 0。"""
    value = score_details.get(key)
    if value is None:
        return "—"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "—"
    if not np.isfinite(numeric):
        return "—"
    return f"{numeric:.{digits}f}"


def _score_method_label(method: str) -> str:
    """将内部评分方法转换为 UI 可读文字。"""
    return {
        "absolute_multimetric": "多指标精排",
        "faiss_recall_only": "FAISS粗排",
        "cpu_multiscale_shapedtw": "CPU多尺度ShapeDTW",
    }.get(str(method), "未知")


def _make_match_thumbnail(query_y: np.ndarray, candidate_y: np.ndarray) -> Image.Image:
    """生成红色手绘线与蓝色匹配走势叠加缩略图。"""
    width, height = 320, 160
    pad_left, pad_top, pad_right, pad_bottom = 14, 12, 14, 18
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    for ratio in (0.25, 0.5, 0.75):
        y_grid = int(pad_top + ratio * plot_h)
        draw.line((pad_left, y_grid, width - pad_right, y_grid), fill="#E5E7EB", width=1)

    def line_points(values: np.ndarray):
        count = max(len(values) - 1, 1)
        return [
            (
                int(pad_left + idx / count * plot_w),
                int(pad_top + (1.0 - float(np.clip(value, 0.0, 1.0))) * plot_h),
            )
            for idx, value in enumerate(values)
        ]

    draw.line(line_points(candidate_y), fill="#2563EB", width=4, joint="curve")
    draw.line(line_points(query_y), fill="#EF4444", width=2, joint="curve")
    return image


# ============================================================
# 构建数据
# ============================================================
def build_index(
    data_source: str,
    window_days: int,
    fetch_bars_per_symbol: int,
    adaptive_scale: bool,
    cat_crypto: bool,
    crypto_symbols: int,
    cat_stock: bool,
    stock_symbols: int,
    cat_futures: bool,
    futures_symbols: int,
    timeframe: str,
    progress=gr.Progress()
) -> Tuple[str, List, gr.update]:
    """构建数据集和索引"""
    global _MGR, _LAST_DATA_SOURCE

    def nonnegative_int(value, default: int = 0) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default

    symbol_counts = {
        "crypto": nonnegative_int(crypto_symbols),
        "stock": nonnegative_int(stock_symbols),
        "futures": nonnegative_int(futures_symbols),
    }
    enabled = {
        "crypto": bool(cat_crypto),
        "stock": bool(cat_stock),
        "futures": bool(cat_futures),
    }
    cats = [cat for cat in ("crypto", "stock", "futures") if enabled[cat] and symbol_counts[cat] > 0]

    if not cats:
        return "[ERROR] 请至少启用一个品类，并将该品类的标的数设为大于 0", None, gr.update(interactive=False)

    cats_tup = tuple(cats)

    d = _TMPDIR / "cpu_shape_demo"
    shutil.rmtree(d, ignore_errors=True)

    # 检查 TUSHARE_TOKEN
    if data_source == "TUSHARE":
        import os
        token = os.environ.get("TUSHARE_TOKEN", "")
        if not token:
            return (
                "[WARNING] TUSHARE_TOKEN not set! Will use MOCK data as fallback.\n\n"
                "To use real Tushare data:\n"
                "1. Get token from https://www.tushare.pro\n"
                "2. Set environment variable: set TUSHARE_TOKEN=your_token\n"
                "3. Restart the application",
                None,
                gr.update(interactive=False)
            )

    progress(0.02, desc="Preparing data fetch...")
    t0 = time.perf_counter()
    requested_fetch_bars = nonnegative_int(fetch_bars_per_symbol)
    effective_fetch_bars = (
        requested_fetch_bars if requested_fetch_bars > 0 else max(300, int(window_days))
    )

    def update_fetch_progress(completed: int, total: int, message: str) -> None:
        """把逐标的构建进度映射到 Gradio 的 2%~62% 区间。"""
        ratio = min(max(completed / max(total, 1), 0.0), 1.0)
        progress(0.02 + ratio * 0.60, desc=message)

    try:
        samples = build_dataset_from_symbol_pool(
            n_symbols=1,  # 兼容旧入口；实际数量由 symbol_counts 分品类控制。
            categories=cats_tup,
            timeframe=timeframe,
            window=int(window_days),
            step=10,  # 最新窗口模式不滑动，仅为兼容底层旧参数保留。
            data_source=data_source,
            verbose=True,  # 启用详细日志
            progress_callback=update_fetch_progress,
            symbol_counts=symbol_counts,
            fetch_bars_per_symbol=effective_fetch_bars,
            latest_only=True,
            # CPU 引擎查询时自行多尺度搜索，数据层只保留最长连续序列。
            adaptive_scale=False,
        )
    except Exception as e:
        print(f"[BUILD][ERROR] Data fetch failed: {type(e).__name__}: {e}", flush=True)
        return f"[ERROR] Data fetch failed: {e}", None, gr.update(interactive=False)

    t_gen = time.perf_counter() - t0

    print(
        f"[CPU-DATA][START] 数据抓取完成，共 {len(samples)} 条原始序列，"
        f"耗时 {t_gen:.2f}s；开始建立连续市场序列库",
        flush=True,
    )
    progress(0.65, desc=f"Storing {len(samples)} continuous market series...")
    t1 = time.perf_counter()

    try:
        if adaptive_scale:
            search_max_length = max(24, min(300, effective_fetch_bars))
            search_min_length = min(24, search_max_length)
        else:
            fixed_length = max(4, min(int(window_days), effective_fetch_bars))
            search_min_length = fixed_length
            search_max_length = fixed_length
        search_config = CpuSearchConfig(
            min_length=search_min_length,
            max_length=search_max_length,
        ).validate()
        mgr = CpuShapeSearchManager(data_dir=str(d), config=search_config)
        total = mgr.build_from_samples(samples, timeframe=timeframe)
        _MGR = mgr
        _LAST_DATA_SOURCE = data_source
    except Exception as e:
        print(f"[CPU-DATA][ERROR] Series build failed: {type(e).__name__}: {e}", flush=True)
        return f"[ERROR] Series build failed: {e}", None, gr.update(interactive=False)

    t_idx = time.perf_counter() - t1

    print(
        f"[CPU-DATA][OK] 连续序列库已建立，写入 {total} 条序列，耗时 {t_idx:.2f}s，"
        f"桶状态={mgr.buckets_status()}",
        flush=True,
    )

    progress(1.0, desc="Done!")

    data_source_label = {
        "MOCK": "MOCK (Math curves)",
        "AKSHARE": "AKShare (Real A-share / futures / crypto, no key)",
        "BINANCE": "Binance (Real crypto prices)",
        "TUSHARE": "Tushare (Real stock prices)",
    }.get(data_source, data_source)

    active_symbol_counts = {cat: symbol_counts[cat] for cat in cats}
    approx_symbols = sum(active_symbol_counts.values())
    raw_lengths = sorted({len(s.raw_points) for s in samples})
    raw_lengths_text = (
        f"{raw_lengths}; every window ends at the latest K-line"
        if data_source != "MOCK"
        else "MOCK synthetic curves (used only for algorithm testing)"
    )
    symbol_summary = " / ".join(f"{cat}={count}" for cat, count in active_symbol_counts.items())

    status = f"""OK - Dataset ready!

**Data Source**: {data_source_label}
**Symbols**: {symbol_summary} → {approx_symbols} total symbols
**K-line Fetch**: latest {effective_fetch_bars} bars/symbol (`0` uses CPU search default 300)
**Search Scales**: {search_min_length}~{search_max_length} bars, geometric ratio={search_config.scale_ratio:.2f}
**Mode**: {'Query-time multi-scale subsequence search' if adaptive_scale else 'Fixed-length subsequence search'}
**Stored Raw Lengths**: {raw_lengths_text}
**Continuous Series**: {total} total
**Timeframe**: {timeframe}
**Build Time**: Fetch {t_gen:.1f}s + Store/preprocess {t_idx:.2f}s = {t_gen+t_idx:.1f}s
**Series Status**: {mgr.buckets_status()}

Search pipeline:
  1. close → log-price → Savitzky-Golay smoothing
  2. Query 128-point resampling + Z-normalization
  3. Multi-scale Price NCC + Derivative NCC recall
  4. Turning-point structure rerank
  5. Subsequence ShapeDTW boundary refinement
  6. Interval NMS de-duplication

Ready to draw shapes in the canvas!
"""

    buckets_table = [
        [b, mgr.buckets_status().get(b, 0)]
        for b in sorted(mgr.buckets_status().keys())
    ]

    return status, buckets_table, gr.update(interactive=True)


# ============================================================
# 匹配
# ============================================================
def do_match(
    sketch_img: Optional[Image.Image],
    timeframe: str,
    cat_all: bool,
    cat_crypto: bool,
    cat_stock: bool,
    cat_futures: bool,
    top_k: int,
) -> Tuple[Optional[PlotData], List, Optional[List], str]:
    """按连续特征和序列相似度匹配手绘走势。"""
    global _MGR

    if _MGR is None:
        return None, [], None, "ERROR: Please click [Build Data] first"

    pts = _sketch_to_points(sketch_img, y_flip=True)
    if len(pts) < 10:
        return None, [], None, "WARNING: Draw more points"

    try:
        query_features = preprocess_sketch(pts, _MGR.config, y_flip=False)
    except Exception as e:
        return None, [], None, f"ERROR: {e}"

    if cat_all:
        cat_q = "all"
    else:
        chosen = []
        if cat_crypto:
            chosen.append("crypto")
        if cat_stock:
            chosen.append("stock")
        if cat_futures:
            chosen.append("futures")
        if not chosen:
            chosen = ["crypto", "stock", "futures"]
        cat_q = chosen[0] if len(chosen) == 1 else "all"

    t0 = time.perf_counter()
    res, total_cand = _MGR.search(
        category=cat_q,
        timeframe=timeframe,
        top_k=int(top_k),
        query_points=pts,
    )
    ms = (time.perf_counter() - t0) * 1000

    if not res:
        return None, [], None, "WARNING: No matching results"

    tbl_rows = []
    gallery_items = []
    candidate_curves = []
    qy = query_features.smooth.astype(np.float64)
    qy_range = float(np.ptp(qy))
    qy = np.full_like(qy, 0.5) if qy_range < 1e-12 else (qy - qy.min()) / qy_range

    for i, r in enumerate(res[:top_k]):
        sc = float(r.get("match_score", 0.0))
        symbol = str(r.get("symbol", "?"))
        symbol_name = str(r.get("symbol_name", ""))
        raw_points = r.get("raw_points") or []
        raw_bars = len(raw_points)
        candidate_y = _resample_result_curve(raw_points)
        score_details = r.get("_score_details", {})
        score_method = _score_method_label(r.get("_score_method", ""))

        tbl_rows.append([
            i + 1,
            f"{sc * 100:.1f}%",
            score_method,
            _format_score_metric(score_details, "price_ncc"),
            _format_score_metric(score_details, "derivative_ncc"),
            _format_score_metric(score_details, "turning_score"),
            _format_score_metric(score_details, "shapedtw_score"),
            r.get("category", "?"),
            symbol,
            symbol_name,
            int(r.get("_coarse_scale", 0)),
            raw_bars,
            f"{r.get('change_pct', 0.0):+.2f}%",
        ])

        if candidate_y is not None:
            candidate_curves.append((i + 1, symbol, sc, raw_bars, candidate_y))
            caption_name = f" {symbol_name}" if symbol_name else ""
            gallery_items.append((
                _make_match_thumbnail(qy, candidate_y),
                f"#{i + 1} {symbol}{caption_name} · {raw_bars}K · {sc * 100:.1f}%",
            ))

    colors = [
        "#2563EB", "#059669", "#7C3AED", "#D97706", "#0891B2",
        "#4F46E5", "#65A30D", "#C026D3", "#0284C7", "#9333EA",
    ]
    plot_traces = [{
        "x": list(range(128)),
        "y": qy.astype(float).tolist(),
        "type": "scatter",
        "mode": "lines",
        "name": "手绘走势",
        "line": {"color": "#EF4444", "width": 4},
        "hovertemplate": "手绘 · %{x}: %{y:.3f}<extra></extra>",
    }]
    for curve_idx, (rank, symbol, score, raw_bars, candidate_y) in enumerate(candidate_curves[:10]):
        plot_traces.append({
            "x": list(range(128)),
            "y": candidate_y.astype(float).tolist(),
            "type": "scatter",
            "mode": "lines",
            "name": f"#{rank} {symbol} [{raw_bars}K] ({score * 100:.1f}%)",
            "line": {
                "color": colors[curve_idx % len(colors)],
                "width": 3 if rank == 1 else 1.5,
            },
            "opacity": 0.9 if rank == 1 else 0.55,
            "hovertemplate": f"#{rank} {symbol} · %{{y:.3f}}<extra></extra>",
        })

    plot_payload = {
        "data": plot_traces,
        "layout": {
            "title": f"手绘走势与 Top{min(len(candidate_curves), 10)} 匹配窗口",
            "xaxis": {"title": "时间序列（0-127）", "range": [0, 127]},
            "yaxis": {"title": "归一化价格（0-1）", "range": [-0.03, 1.03]},
            "legend": {"orientation": "h", "y": -0.22},
            "margin": {"l": 55, "r": 20, "t": 55, "b": 100},
            "height": 480,
            "hovermode": "x unified",
        }
    }
    # gr.Plot 需要 Figure 或 PlotData；普通 dict 在 Gradio 6 会触发
    # AttributeError: 'dict' object has no attribute '__module__'。
    plot_data = PlotData(
        type="plotly",
        plot=json.dumps(plot_payload, ensure_ascii=False),
    )

    sc1 = float(res[0].get("match_score", 0.0))
    top1_symbol = res[0].get("symbol", "?")
    top1_name = res[0].get("symbol_name", "")
    top1_raw_bars = len(res[0].get("raw_points") or [])
    top1_details = res[0].get("_score_details", {})
    top1_method = _score_method_label(res[0].get("_score_method", ""))
    top1_price = _format_score_metric(top1_details, "price_ncc", 4)
    top1_derivative = _format_score_metric(top1_details, "derivative_ncc", 4)
    top1_turning = _format_score_metric(top1_details, "turning_score", 4)
    top1_shapedtw = _format_score_metric(top1_details, "shapedtw_score", 4)
    top1_distance = _format_score_metric(top1_details, "shapedtw_distance", 4)
    top1_coarse_scale = int(res[0].get("_coarse_scale", 0))
    top1_refined_start = int(res[0].get("_refined_start", 0))
    top1_refined_end = int(res[0].get("_refined_end", 0))
    diagnostics = _MGR.last_diagnostics

    status = f"""OK - Match completed in {ms:.1f}ms

**Top1 Match**: {top1_symbol} {top1_name} · refined window {top1_raw_bars} K-lines · fused similarity {sc1*100:.1f}%
**Score Meaning**: Shape similarity, not probability/confidence
**Score Source**: {top1_method}
**Top1 Components**: Price NCC={top1_price} · Derivative NCC={top1_derivative} · Turning={top1_turning} · ShapeDTW={top1_shapedtw} (distance={top1_distance})
**Boundary**: coarse scale={top1_coarse_scale} K → refined [{top1_refined_start}, {top1_refined_end}) = {top1_raw_bars} K
**Match Mode**: Multi-scale NCC → Derivative NCC → Turning Point → Subsequence ShapeDTW → Interval NMS
**Fixed Shape Classification / FAISS**: Disabled

**Query Features**:
  • Resample points: {len(query_features.normalized)}
  • Significant turning points: {len(query_features.turning_points)}
  • Smooth: Savitzky-Golay window={_MGR.config.smooth_window}, polyorder={_MGR.config.smooth_polyorder}

**Search Range**: {cat_q} × {timeframe} × Top{top_k}
**Candidates**: NCC={total_cand} → Turning={int(diagnostics.get('turning_candidates', 0))} → ShapeDTW={int(diagnostics.get('dtw_candidates', 0))} → NMS={int(diagnostics.get('nms_candidates', 0))}
**Preview**: Red = your drawing, Blue/colored = matched market window
"""

    print(
        f"[MATCH][OK] points={len(pts)}，candidates={total_cand}，"
        f"top1={top1_symbol}，score={sc1:.4f}，"
        f"method={res[0].get('_score_method', 'unknown')}，"
        f"price_ncc={top1_price}，derivative_ncc={top1_derivative}，"
        f"turning={top1_turning}，shapedtw={top1_shapedtw}，"
        f"boundary={top1_refined_start}:{top1_refined_end}，"
        f"elapsed={ms:.1f}ms",
        flush=True,
    )
    return plot_data, gallery_items, tbl_rows, status


# ============================================================
# Gradio 应用
# ============================================================
def create_app():
    """创建应用"""
    with gr.Blocks(title="Shape Matching Demo") as app:

        gr.Markdown("""
# Shape Matching Demo - Interactive UI

## Workflow
1. **Left Panel**: Configure data source and parameters
2. **Click [Build Data]**: Fetch and store continuous market series
3. **Middle Canvas**: Draw any shape pattern
4. **Right Panel**: View refined subsequence boundaries and explainable scores

---
        """)

        with gr.Row():
            # Left Panel
            with gr.Column(scale=1):
                gr.Markdown("### Configuration")

                data_source = gr.Radio(
                    choices=["MOCK", "AKSHARE", "BINANCE", "TUSHARE"],
                    value="AKSHARE",
                    label="Data Source",
                    info="MOCK=Math | AKSHARE=Real multi-asset (no key) | BINANCE=Real crypto | TUSHARE=Real stocks (needs token)"
                )

                window_days = gr.Slider(
                    minimum=10, maximum=120, value=60, step=10,
                    label="固定搜索长度 K 线数",
                    info="关闭 CPU 多尺度时生效；开启时自动搜索 24~300 根"
                )

                fetch_bars_per_symbol = gr.Number(
                    value=300, minimum=0, maximum=2400, precision=0,
                    label="每标的连续历史 K 线数",
                    info="0 = CPU 搜索默认 300；序列越长，可检索的历史子段越多"
                )

                adaptive_scale = gr.Checkbox(
                    value=True,
                    label="CPU 多尺度子序列搜索",
                    info="查询时按几何尺度搜索连续历史，ShapeDTW 自动精修真实边界"
                )

                gr.Markdown("**品类与独立标的数量**")
                with gr.Row():
                    cat_crypto = gr.Checkbox(value=True, label="Crypto", scale=2)
                    crypto_symbols = gr.Number(
                        value=10, minimum=0, maximum=100, precision=0,
                        label="标的数", scale=1,
                    )
                with gr.Row():
                    cat_stock = gr.Checkbox(value=True, label="Stock", scale=2)
                    stock_symbols = gr.Number(
                        value=10, minimum=0, maximum=100, precision=0,
                        label="标的数", scale=1,
                    )
                with gr.Row():
                    cat_futures = gr.Checkbox(value=False, label="Futures", scale=2)
                    futures_symbols = gr.Number(
                        value=10, minimum=0, maximum=100, precision=0,
                        label="标的数", scale=1,
                    )

                timeframe = gr.Radio(
                    choices=["1h", "4h", "1d"],
                    value="1d",
                    label="Timeframe"
                )

                build_btn = gr.Button("Build Data", variant="primary", size="lg")

                build_status = gr.Markdown("")
                buckets_table = gr.Dataframe(
                    headers=["Bucket", "Continuous Series Count"],
                    label="CPU Series Status",
                )

            # Middle Canvas
            with gr.Column(scale=1):
                gr.Markdown("### Draw Shape")
                gr.Markdown("Draw any pattern, then it auto-matches")

                sketch = gr.Sketchpad(
                    label="Canvas",
                    height=400,
                    show_label=False,
                    sources=[],
                )

                clear_btn = gr.Button("Clear", size="sm")
                clear_btn.click(lambda: None, outputs=sketch)

            # Right Panel
            with gr.Column(scale=1):
                gr.Markdown("### Results")

                with gr.Row():
                    cat_all = gr.Checkbox(value=True, label="All Categories", scale=1)
                    top_k = gr.Slider(1, 50, value=10, step=1, label="Top K", scale=1)

                plot = gr.Plot(label="Comparison Chart")

                gr.Markdown("**匹配缩略图：红线为手绘走势，蓝线为该标的匹配窗口。**")
                result_gallery = gr.Gallery(
                    label="TopK Match Thumbnails",
                    columns=2,
                    height=420,
                    object_fit="contain",
                )

                result_table = gr.Dataframe(
                    headers=[
                        "Rank", "Similarity", "Score Source",
                        "Price NCC↑", "Derivative NCC↑", "Turning↑", "ShapeDTW↑",
                        "Category", "Symbol", "Name", "Coarse K", "Refined K", "Change%",
                    ],
                    label="Top10 Results",
                    interactive=False,
                )

                match_status = gr.Markdown("")

        # Events
        build_btn.click(
            fn=build_index,
            inputs=[
                data_source, window_days, fetch_bars_per_symbol, adaptive_scale,
                cat_crypto, crypto_symbols,
                cat_stock, stock_symbols,
                cat_futures, futures_symbols,
                timeframe,
            ],
            outputs=[build_status, buckets_table, sketch],
        )

        sketch.change(
            fn=do_match,
            inputs=[sketch, timeframe, cat_all, cat_crypto, cat_stock, cat_futures, top_k],
            outputs=[plot, result_gallery, result_table, match_status],
        )

        return app


if __name__ == "__main__":
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(
                    encoding="utf-8",
                    errors="replace",
                    line_buffering=True,
                    write_through=True,
                )

    import socket
    def find_free_port(start=7860):
        for port in range(start, start + 10):
            try:
                sock = socket.socket()
                sock.bind(('127.0.0.1', port))
                sock.close()
                return port
            except OSError:
                continue
        return start

    configured_port = int(os.environ.get("GRADIO_SERVER_PORT", "7860"))
    port = find_free_port(configured_port)
    print("\n[INFO] Starting Shape Matching Demo UI", flush=True)
    print(f"[INFO] Open: http://127.0.0.1:{port}\n", flush=True)

    app = create_app()
    app.launch(server_name="127.0.0.1", server_port=port, show_error=True)

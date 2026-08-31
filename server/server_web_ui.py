#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
形态搜索统一 Web UI（手绘页 + 整体设置页）。

创建时间：2026-08-28
作用：7860 提供手绘匹配、市场数据配置和运行看板；页面通过 FastAPI 读写持久化配置，
      不在 Gradio 进程内重复拉行情/建库。
使用方式：python server_web_ui.py --port 7860。

修改时间：2026-08-28
修改作用：Crypto 设置切换为 Binance Public，增加 quote 资产、请求间隔、并发数、每分钟权重预算和重试次数。

修改时间：2026-08-29
修改作用：设置页与手绘页周期扩展为 5m/15m/30m/60m/4h/1d/1w。

修改时间：2026-08-29
修改作用：市场数据与运行状态页增加持续可见的 HTML 进度条，实时日志缩减为阶段/异常摘要。

修改时间：2026-08-29
修改作用：数量 0 明确表示全部；补充刷新检查周期及 Binance 可用排序指标，与端侧数据页保持一致。

修改时间：2026-08-31
修改作用：兼容新版 Gradio ImageEditor 将 composite/background 返回为 NumPy 数组；
          禁止对数组执行布尔 or，避免点击手绘匹配时报数组真值不明确。
使用方式：7860 页面继续直接绘图并点击匹配，无需转换上传格式。

修改时间：2026-08-31
修改作用：将原 7861 整体设置页完整合并到 7860，并兼容 ImageEditor 的 layers 回退结构；
          所有图片候选均显式判空，不再对 NumPy 数组求布尔值。
使用方式：打开 7860，在顶部标签中切换手绘匹配、连接、市场数据和运行状态。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

import gradio as gr
import numpy as np
import requests
from gradio.components.plot import PlotData
from PIL import Image, ImageDraw


API_BASE = os.environ.get("SHAPE_API_BASE", "http://127.0.0.1:8000").rstrip("/")
_LAST_RESULTS: Dict[str, Dict[str, Any]] = {}
_RESULT_LOCK = threading.Lock()


def _request(method: str, path: str, timeout: float = 15.0, **kwargs) -> Any:
    try:
        response = requests.request(method, f"{API_BASE}{path}", timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(f"无法连接 FastAPI {API_BASE}: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"FastAPI 返回非 JSON（HTTP {response.status_code}）") from exc
    if response.status_code >= 400:
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    if isinstance(payload, dict) and payload.get("code", 0) != 0:
        raise RuntimeError(str(payload.get("msg", "服务器拒绝请求")))
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def _sketch_to_points(value: Any, min_points: int = 20) -> List[List[float]]:
    if value is None:
        return []
    background = None
    if isinstance(value, dict):
        editor_value = value
        background = editor_value.get("background")
        composite = editor_value.get("composite")
        if composite is not None:
            value = composite
        else:
            value = None
            layers = editor_value.get("layers")
            if isinstance(layers, (list, tuple)):
                for layer in reversed(layers):
                    if layer is not None:
                        value = layer
                        break
            if value is None:
                value = background
    if value is None:
        return []
    if not isinstance(value, Image.Image):
        try:
            value = Image.fromarray(value)
        except Exception:
            return []
    image = np.asarray(value.convert("RGBA"))
    alpha = image[..., 3]
    mask = None
    if background is not None:
        try:
            if not isinstance(background, Image.Image):
                background = Image.fromarray(background)
            bg = np.asarray(background.convert("RGBA").resize(value.size))
            mask = (alpha > 30) & (np.max(np.abs(image.astype(np.int16) - bg.astype(np.int16)), axis=2) > 18)
        except Exception:
            mask = None
    if mask is None or not mask.any():
        rgb = image[..., :3].astype(np.int16)
        border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]], axis=0)
        background_color = np.median(border, axis=0)
        mask = (alpha > 30) & (np.max(np.abs(rgb - background_color), axis=2) > 24)
    if not mask.any():
        return []
    ys, xs = np.where(mask)
    bins = np.linspace(float(xs.min()), float(xs.max()), 96)
    groups = np.digitize(xs, bins[1:-1])
    x_values = np.asarray([xs[groups == item].mean() for item in np.unique(groups)], dtype=float)
    y_values = np.asarray([np.median(ys[groups == item]) for item in np.unique(groups)], dtype=float)
    if len(x_values) < 2:
        return []
    target_count = max(min_points, len(x_values))
    target = np.linspace(0.0, 1.0, target_count)
    source = np.linspace(0.0, 1.0, len(x_values))
    y_values = np.interp(target, source, y_values)
    y_range = max(float(np.ptp(y_values)), 1e-9)
    normalized_y = 1.0 - (y_values - y_values.min()) / y_range
    return np.column_stack([target, normalized_y]).astype(float).tolist()


def _curve(points: Any, count: int = 128) -> Optional[np.ndarray]:
    try:
        array = np.asarray(points, dtype=float)
    except (TypeError, ValueError):
        return None
    if array.ndim != 2 or len(array) < 2 or array.shape[1] < 2:
        return None
    array = array[np.isfinite(array[:, :2]).all(axis=1)]
    if len(array) < 2:
        return None
    array = array[np.argsort(array[:, 0])]
    x = array[:, 0]
    x = (x - x.min()) / max(float(np.ptp(x)), 1e-9)
    unique_x, indices = np.unique(x, return_index=True)
    y = array[indices, 1]
    y = np.full_like(y, 0.5) if np.ptp(y) < 1e-9 else (y - y.min()) / np.ptp(y)
    return np.interp(np.linspace(0.0, 1.0, count), unique_x, y)


def _thumbnail(query: np.ndarray, candidate: np.ndarray) -> Image.Image:
    width, height, pad = 320, 160, 14
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for ratio in (0.25, 0.5, 0.75):
        y = int(pad + ratio * (height - pad * 2))
        draw.line((pad, y, width - pad, y), fill="#E5E7EB", width=1)

    def coordinates(values: np.ndarray):
        return [
            (
                int(pad + index / max(len(values) - 1, 1) * (width - pad * 2)),
                int(pad + (1.0 - float(value)) * (height - pad * 2)),
            )
            for index, value in enumerate(values)
        ]

    draw.line(coordinates(candidate), fill="#2563EB", width=4)
    draw.line(coordinates(query), fill="#EF4444", width=2)
    return image


def _plot_data(traces: List[Dict[str, Any]], title: str, height: int = 470) -> PlotData:
    payload = {
        "data": traces,
        "layout": {
            "title": title,
            "height": height,
            "xaxis": {"title": "时间序列"},
            "yaxis": {"title": "归一化价格", "range": [-0.03, 1.03]},
            "legend": {"orientation": "h", "y": -0.2},
            "margin": {"l": 55, "r": 20, "t": 55, "b": 95},
        },
    }
    return PlotData(type="plotly", plot=json.dumps(payload, ensure_ascii=False))


def match_shape(sketch: Any, timeframe: str, category: str, top_k: int):
    points = _sketch_to_points(sketch)
    if len(points) < 10:
        return None, [], [], "请先在画板上画出完整曲线", gr.update(choices=[], value=None)
    try:
        data = _request(
            "POST",
            "/api/v1/shape/match",
            timeout=45.0,
            json={
                "points": points,
                "timeframe": timeframe,
                "category": category,
                "limit": int(top_k),
                "compact": False,
            },
        )
    except Exception as exc:
        return None, [], [], f"匹配失败：{exc}", gr.update(choices=[], value=None)

    results = data.get("list", [])
    query = _curve(points)
    if query is None or not results:
        return None, [], [], "服务器未返回候选，请检查数据刷新状态", gr.update(choices=[], value=None)
    traces = [{
        "x": list(range(128)), "y": query.tolist(), "type": "scatter", "mode": "lines",
        "name": "手绘走势", "line": {"color": "#EF4444", "width": 4},
    }]
    gallery, rows, choices = [], [], []
    colors = ["#2563EB", "#059669", "#7C3AED", "#D97706", "#0891B2", "#4F46E5"]
    stored: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate(results, start=1):
        candidate = _curve(item.get("preview_points", []))
        if candidate is None:
            continue
        score = float(item.get("match_score", 0.0))
        symbol = str(item.get("symbol", "?"))
        name = str(item.get("name", ""))
        label = f"#{index} {symbol} {name}".strip()
        choices.append(label)
        stored[label] = item
        traces.append({
            "x": list(range(128)), "y": candidate.tolist(), "type": "scatter", "mode": "lines",
            "name": f"#{index} {symbol} ({score * 100:.1f}%)",
            "line": {"color": colors[(index - 1) % len(colors)], "width": 3 if index == 1 else 1.5},
            "opacity": 0.9 if index == 1 else 0.55,
        })
        gallery.append((_thumbnail(query, candidate), f"{label} · {score * 100:.1f}%"))
        details = item.get("score_details", {})
        rows.append([
            index, f"{score * 100:.1f}%", item.get("category", "?"), symbol, name,
            int(item.get("raw_bars", 0)),
            f"{float(details.get('price_ncc', 0.0)):.3f}",
            f"{float(details.get('derivative_ncc', 0.0)):.3f}",
            f"{float(details.get('turning_score', 0.0)):.3f}",
            f"{float(details.get('shapedtw_score', 0.0)):.3f}",
            f"{float(item.get('change_pct', 0.0)):+.2f}%",
        ])
    with _RESULT_LOCK:
        _LAST_RESULTS.clear()
        _LAST_RESULTS.update(stored)
    status = (
        f"匹配完成：{len(rows)} 个结果，服务端耗时 {int(data.get('query_ms', 0))} ms。"
        "手绘请求仅检索预加载内存库，本次没有拉取行情。"
    )
    return _plot_data(traces, f"手绘与 Top{len(rows)} 匹配窗口"), gallery, rows, status, gr.update(
        choices=choices, value=choices[0] if choices else None
    )


def load_kline(selection: str, timeframe: str):
    with _RESULT_LOCK:
        item = dict(_LAST_RESULTS.get(selection, {}))
    if not item:
        return None, "请先选择匹配结果"
    params = {"symbol": item["symbol"], "tf": timeframe, "limit": 300}
    if item.get("window_start_ts"):
        params["from_ts"] = item["window_start_ts"]
    if item.get("window_end_ts"):
        params["to_ts"] = item["window_end_ts"]
    try:
        data = _request("GET", "/api/v1/market/kline", timeout=20.0, params=params)
    except Exception as exc:
        return None, f"K 线加载失败：{exc}"
    bars = data.get("bars", [])
    if not bars:
        return None, "该匹配区间没有 K 线"
    trace = {
        "x": [bar["t"] for bar in bars],
        "open": [bar["o"] for bar in bars],
        "high": [bar["h"] for bar in bars],
        "low": [bar["l"] for bar in bars],
        "close": [bar["c"] for bar in bars],
        "type": "candlestick",
        "name": data.get("symbol", "K线"),
    }
    payload = {
        "data": [trace],
        "layout": {
            "title": f"{data.get('symbol')} {data.get('name', '')} · {timeframe}",
            "height": 480,
            "xaxis": {"rangeslider": {"visible": True}},
            "margin": {"l": 55, "r": 20, "t": 55, "b": 50},
        },
    }
    return PlotData(type="plotly", plot=json.dumps(payload, ensure_ascii=False)), f"已加载 {len(bars)} 根 K 线"


def config_values():
    try:
        config = _request("GET", "/api/v1/market-data/config")
        return (
            config["timeframes"], config["feature_window"], config["fetch_bars"], config["adaptive_scale"],
            config["refresh_check_seconds"],
            config["stock"]["enabled"], config["stock"]["count"], config["stock"]["rank_metric"],
            config["stock"]["rank_order"], config["stock"]["sector"],
            config["crypto"]["enabled"], config["crypto"]["count"], config["crypto"]["rank_metric"],
            config["crypto"]["rank_order"], config["crypto"]["quote_asset"],
            config["crypto"]["binance_request_interval_ms"],
            config["crypto"]["binance_weight_limit_per_minute"],
            config["crypto"]["binance_concurrency"],
            config["crypto"]["binance_retry_count"],
            f"已从 {API_BASE} 加载配置",
        )
    except Exception as exc:
        defaults = (
            ["1d"], 60, 300, True, 300, True, 300, "market_cap", "top", "all",
            True, 300, "volume", "top", "USDT", 150, 1200, 4, 3,
        )
        return (*defaults, f"配置加载失败：{exc}")


def save_config(
    timeframes, feature_window, fetch_bars, adaptive_scale, refresh_check_seconds,
    stock_enabled, stock_count, stock_metric, stock_order, stock_sector,
    crypto_enabled, crypto_count, crypto_metric, crypto_order, crypto_quote_asset,
    binance_interval_ms, binance_weight_limit, binance_concurrency, binance_retry_count,
    refresh: bool,
):
    payload = {
        "data_source": "AKSHARE+BINANCE_PUBLIC",
        "timeframes": list(timeframes or ["1d"]),
        "feature_window": int(feature_window),
        "fetch_bars": int(fetch_bars),
        "adaptive_scale": bool(adaptive_scale),
        "refresh_check_seconds": int(refresh_check_seconds),
        "stock": {
            "enabled": bool(stock_enabled), "count": int(stock_count),
            "rank_metric": stock_metric, "rank_order": stock_order,
            "sector": stock_sector or "all",
        },
        "crypto": {
            "enabled": bool(crypto_enabled), "count": int(crypto_count),
            "rank_metric": crypto_metric, "rank_order": crypto_order,
            "quote_asset": crypto_quote_asset or "USDT",
            "binance_request_interval_ms": int(binance_interval_ms),
            "binance_weight_limit_per_minute": int(binance_weight_limit),
            "binance_concurrency": int(binance_concurrency),
            "binance_retry_count": int(binance_retry_count),
        },
    }
    try:
        _request("PUT", f"/api/v1/market-data/config?refresh={'true' if refresh else 'false'}", json=payload)
        action = "已保存并在后台刷新" if refresh else "已保存（未刷新）"
        return f"{action}。旧数据会一直可搜索，直到新数据完整构建后原子替换。"
    except Exception as exc:
        return f"保存失败：{exc}"


def _progress_bar(progress: float, state: str, message: str) -> str:
    """生成可由 Timer 持续更新的确定型进度条。"""
    progress = min(100.0, max(0.0, progress))
    color = "#ef4444" if state in {"error", "degraded"} else "#f97316" if progress < 100 else "#22c55e"
    safe_message = html.escape(str(message))
    return (
        '<div style="padding:12px 14px;border:1px solid #374151;border-radius:10px;">'
        f'<div style="display:flex;justify-content:space-between;margin-bottom:7px;">'
        f'<strong>数据加载</strong><span>{progress:.1f}%</span></div>'
        '<div style="height:18px;background:#1f2937;border-radius:9px;overflow:hidden;">'
        f'<div style="height:100%;width:{progress:.1f}%;background:{color};transition:width .35s ease;">'
        '</div></div>'
        f'<div style="margin-top:7px;color:#9ca3af;font-size:13px;">{safe_message}</div></div>'
    )


def refresh_status():
    try:
        data = _request("GET", "/api/v1/market-data/status")
    except Exception as exc:
        visual = _progress_bar(0.0, "error", f"状态读取失败：{exc}")
        return visual, visual, f"状态读取失败：{exc}", [], ""
    state = data.get("state", "unknown")
    progress = float(data.get("progress", 0.0)) * 100.0
    message = data.get("message", "")
    error = data.get("last_error") or "无"
    markdown = (
        f"**状态**：`{state}` · **进度**：{progress:.1f}%  \n"
        f"**当前消息**：{message}  \n"
        f"**最后错误**：{error}  \n"
        f"**Crypto 能力**：{data.get('crypto_capability', '')}"
    )
    buckets = [[key, value] for key, value in sorted(data.get("buckets", {}).items())]
    logs = "\n".join(data.get("logs", [])[-30:])
    visual = _progress_bar(progress, state, message)
    return visual, visual, markdown, buckets, logs


def refresh_sectors():
    try:
        data = _request("GET", "/api/v1/market-data/sectors?refresh=true", timeout=30.0)
        items = ["all", *data.get("items", [])]
        return gr.update(choices=items, value="all"), f"已加载 {len(items) - 1} 个行业板块"
    except Exception as exc:
        return gr.update(), f"板块加载失败：{exc}"


def add_settings_tabs(app: gr.Blocks) -> None:
    with gr.Tab("设备与连接"):
        gr.Markdown(f"""
### 板卡、FastAPI 和设置页

- 当前 Web UI 连接：`{API_BASE}`
- 手表点击顶部 WiFi 状态进入 **SETTINGS**，或按 B 开启 `M5Shape-*` 热点。
- 连接热点后打开 `http://192.168.4.1`，在高级选项同时设置 WiFi 和 `Shape Search Server URL`。
- 服务器 URL 必须使用终端打印的电脑局域网 IP，例如 `http://192.168.x.x:8000`，不能用 `127.0.0.1`。
        """)

    with gr.Tab("市场数据"):
        gr.Markdown(
            "**A 股：AKShare；Crypto：Binance Spot Public（无需 API Key）**。"
            "刷新只在 FastAPI 后台进行，手绘匹配不会重新拉行情。"
        )
        source = gr.Textbox(
            value="AKSHARE + BINANCE_PUBLIC", label="数据源", interactive=False
        )
        del source
        with gr.Row():
            timeframes = gr.CheckboxGroup(
                ["5m", "15m", "30m", "60m", "4h", "1d", "1w"],
                value=["1d"], label="预加载周期",
            )
            adaptive = gr.Checkbox(value=True, label="CPU 多尺度搜索")
        with gr.Row():
            feature_window = gr.Slider(10, 300, value=60, step=5, label="参考形态窗口")
            fetch_bars = gr.Slider(20, 1000, value=300, step=10, label="每标的最新 K 线数")
            refresh_check_seconds = gr.Slider(
                60, 3600, value=300, step=60, label="新 K 线检查周期（秒）"
            )

        gr.Markdown("### A 股")
        with gr.Row():
            stock_enabled = gr.Checkbox(value=True, label="启用")
            stock_count = gr.Number(
                value=300, minimum=0, maximum=3000, precision=0, label="数量 N（0=全部）"
            )
            stock_metric = gr.Dropdown(
                ["market_cap", "volume", "volume_ratio"], value="market_cap", label="排序指标"
            )
            stock_order = gr.Radio(["top", "bottom"], value="top", label="前/后 N")
        with gr.Row():
            stock_sector = gr.Dropdown(["all"], value="all", allow_custom_value=True, label="行业板块")
            sector_button = gr.Button("从 AKShare 刷新板块列表")
        sector_status = gr.Markdown("")

        gr.Markdown("### 虚拟货币")
        gr.Markdown(
            "Binance `/api/v3/klines` 单次权重 2、最多 1000 根；全市场 24h ticker 权重 80。"
            "默认只使用 1200/6000 weight/min，并监控 `X-MBX-USED-WEIGHT-1M`；"
            "收到 429/418 后严格遵守 `Retry-After`。使用仅市场数据域名及固定 GET 白名单，"
            "不会读取或保存 API Key/Secret。Binance 不提供市值与量比；可按 24h 成交额、"
            "基础币成交量、成交笔数或涨跌幅排序。"
        )
        with gr.Row():
            crypto_enabled = gr.Checkbox(value=True, label="启用")
            crypto_count = gr.Number(
                value=300, minimum=0, maximum=3000, precision=0, label="数量 N（0=全部）"
            )
            crypto_metric = gr.Dropdown(
                ["volume", "base_volume", "trade_count", "change_pct"],
                value="volume", label="排序指标（Binance 24h ticker）"
            )
            crypto_order = gr.Radio(["top", "bottom"], value="top", label="前/后 N")
        with gr.Row():
            crypto_quote_asset = gr.Dropdown(
                ["USDT", "USDC", "FDUSD"], value="USDT", label="计价资产"
            )
            binance_interval_ms = gr.Slider(
                50, 1000, value=150, step=10, label="请求最小间隔 (ms)"
            )
            binance_weight_limit = gr.Slider(
                100, 4800, value=1200, step=100, label="本地权重预算 / min"
            )
            binance_concurrency = gr.Slider(1, 8, value=4, step=1, label="并发连接数")
            binance_retry_count = gr.Slider(0, 5, value=3, step=1, label="5xx/网络重试次数")

        with gr.Row():
            save_button = gr.Button("仅保存参数")
            refresh_button = gr.Button("保存并立即后台刷新", variant="primary")
        data_progress = gr.HTML(_progress_bar(0.0, "created", "等待加载状态"))
        save_status = gr.Markdown("")
        config_outputs = [
            timeframes, feature_window, fetch_bars, adaptive, refresh_check_seconds,
            stock_enabled, stock_count, stock_metric, stock_order, stock_sector,
            crypto_enabled, crypto_count, crypto_metric, crypto_order, crypto_quote_asset,
            binance_interval_ms, binance_weight_limit, binance_concurrency,
            binance_retry_count, save_status,
        ]
        config_inputs = config_outputs[:-1]
        app.load(config_values, outputs=config_outputs)
        save_button.click(lambda *args: save_config(*args, False), inputs=config_inputs, outputs=save_status)
        refresh_event = refresh_button.click(
            lambda *args: save_config(*args, True), inputs=config_inputs, outputs=save_status
        )
        sector_button.click(refresh_sectors, outputs=[stock_sector, sector_status])

    with gr.Tab("运行状态"):
        status_progress = gr.HTML(_progress_bar(0.0, "created", "等待加载状态"))
        status_markdown = gr.Markdown("")
        bucket_table = gr.Dataframe(headers=["数据桶", "连续序列数"], interactive=False)
        logs = gr.Textbox(label="阶段与异常日志（最近 30 条）", lines=12, interactive=False)
        status_button = gr.Button("刷新状态")
        status_outputs = [data_progress, status_progress, status_markdown, bucket_table, logs]
        status_button.click(refresh_status, outputs=status_outputs)
        refresh_event.then(refresh_status, outputs=status_outputs)
        app.load(refresh_status, outputs=status_outputs)
        if hasattr(gr, "Timer"):
            timer = gr.Timer(1.0)
            timer.tick(refresh_status, outputs=status_outputs)


def create_app() -> gr.Blocks:
    with gr.Blocks(title="M5 Shape Search") as app:
        gr.Markdown("# M5 Shape Search")
        with gr.Tab("手绘匹配"):
            with gr.Row():
                with gr.Column(scale=4):
                    sketch = gr.Sketchpad(label="画板", height=410, sources=[])
                    with gr.Row():
                        timeframe = gr.Radio(
                            ["5m", "15m", "30m", "60m", "4h", "1d", "1w"],
                            value="1d", label="K 线周期",
                        )
                        category = gr.Radio(["all", "stock", "crypto"], value="all", label="品类")
                        top_k = gr.Slider(1, 20, value=10, step=1, label="Top K")
                    with gr.Row():
                        match_button = gr.Button("开始服务端匹配", variant="primary")
                        cancel_button = gr.Button("取消等待", variant="stop")
                        clear_button = gr.Button("清空")
                    match_status = gr.Markdown("")
                with gr.Column(scale=6):
                    comparison = gr.Plot(label="对比图")
                    gallery = gr.Gallery(label="结果缩略图", columns=2, height=390, object_fit="contain")
            table = gr.Dataframe(
                headers=["Rank", "Similarity", "Category", "Symbol", "Name", "K", "NCC", "dNCC", "Turning", "ShapeDTW", "Change"],
                interactive=False,
            )
            with gr.Row():
                selected = gr.Dropdown(label="查看单标的 K 线详情")
                detail_button = gr.Button("加载详情")
            detail_plot = gr.Plot(label="单标的 K 线")
            detail_status = gr.Markdown("")

            match_event = match_button.click(
                match_shape,
                inputs=[sketch, timeframe, category, top_k],
                outputs=[comparison, gallery, table, match_status, selected],
            )
            cancel_button.click(
                lambda: "已取消页面等待；FastAPI 中已开始的 CPU 精排会很快自行结束。",
                outputs=match_status,
                cancels=[match_event],
            )
            clear_button.click(lambda: None, outputs=sketch)
            detail_button.click(load_kline, inputs=[selected, timeframe], outputs=[detail_plot, detail_status])
        add_settings_tabs(app)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    print(f"[WEB] unified, API={API_BASE}, URL=http://127.0.0.1:{args.port}", flush=True)
    create_app().queue().launch(
        server_name="127.0.0.1",
        server_port=args.port,
        show_error=True,
        inbrowser=False,
    )


if __name__ == "__main__":
    main()

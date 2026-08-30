# -*- coding: utf-8 -*-
"""
真实数据采集 & 滑窗切形态 构建器（统一入口）
==========================================
- MOCK 模式：仍然用 10 类数学形态 + 固定标的池循环，但对外只暴露「标的池数量 N / 步长 / 滑窗长度」参数
- Tushare：真实 A 股日线（需要 TUSHARE_TOKEN 环境变量）
- Binance：真实加密货币现货 K 线（1d/1h/4h，无需 key）
- AKShare：多资产数据源（A股、期货、加密货币，无需 key）

修改记录：
- 2026-08-27：增加逐品类、逐标的数据抓取日志、耗时与进度回调；真实窗口不再归入固定形态类别。
- 2026-08-28：支持各品类独立数量、每标的最新 K 线数量，以及所有窗口结束于最新 K 线的自适应尺度。
- 2026-08-28：真实样本保留连续 close/timestamp，供 CPU Multi-scale NCC 在查询时搜索任意子序列。
- 2026-08-28：真实样本同步保留 OHLC，供硬件端结果详情页绘制真实蜡烛图。

调用方式（外部统一入口）：
    from core.real_data_builder import build_dataset_from_symbol_pool
    samples = build_dataset_from_symbol_pool(
        n_symbols=200,
        categories=("stock", "crypto"),
        timeframe="1d",
        window_days=60,
        step_days=10,
        data_source="AKSHARE",  # 新增 AKShare
        symbol_counts={"stock": 10, "crypto": 5},
        fetch_bars_per_symbol=60,  # 每标的只拉最新 60 根
        latest_only=True,
        adaptive_scale=True,       # 多个回看尺度统一缩放到 window 后提取特征
    )
"""
from __future__ import annotations
import os
import json
import time
import math
from datetime import datetime, timedelta
from typing import List, Tuple, Optional, Dict, Any, Callable

import numpy as np

from .mock_shape_generator import (
    ShapeSample, generate_samples, SHAPE_TYPES,
    _CRYPTO_POOL, _STOCK_POOL, _FUTURES_POOL,
)
from .shape_features import full_pipeline

# ---------------------------------------------------------------------
# 可选数据源：真实数据实现，占位即可（要生产运行时再补真 key）
# ---------------------------------------------------------------------

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, str], None]


def _fetch_binance_ohlcv(
    symbol: str,
    timeframe: str = "1d",
    limit: int = 2400,
    log: Optional[LogCallback] = None,
) -> Optional[np.ndarray]:
    """Binance public klines → (N,5) [ts, open, high, low, close] float64；失败返回 None"""
    try:
        import requests
        interval = {"1d": "1d", "4h": "4h", "1h": "1h"}.get(timeframe, "1d")
        url = "https://api.binance.com/api/v3/klines"
        if log:
            log(f"[HTTP][BINANCE] 请求 {symbol} {interval}，limit={limit}，timeout=8s")
        r = requests.get(url, params={"symbol": symbol, "interval": interval,
                                       "limit": limit}, timeout=8)
        if r.status_code != 200:
            if log:
                log(f"[HTTP][BINANCE][WARN] {symbol} HTTP {r.status_code}: {r.text[:160]}")
            return None
        rows = r.json()
        if not rows:
            if log:
                log(f"[HTTP][BINANCE][WARN] {symbol} 返回空数据")
            return None
        arr = np.zeros((len(rows), 5), dtype=np.float64)
        for i, row in enumerate(rows):
            arr[i, 0] = float(row[0]) / 1000.0          # ts (秒)
            arr[i, 1] = float(row[1])                     # open
            arr[i, 2] = float(row[2])                     # high
            arr[i, 3] = float(row[3])                     # low
            arr[i, 4] = float(row[4])                     # close
        return arr
    except Exception as exc:
        if log:
            log(f"[HTTP][BINANCE][ERROR] {symbol}: {type(exc).__name__}: {exc}")
        return None


def _fetch_tushare_daily(
    ts_code: str,
    token: str,
    start_date: str = "20160101",
    end_date: str = "20991231",
    limit: int = 2400,
    log: Optional[LogCallback] = None,
) -> Optional[np.ndarray]:
    """Tushare daily → (N,5) [ts, open, high, low, close]；失败返回 None"""
    try:
        import tushare as ts  # type: ignore
        ts.set_token(token)
        pro = ts.pro_api()
        if log:
            log(f"[HTTP][TUSHARE] 请求 {ts_code}，{start_date}~{end_date}，保留最新 {limit} 根")
        df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or len(df) == 0:
            if log:
                log(f"[HTTP][TUSHARE][WARN] {ts_code} 返回空数据")
            return None
        df = df.sort_values("trade_date").tail(limit).reset_index(drop=True)
        n = len(df)
        arr = np.zeros((n, 5), dtype=np.float64)
        for i in range(n):
            ymd = str(df.at[i, "trade_date"])   # YYYYMMDD
            ts_day = time.mktime(time.strptime(ymd, "%Y%m%d"))
            arr[i, 0] = ts_day
            arr[i, 1] = float(df.at[i, "open"])
            arr[i, 2] = float(df.at[i, "high"])
            arr[i, 3] = float(df.at[i, "low"])
            arr[i, 4] = float(df.at[i, "close"])
        return arr
    except Exception as exc:
        if log:
            log(f"[HTTP][TUSHARE][ERROR] {ts_code}: {type(exc).__name__}: {exc}")
        return None


def _fetch_akshare_ohlcv(
    symbol: str,
    category: str,
    timeframe: str = "1d",
    limit: int = 2400,
    log: Optional[LogCallback] = None,
) -> Optional[np.ndarray]:
    """AKShare 多资产 K 线 → (N,5) [ts, open, high, low, close]；失败返回 None

    - stock:   如 "SH:600519" → stock_zh_a_daily (新浪源, 前复权日线, 无需 key)
    - futures: 如 "SHFE:CU2410" → 取品种 "CU0" 主力连续合约 (futures_main_sina 新浪源)
    - crypto:  本函数不支持，由调用方 fallback 到币安 public API（AKShare 当前版本无历史 K 线）
    """
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:
        if log:
            log(f"[AKSHARE][ERROR] 导入失败: {type(exc).__name__}: {exc}")
        return None

    ts_col = "date"
    try:
        end_date = datetime.now().strftime("%Y%m%d")
        lookback_days = max(365, int(limit * 2.2))
        start_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
        if category == "stock":
            # 新浪源：symbol 需小写市场前缀 + 代码，如 sh600519 / sz000858
            market = "sh" if ":" not in symbol or symbol.upper().startswith("SH:") else "sz"
            code = symbol.split(":")[-1]
            if log:
                log(
                    f"[HTTP][AKSHARE] 股票新浪源 {market}{code}，"
                    f"{start_date}~{end_date}，保留最新 {limit} 根"
                )
            df = ak.stock_zh_a_daily(symbol=f"{market}{code}",
                                     start_date=start_date, end_date=end_date,
                                     adjust="qfq")
            o, h, l, c = "open", "high", "low", "close"
        elif category == "futures":
            code = symbol.split(":")[-1]
            product = "".join(ch for ch in code if ch.isalpha())
            if not product:
                if log:
                    log(f"[HTTP][AKSHARE][WARN] 无法从期货代码 {symbol} 提取品种")
                return None
            if log:
                log(
                    f"[HTTP][AKSHARE] 期货新浪源 {product}0，"
                    f"{start_date}~{end_date}，保留最新 {limit} 根"
                )
            df = ak.futures_main_sina(symbol=f"{product}0",
                                      start_date=start_date, end_date=end_date)
            o, h, l, c = "开盘价", "最高价", "最低价", "收盘价"
            ts_col = "日期"
        else:
            return None

        if df is None or len(df) == 0:
            if log:
                log(f"[HTTP][AKSHARE][WARN] {symbol} 返回空数据")
            return None
        for col in (ts_col, o, h, l, c):
            if col not in df.columns:
                if log:
                    log(f"[HTTP][AKSHARE][WARN] {symbol} 缺少字段 {col}")
                return None
        df = df.sort_values(ts_col)
        df = df.tail(limit).reset_index(drop=True)
        n = len(df)
        arr = np.zeros((n, 5), dtype=np.float64)
        for i in range(n):
            date_str = str(df.at[i, ts_col])
            date_str = date_str.split(" ")[0].replace("/", "-")
            try:
                arr[i, 0] = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
            except Exception:
                arr[i, 0] = i * 86400.0
            arr[i, 1] = float(df.at[i, o])
            arr[i, 2] = float(df.at[i, h])
            arr[i, 3] = float(df.at[i, l])
            arr[i, 4] = float(df.at[i, c])
        return arr
    except Exception as exc:
        if log:
            log(f"[HTTP][AKSHARE][ERROR] {symbol}: {type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------------
# 通用工具：真实 OHLCV close 序列 → 按滑窗生成 ShapeSample 列表
# ---------------------------------------------------------------------
def _slice_ohlcv_to_samples(
    ohlcv: np.ndarray,                    # (N,5) [ts,o,h,l,c]
    category: str, symbol: str, symbol_name: str,
    timeframe: str,
    window: int = 60,
    step: int = 10,
    sample_id_seed: int = 0,
    max_windows: Optional[int] = None,
) -> List[ShapeSample]:
    """一条真实 K 线 → 滑窗切形态样本；仅保留连续特征，生产数据不做固定形态分类。"""
    if ohlcv is None or len(ohlcv) < window:
        return []
    close = ohlcv[:, 4].astype(np.float64)
    timestamps = ohlcv[:, 0].astype(np.float64)
    samples: List[ShapeSample] = []
    sid = sample_id_seed
    n = len(close)
    starts = list(range(0, n - window + 1, step))
    if max_windows is not None and max_windows > 0:
        # 只保留最新的 N 个窗口，避免一个标的生成数百条高度重叠样本。
        starts = starts[-int(max_windows):]
    for start in starts:
        seg = close[start:start + window]
        # Min-Max 归一化成 (N,2) 点序列用于 full_pipeline
        xs = np.linspace(0, 1, window, dtype=np.float64)
        ys = seg.astype(np.float64)
        mn, mx = ys.min(), ys.max()
        rng = max(mx - mn, 1e-9)
        yn = (ys - mn) / rng
        pts = np.stack([xs, yn], axis=1).tolist()
        seq_128, stats, vec = full_pipeline(pts, y_flip=False)
        samples.append(ShapeSample(
            sample_id=sid,
            category=category,
            symbol=symbol,
            symbol_name=symbol_name,
            # 兼容现有数据结构保留字段，但不参与召回、过滤、排序或 UI 展示。
            shape_type="feature_match",
            raw_points=pts,
            seq_128=seq_128,
            vector=vec,
            start_ts=int(timestamps[start]),
            end_ts=int(timestamps[start + window - 1]),
            close_price=round(float(close[-1]), 4),
            change_pct=round(float((seg[-1] / max(seg[0], 1e-9) - 1.0) * 100), 2),
            market_timestamps=timestamps[start:start + window].astype(np.int64).tolist(),
            close_series=seg.astype(float).tolist(),
            ohlc_series=ohlcv[start:start + window, 1:5].astype(float).tolist(),
        ))
        sid += 1
    return samples


def _adaptive_scale_lengths(feature_window: int, available_bars: int) -> List[int]:
    """生成最多 5 个自适应回看长度；所有长度对应的窗口都以最新 K 线结束。"""
    feature_window = max(2, int(feature_window))
    available_bars = max(2, int(available_bars))
    lengths = []
    for scale in (0.50, 0.75, 1.00, 1.50, 2.00):
        raw_length = max(2, int(round(feature_window * scale)))
        if raw_length <= available_bars and raw_length not in lengths:
            lengths.append(raw_length)
    if available_bars not in lengths:
        if len(lengths) >= 5:
            lengths[-1] = available_bars
        else:
            lengths.append(available_bars)
    return sorted(set(lengths))


def _latest_ohlcv_to_samples(
    ohlcv: np.ndarray,
    category: str,
    symbol: str,
    symbol_name: str,
    timeframe: str,
    feature_window: int = 60,
    adaptive_scale: bool = False,
    sample_id_seed: int = 0,
) -> List[ShapeSample]:
    """将最新 K 线生成单窗口或多尺度窗口；所有窗口的结束时间都是最新一根 K 线。"""
    if ohlcv is None or len(ohlcv) < 2:
        return []
    close = ohlcv[:, 4].astype(np.float64)
    timestamps = ohlcv[:, 0].astype(np.float64)
    available_bars = len(close)
    raw_lengths = (
        _adaptive_scale_lengths(feature_window, available_bars)
        if adaptive_scale
        else [available_bars]
    )

    samples: List[ShapeSample] = []
    sid = sample_id_seed
    target_count = max(2, int(feature_window))
    target_x = np.linspace(0.0, 1.0, target_count, dtype=np.float64)
    for raw_length in raw_lengths:
        seg = close[-raw_length:]
        raw_x = np.linspace(0.0, 1.0, raw_length, dtype=np.float64)
        value_range = max(float(seg.max() - seg.min()), 1e-9)
        raw_y = (seg - seg.min()) / value_range

        # 先按时间轴缩放为特征窗口长度，再进入统一的 128 点特征流水线。
        scaled_y = np.interp(target_x, raw_x, raw_y)
        scaled_points = np.stack([target_x, scaled_y], axis=1).tolist()
        seq_128, _stats, vec = full_pipeline(scaled_points, y_flip=False)
        raw_points = np.stack([raw_x, raw_y], axis=1).tolist()

        samples.append(ShapeSample(
            sample_id=sid,
            category=category,
            symbol=symbol,
            symbol_name=symbol_name,
            shape_type="feature_match",
            raw_points=raw_points,
            seq_128=seq_128,
            vector=vec,
            start_ts=int(timestamps[-raw_length]),
            end_ts=int(timestamps[-1]),
            close_price=round(float(close[-1]), 4),
            change_pct=round(float((seg[-1] / max(seg[0], 1e-9) - 1.0) * 100), 2),
            market_timestamps=timestamps[-raw_length:].astype(np.int64).tolist(),
            close_series=seg.astype(float).tolist(),
            ohlc_series=ohlcv[-raw_length:, 1:5].astype(float).tolist(),
        ))
        sid += 1
    return samples


# ---------------------------------------------------------------------
# 对外统一入口：按「标的池数量 N」构建形态样本集
# ---------------------------------------------------------------------
def build_dataset_from_symbol_pool(
    n_symbols: int = 100,
    categories: Tuple[str, ...] = ("crypto", "stock", "futures"),
    timeframe: str = "1d",
    window: int = 60,
    step: int = 10,
    data_source: str = "MOCK",
    seed: int = 20260820,
    verbose: bool = False,
    progress_callback: Optional[ProgressCallback] = None,
    symbol_counts: Optional[Dict[str, int]] = None,
    max_windows_per_symbol: Optional[int] = None,
    fetch_bars_per_symbol: Optional[int] = None,
    latest_only: bool = False,
    adaptive_scale: bool = False,
) -> List[ShapeSample]:
    """
    用「标的池数量 N」控制总规模，不再暴露"每形态干净样本数 / 手绘扰动倍数"。

    参数：
        n_symbols: 每品类采集多少个标的（例如 n_symbols=100 × 3 品类 = 300 只标的）
        categories: 可选品类组合
        timeframe: 1d / 4h / 1h（决定真实 K 线周期）
        window / step: 滑窗长度与步长，默认 60 交易日 / 步长 10
        data_source:
            "MOCK"    —— 仍然用数学生成，但只按标的数控制规模（UI 无预制形态概念）
            "BINANCE" —— 真实加密货币（无需 key，public API）
            "TUSHARE" —— 真实 A 股，需要环境变量 TUSHARE_TOKEN
            "ALPACA"  —— 占位
        seed: MOCK 模式下的随机种子（真实数据模式下忽略）
        verbose: 是否向终端输出详细日志
        progress_callback: 可选回调，参数为 (已完成数, 总数, 当前状态)
        symbol_counts: 各品类独立标的数量，例如 {"stock": 10, "crypto": 5}
        max_windows_per_symbol: 每个真实标的最多保留多少个最新形态窗口；None 表示不限制
        fetch_bars_per_symbol: 每个标的只拉取最近多少根 K 线；None/0 表示等于 window
        latest_only: True 时不做历史滑窗，只生成以最新 K 线结尾的窗口
        adaptive_scale: latest_only=True 时生成不同原始长度的最新窗口并统一缩放到 window
    """
    samples: List[ShapeSample] = []
    sid = 0
    rng = np.random.RandomState(seed)

    # 每种品类，取前 N 个标的代码 / 名称
    pool_by_cat: Dict[str, List[Tuple[str, str]]] = {
        "crypto": list(_CRYPTO_POOL),
        "stock":  list(_STOCK_POOL),
        "futures": list(_FUTURES_POOL),
    }

    counts_by_cat = {
        cat: max(0, int(symbol_counts.get(cat, n_symbols))) if symbol_counts else max(0, int(n_symbols))
        for cat in pool_by_cat
    }
    active_categories = [
        cat for cat in categories if pool_by_cat.get(cat) and counts_by_cat.get(cat, 0) > 0
    ]
    effective_fetch_bars = max(2, int(fetch_bars_per_symbol or window))
    scale_lengths = (
        _adaptive_scale_lengths(window, effective_fetch_bars)
        if latest_only and adaptive_scale
        else [effective_fetch_bars]
    )
    total_units = max(1, sum(counts_by_cat[cat] for cat in active_categories))
    completed_units = 0

    def detail_log(message: str) -> None:
        if verbose:
            print(message, flush=True)

    def report(message: str, completed: Optional[int] = None) -> None:
        done = completed_units if completed is None else completed
        detail_log(message)
        if progress_callback:
            try:
                progress_callback(done, total_units, message)
            except Exception as exc:
                detail_log(f"[BUILD][WARN] 进度回调失败: {type(exc).__name__}: {exc}")

    report(
        f"[BUILD][START] source={data_source.upper()}，categories={active_categories}，"
        f"标的数量={{{', '.join(f'{cat}: {counts_by_cat[cat]}' for cat in active_categories)}}}，"
        f"timeframe={timeframe}，特征窗口={window}，"
        f"模式={'最新自适应放缩' if latest_only and adaptive_scale else '最新单窗口' if latest_only else '历史滑窗'}，"
        f"每标的拉取={effective_fetch_bars}根，原始窗口长度={scale_lengths if latest_only else '按step滑动'}",
        0,
    )

    for cat in active_categories:
        pool = pool_by_cat.get(cat, [])
        cat_n_symbols = counts_by_cat[cat]
        category_started = time.perf_counter()
        category_samples_before = len(samples)
        # 按当前品类独立数量取标的，若池不够则循环取。
        picked: List[Tuple[str, str]] = []
        for i in range(cat_n_symbols):
            picked.append(pool[i % len(pool)])

        if data_source.upper() == "MOCK":
            # ----------------------------------------------------------------
            # MOCK：把"标的数 N"映射到 generate_samples 的 per_type_count
            # 总样本量 ≈ N × (平均 274 窗口/标的)
            # generate_samples 默认产出 = per_type_count × 10类 × augment × 3品类
            # 我们把 augment 固定 = 5，per_type_count 计算如下：
            # ----------------------------------------------------------------
            augment = 5
            windows_per_symbol = (
                len(scale_lengths) if latest_only else (max_windows_per_symbol or 274)
            )
            per_cat_target = max(1, cat_n_symbols * windows_per_symbol)
            # generate_samples 每品类每类产出 = per_type_count × augment
            desired_per_type = max(1, math.ceil(per_cat_target / (len(SHAPE_TYPES) * augment)))
            report(
                f"[MOCK][START] {cat}: 标的={cat_n_symbols}，目标窗口≈{per_cat_target}，"
                f"per_type_count={desired_per_type}，augment={augment}"
            )
            subs = generate_samples(
                per_type_count=desired_per_type, seed=seed + hash(cat) % 100000,
                categories=(cat,), augment_per_clean=augment,
            )
            if len(subs) > per_cat_target:
                # generate_samples 以“10类×增强倍数”为最小批次，按均匀索引裁到 UI 指定规模。
                keep_idx = np.linspace(0, len(subs) - 1, per_cat_target, dtype=int)
                subs = [subs[int(i)] for i in keep_idx]
            # 修正 sample_id 避免重复
            for s in subs:
                s.sample_id = sid; sid += 1
            samples.extend(subs)
            completed_units += cat_n_symbols
            report(
                f"[MOCK][OK] {cat}: 生成 {len(subs)} 个形态，"
                f"耗时 {time.perf_counter() - category_started:.2f}s",
                completed_units,
            )
            continue

        # ---------------------------------------------------------------------
        # 真实数据源路径（BINANCE / TUSHARE）
        # ---------------------------------------------------------------------
        windows_limit = len(scale_lengths) if latest_only else (max_windows_per_symbol or 600)
        per_cat_limit_samples = cat_n_symbols * windows_limit
        taken_samples_in_cat = 0
        for idx, (sym, sname) in enumerate(picked):
            if taken_samples_in_cat >= per_cat_limit_samples:
                skipped = len(picked) - idx
                completed_units += skipped
                report(
                    f"[FETCH][LIMIT] {cat}: 已达到 {per_cat_limit_samples} 个形态上限，"
                    f"跳过剩余 {skipped} 个标的",
                    completed_units,
                )
                break
            symbol_started = time.perf_counter()
            report(
                f"[FETCH][START] {data_source.upper()} {cat} {idx + 1}/{len(picked)} "
                f"{sym} ({sname})"
            )
            ohlcv = None
            requested_bars = effective_fetch_bars if latest_only else (
                window + step * (max_windows_per_symbol - 1)
                if max_windows_per_symbol is not None and max_windows_per_symbol > 0
                else None
            )
            if data_source.upper() == "BINANCE" and cat == "crypto":
                symbol_clean = sym.split(":", 1)[-1] if ":" in sym else sym
                source_limit = {"1d": 2400, "4h": 1500, "1h": 2000}.get(timeframe, 2400)
                limit_klines = min(source_limit, requested_bars) if requested_bars else source_limit
                ohlcv = _fetch_binance_ohlcv(
                    symbol_clean, timeframe=timeframe, limit=limit_klines, log=detail_log
                )
            elif data_source.upper() == "TUSHARE" and cat == "stock":
                token = os.environ.get("TUSHARE_TOKEN", "")
                if not token:
                    # 没 key 则 fallback 到 MOCK（保证界面能跑），只对该标的
                    ohlcv = None
                else:
                    ts_code = sym.split(":", 1)[-1] if ":" in sym else sym
                    # Tushare 代码必须是带后缀的，例如 600519.SH；我们 pool 里是 SH:600519 → 调换顺序
                    if "." not in ts_code and ts_code:
                        market, rest = ts_code.split(":") if ":" in ts_code else ("SH", ts_code)
                        ts_code = f"{rest}.{market}"
                    source_limit = {"1d": 2400, "4h": 1500, "1h": 2000}.get(timeframe, 2400)
                    limit_klines = min(source_limit, requested_bars) if requested_bars else source_limit
                    ohlcv = _fetch_tushare_daily(
                        ts_code, token=token, limit=limit_klines, log=detail_log
                    )
            elif data_source.upper() == "AKSHARE":
                source_limit = {"1d": 2400, "4h": 1500, "1h": 2000}.get(timeframe, 2400)
                limit_klines = min(source_limit, requested_bars) if requested_bars else source_limit
                if cat == "crypto":
                    # AKShare 当前版本无加密货币历史 K 线 → fallback 币安 public API
                    symbol_clean = sym.split(":", 1)[-1] if ":" in sym else sym
                    ohlcv = _fetch_binance_ohlcv(symbol_clean, timeframe=timeframe,
                                                 limit=limit_klines, log=detail_log)
                else:
                    ohlcv = _fetch_akshare_ohlcv(sym, category=cat, timeframe=timeframe,
                                                 limit=limit_klines, log=detail_log)
            # 其它数据源暂时返回空（占位）

            min_required_bars = 2 if latest_only else window
            if ohlcv is None or len(ohlcv) < min_required_bars:
                # 真实接口拉不到时使用 MOCK 曲线兜底；最新模式仍保持“所有窗口结束于最新K线”。
                fallback_samples = 0
                if latest_only:
                    seg_len = effective_fetch_bars
                    close_mock = np.cumsum(rng.randn(seg_len)) * 0.02 + rng.uniform(5, 200)
                    close_mock = np.clip(close_mock, 0.01, None)
                    ts0 = time.time() - 86400 * seg_len
                    arr = np.zeros((seg_len, 5), dtype=np.float64)
                    arr[:, 0] = ts0 + np.arange(seg_len) * 86400
                    arr[:, 1] = close_mock * (1 + rng.randn(seg_len) * 0.01)
                    arr[:, 2] = close_mock * (1 + np.abs(rng.randn(seg_len)) * 0.02)
                    arr[:, 3] = close_mock * (1 - np.abs(rng.randn(seg_len)) * 0.02)
                    arr[:, 4] = close_mock
                    sub = _latest_ohlcv_to_samples(
                        arr, cat, sym, sname, timeframe,
                        feature_window=window,
                        adaptive_scale=adaptive_scale,
                        sample_id_seed=sid,
                    )
                    for s in sub:
                        s.sample_id = sid; sid += 1
                    samples.extend(sub)
                    taken_samples_in_cat += len(sub)
                    fallback_samples = len(sub)
                else:
                    n_seg = rng.randint(3, 6)
                    for _ in range(n_seg):
                        remaining_windows = (
                            max_windows_per_symbol - fallback_samples
                            if max_windows_per_symbol is not None
                            else None
                        )
                        if remaining_windows is not None and remaining_windows <= 0:
                            break
                        seg_len = rng.randint(window + 5, window + 80)
                        close_mock = np.cumsum(rng.randn(seg_len)) * 0.02 + rng.uniform(5, 200)
                        close_mock = np.clip(close_mock, 0.01, None)
                        ts0 = time.time() - 86400 * seg_len
                        arr = np.zeros((seg_len, 5), dtype=np.float64)
                        arr[:, 0] = ts0 + np.arange(seg_len) * 86400
                        arr[:, 1] = close_mock * (1 + rng.randn(seg_len) * 0.01)
                        arr[:, 2] = close_mock * (1 + np.abs(rng.randn(seg_len)) * 0.02)
                        arr[:, 3] = close_mock * (1 - np.abs(rng.randn(seg_len)) * 0.02)
                        arr[:, 4] = close_mock
                        sub = _slice_ohlcv_to_samples(
                            arr, cat, sym, sname, timeframe,
                            window=window, step=step, sample_id_seed=sid,
                            max_windows=remaining_windows,
                        )
                        for s in sub:
                            s.sample_id = sid; sid += 1
                        samples.extend(sub)
                        taken_samples_in_cat += len(sub)
                        fallback_samples += len(sub)
                completed_units += 1
                report(
                    f"[FETCH][FALLBACK] {cat} {sym}: 未取得至少 {min_required_bars} 根有效K线，"
                    f"改用 MOCK，生成 {fallback_samples} 个形态，"
                    f"耗时 {time.perf_counter() - symbol_started:.2f}s",
                    completed_units,
                )
                continue

            if latest_only:
                sub = _latest_ohlcv_to_samples(
                    ohlcv, cat, sym, sname, timeframe,
                    feature_window=window,
                    adaptive_scale=adaptive_scale,
                    sample_id_seed=sid,
                )
            else:
                sub = _slice_ohlcv_to_samples(
                    ohlcv, cat, sym, sname, timeframe,
                    window=window, step=step, sample_id_seed=sid,
                    max_windows=max_windows_per_symbol,
                )
            for s in sub:
                s.sample_id = sid; sid += 1
            samples.extend(sub)
            taken_samples_in_cat += len(sub)
            completed_units += 1
            report(
                f"[FETCH][OK] {cat} {idx + 1}/{len(picked)} {sym}: "
                f"K线={len(ohlcv)}，形态窗口={len(sub)}，品类窗口累计={taken_samples_in_cat}，"
                f"原始窗口长度={[len(s.raw_points) for s in sub]}，均结束于最新K线，"
                f"耗时 {time.perf_counter() - symbol_started:.2f}s",
                completed_units,
            )

        report(
            f"[BUILD][CATEGORY] {cat} 完成：新增 {len(samples) - category_samples_before} 个形态，"
            f"耗时 {time.perf_counter() - category_started:.2f}s",
            completed_units,
        )

    report(f"[BUILD][DONE] 共生成 {len(samples)} 个形态样本", total_units)
    return samples


__all__ = [
    "build_dataset_from_symbol_pool",
    "_slice_ohlcv_to_samples",
    "_latest_ohlcv_to_samples",
    "_fetch_binance_ohlcv",
    "_fetch_tushare_daily",
    "_fetch_akshare_ohlcv",
]

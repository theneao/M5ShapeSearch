# -*- coding: utf-8 -*-
"""
AKShare A 股 + Binance Public Crypto 市场数据构建器。

创建时间：2026-08-28
作用：A 股按市值/成交量/量比及行业选池，Crypto 使用 Binance 公开 USDT 现货行情；
      两类数据都只保存最新连续 K 线并转换为 CPU 多尺度搜索样本，禁止 MOCK 回退。
使用方式：build_market_dataset(config, timeframe, log, progress)；
          参数页板块下拉框调用 list_stock_sectors()。

修改时间：2026-08-28
修改作用：Crypto 从 AKShare 无历史快照改为 Binance Spot 公共 K 线，并接入共享权重限速客户端。

修改时间：2026-08-28
修改作用：Crypto 使用可配置的小规模并发隐藏网络延迟，请求启动速度仍由共享权重限速器统一控制。

修改时间：2026-08-29
修改作用：A 股增加 5m/15m/30m/60m 分钟 K 线，并保留旧 1h 到 60m 的兼容映射。

修改时间：2026-08-29
修改作用：取消逐标的请求/成功/数据不足日志，改为进度回调和分品类 built/skipped 汇总；单标的异常仍保留。

修改时间：2026-08-29
修改作用：选池数量 0 表示全部候选，不再误清空品类缓存。
"""
from __future__ import annotations
# 修改时间：2026-08-29
# 修改作用：A 股实时排名接口失败时复用已缓存标的池，仍继续检查这些标的的新 K 线。
# 使用方式：MarketDataService 会按周期注入 _fallback_stock_pool；首次建库且无缓存时仍明确报错。

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .mock_shape_generator import ShapeSample
from .real_data_builder import _latest_ohlcv_to_samples
from .binance_public_data import (
    BinancePublicError,
    BinanceRateLimitError,
    get_binance_client,
)


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, str], None]

STOCK_METRICS = {
    "market_cap": "总市值",
    "volume": "成交量",
    "volume_ratio": "量比",
}
CRYPTO_METRICS = {
    "volume": "24小时成交量",
}


def _log(callback: Optional[LogCallback], message: str) -> None:
    if callback:
        callback(message)


def _numeric(frame: Any, column: str):
    import pandas as pd

    return pd.to_numeric(frame[column], errors="coerce")


def _stock_symbol(code: str) -> str:
    code = str(code).strip().zfill(6)
    if code.startswith(("4", "8")):
        return f"BJ:{code}"
    if code.startswith(("5", "6", "9")):
        return f"SH:{code}"
    return f"SZ:{code}"


def list_stock_sectors() -> List[str]:
    """返回 AKShare 东财行业板块名称；网络失败由调用方显示。"""
    import akshare as ak

    frame = ak.stock_board_industry_name_em()
    if frame is None or "板块名称" not in frame.columns:
        return []
    return sorted({str(value).strip() for value in frame["板块名称"].dropna() if str(value).strip()})


def select_stock_pool(
    count: int,
    metric: str,
    order: str,
    sector: str = "all",
    log: Optional[LogCallback] = None,
) -> List[Tuple[str, str]]:
    """从 A 股实时行情中按指定指标选前/后 N 只，可限定行业板块。"""
    import akshare as ak

    metric_column = STOCK_METRICS.get(metric)
    if metric_column is None:
        raise ValueError(f"不支持的 A 股排序指标: {metric}")
    _log(log, "[AKSHARE][POOL] 请求沪深京 A 股实时排名")
    frame = ak.stock_zh_a_spot_em()
    required = {"代码", "名称", metric_column}
    if frame is None or not required.issubset(set(frame.columns)):
        missing = sorted(required - set(frame.columns if frame is not None else []))
        raise RuntimeError(f"AKShare A 股实时行情缺少字段: {missing}")

    sector = str(sector or "all").strip()
    if sector.lower() != "all":
        _log(log, f"[AKSHARE][POOL] 请求行业板块成分: {sector}")
        members = ak.stock_board_industry_cons_em(symbol=sector)
        if members is None or "代码" not in members.columns:
            raise RuntimeError(f"行业板块 {sector} 未返回有效成分股")
        codes = {str(value).strip().zfill(6) for value in members["代码"].dropna()}
        frame = frame[frame["代码"].astype(str).str.zfill(6).isin(codes)]

    frame = frame.copy()
    frame["_rank_value"] = _numeric(frame, metric_column)
    frame = frame[np.isfinite(frame["_rank_value"].to_numpy(dtype=float, na_value=np.nan))]
    frame = frame.sort_values("_rank_value", ascending=(order == "bottom"))
    frame = frame.drop_duplicates(subset=["代码"])
    if int(count) > 0:
        frame = frame.head(int(count))
    selected = [
        (_stock_symbol(str(row["代码"])), str(row["名称"]))
        for _, row in frame.iterrows()
    ]
    _log(
        log,
        f"[AKSHARE][POOL][OK] A股 sector={sector}, metric={metric}/{order}, "
        f"requested={count}, selected={len(selected)}",
    )
    return selected


def inspect_crypto_pool(
    count: int,
    metric: str,
    order: str,
    log: Optional[LogCallback] = None,
) -> Tuple[List[Tuple[str, str]], str]:
    """
    检查 AKShare 虚拟货币快照能力。

    AKShare 1.18.x 仅 crypto_js_spot 快照，没有可用于形态检索的历史 OHLC；
    因此只返回可见标的供参数页诊断，不伪造 K 线样本。
    """
    import akshare as ak

    frame = ak.crypto_js_spot()
    if frame is None or len(frame) == 0:
        return [], "AKShare crypto_js_spot 返回空数据"
    if metric not in CRYPTO_METRICS:
        return [], (
            f"AKShare crypto_js_spot 不提供 {metric} 字段；"
            "当前只能按 24 小时成交量排序"
        )
    column = CRYPTO_METRICS[metric]
    required = {"市场", "交易品种", column, "更新时间"}
    if not required.issubset(set(frame.columns)):
        return [], f"AKShare crypto_js_spot 缺少字段: {sorted(required - set(frame.columns))}"
    frame = frame.copy()
    frame["_rank_value"] = _numeric(frame, column)
    frame = frame.sort_values("_rank_value", ascending=(order == "bottom"))
    frame = frame.drop_duplicates(subset=["交易品种"])
    if int(count) > 0:
        frame = frame.head(int(count))
    pool = [
        (f"AKCRYPTO:{row['交易品种']}", f"{row['交易品种']} @ {row['市场']}")
        for _, row in frame.iterrows()
    ]
    newest = str(frame["更新时间"].max()) if len(frame) else "unknown"
    message = (
        f"AKShare 仅返回 {len(frame)} 条虚拟货币快照（最新时间 {newest}），"
        "且无历史 OHLC K 线，本次不构建 crypto 形态库"
    )
    _log(log, f"[AKSHARE][CRYPTO][UNSUPPORTED] {message}")
    return pool, message


def _frame_to_ohlcv(frame: Any, mapping: Dict[str, str], limit: int) -> Optional[np.ndarray]:
    if frame is None or len(frame) == 0:
        return None
    required = set(mapping.values())
    if not required.issubset(set(frame.columns)):
        return None
    frame = frame.copy().sort_values(mapping["time"]).tail(max(2, int(limit))).reset_index(drop=True)
    rows: List[List[float]] = []
    for _, row in frame.iterrows():
        try:
            value = row[mapping["time"]]
            timestamp = float(datetime.fromisoformat(str(value).replace("/", "-")).timestamp())
            ohlc = [float(row[mapping[key]]) for key in ("open", "high", "low", "close")]
        except (TypeError, ValueError, OverflowError):
            continue
        if np.isfinite([timestamp, *ohlc]).all() and min(ohlc) > 0:
            rows.append([timestamp, *ohlc])
    return np.asarray(rows, dtype=np.float64) if len(rows) >= 2 else None


def fetch_stock_ohlcv(
    symbol: str,
    timeframe: str,
    limit: int,
    log: Optional[LogCallback] = None,
) -> Optional[np.ndarray]:
    """AKShare A 股 5/15/30/60 分钟、日/周线；4h 由当日 60m K 线聚合。"""
    import akshare as ak
    import pandas as pd

    code = str(symbol).split(":")[-1]
    end_date = datetime.now().strftime("%Y%m%d")
    canonical_timeframe = "60m" if timeframe == "1h" else timeframe
    minute_periods = {"5m": "5", "15m": "15", "30m": "30", "60m": "60", "4h": "60"}
    bars_per_day = {"5m": 48, "15m": 16, "30m": 8, "60m": 4, "4h": 1}
    if canonical_timeframe in minute_periods:
        lookback_days = max(30, int(limit / bars_per_day[canonical_timeframe] * 3) + 10)
    else:
        lookback_days = max(400, int(limit * (10 if canonical_timeframe == "1w" else 3)))
    start_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
    try:
        if canonical_timeframe in {"1d", "1w"}:
            period = "weekly" if canonical_timeframe == "1w" else "daily"
            frame = ak.stock_zh_a_hist(
                symbol=code,
                period=period,
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            )
            return _frame_to_ohlcv(
                frame,
                {"time": "日期", "open": "开盘", "high": "最高", "low": "最低", "close": "收盘"},
                limit,
            )

        if canonical_timeframe not in minute_periods:
            raise ValueError(f"不支持的周期: {timeframe}")
        start_dt = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d 09:30:00")
        end_dt = datetime.now().strftime("%Y-%m-%d 15:00:00")
        ak_period = minute_periods[canonical_timeframe]
        frame = ak.stock_zh_a_hist_min_em(
            symbol=code,
            start_date=start_dt,
            end_date=end_dt,
            period=ak_period,
            adjust="qfq",
        )
        if canonical_timeframe == "4h" and frame is not None and len(frame):
            frame = frame.copy()
            frame["时间"] = pd.to_datetime(frame["时间"])
            frame["_day"] = frame["时间"].dt.date
            frame = frame.groupby("_day", as_index=False).agg({
                "时间": "last",
                "开盘": "first",
                "最高": "max",
                "最低": "min",
                "收盘": "last",
            })
        return _frame_to_ohlcv(
            frame,
            {"time": "时间", "open": "开盘", "high": "最高", "low": "最低", "close": "收盘"},
            limit,
        )
    except Exception as exc:
        _log(log, f"[AKSHARE][KLINE][ERROR] {symbol}: {type(exc).__name__}: {exc}")
        return None


def build_market_dataset(
    config: Dict[str, Any],
    timeframe: str,
    log: Optional[LogCallback] = None,
    progress: Optional[ProgressCallback] = None,
) -> Tuple[List[ShapeSample], Dict[str, Any]]:
    """按一个周期构建最新连续序列，返回样本与分品类结果。"""
    if timeframe == "1h":
        timeframe = "60m"
    if timeframe not in {"5m", "15m", "30m", "60m", "4h", "1d", "1w"}:
        raise ValueError(f"无效周期: {timeframe}")
    samples: List[ShapeSample] = []
    report: Dict[str, Any] = {"timeframe": timeframe, "categories": {}, "warnings": []}
    window = max(10, int(config.get("feature_window", 60)))
    fetch_bars = max(window, int(config.get("fetch_bars", 300)))
    adaptive = bool(config.get("adaptive_scale", True))
    jobs: List[Tuple[str, str, str]] = []
    refresh_categories = {
        str(item) for item in config.get("_refresh_categories", ("stock", "crypto"))
    }

    stock_cfg = config.get("stock", {})
    fallback_stock_pool: List[Tuple[str, str]] = []
    for item in config.get("_fallback_stock_pool", []):
        if isinstance(item, dict):
            symbol = str(item.get("symbol", "")).strip()
            name = str(item.get("name", "")).strip()
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            symbol, name = str(item[0]).strip(), str(item[1]).strip()
        else:
            continue
        if symbol:
            fallback_stock_pool.append((symbol, name))
    if "stock" not in refresh_categories:
        pass
    elif stock_cfg.get("enabled", True):
        try:
            pool = select_stock_pool(
                int(stock_cfg.get("count", 300)),
                str(stock_cfg.get("rank_metric", "market_cap")),
                str(stock_cfg.get("rank_order", "top")),
                str(stock_cfg.get("sector", "all")),
                log,
            )
            jobs.extend(("stock", symbol, name) for symbol, name in pool)
            report["categories"]["stock"] = {
                "source": "AKSHARE",
                "requested": int(stock_cfg.get("count", 300)),
                "selected": len(pool),
                "complete": True,
                "clear": False,
            }
        except Exception as exc:
            if fallback_stock_pool:
                jobs.extend(("stock", symbol, name) for symbol, name in fallback_stock_pool)
                report["categories"]["stock"] = {
                    "source": "AKSHARE_CACHED_POOL",
                    "requested": int(stock_cfg.get("count", 300)),
                    "selected": len(fallback_stock_pool),
                    "complete": True,
                    "clear": False,
                    "pool_fallback": True,
                    "pool_error": f"{type(exc).__name__}: {exc}",
                }
                report["warnings"].append(
                    f"A 股实时排名不可用，已复用 {len(fallback_stock_pool)} 个缓存标的检查新 K 线"
                )
                _log(
                    log,
                    f"[AKSHARE][POOL][FALLBACK] ranking failed; reuse cached symbols="
                    f"{len(fallback_stock_pool)}",
                )
            else:
                report["categories"]["stock"] = {
                    "source": "AKSHARE", "selected": 0, "complete": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                report["warnings"].append(f"A股标的池获取失败: {exc}")
    else:
        report["categories"]["stock"] = {
            "source": "AKSHARE", "selected": 0, "built": 0, "complete": True, "clear": True,
        }

    crypto_cfg = config.get("crypto", {})
    binance_client = None
    if "crypto" not in refresh_categories:
        pass
    elif crypto_cfg.get("enabled", True):
        try:
            binance_client = get_binance_client(config)
            pool, warnings = binance_client.select_crypto_pool(
                int(crypto_cfg.get("count", 300)),
                str(crypto_cfg.get("rank_metric", "volume")),
                str(crypto_cfg.get("rank_order", "top")),
                str(crypto_cfg.get("quote_asset", "USDT")),
                log,
            )
            jobs.extend(("crypto", symbol, name) for symbol, name in pool)
            report["categories"]["crypto"] = {
                "source": "BINANCE_PUBLIC",
                "requested": int(crypto_cfg.get("count", 300)),
                "selected": len(pool),
                "complete": True,
                "clear": False,
            }
            report["warnings"].extend(warnings)
        except Exception as exc:
            report["categories"]["crypto"] = {
                "source": "BINANCE_PUBLIC", "selected": 0, "built": 0, "complete": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            report["warnings"].append(f"Binance Crypto 标的池获取失败: {exc}")
    else:
        report["categories"]["crypto"] = {
            "source": "BINANCE_PUBLIC", "selected": 0, "built": 0, "complete": True, "clear": True,
        }

    total = len(jobs)
    built_by_category: Dict[str, int] = {}
    skipped_by_category: Dict[str, int] = {}

    def consume(category: str, symbol: str, name: str, ohlcv: Optional[np.ndarray]) -> None:
        if ohlcv is None or len(ohlcv) < max(4, min(window, 10)):
            skipped_by_category[category] = skipped_by_category.get(category, 0) + 1
            return
        generated = _latest_ohlcv_to_samples(
            ohlcv,
            category,
            symbol,
            name,
            timeframe,
            feature_window=window,
            adaptive_scale=False,
            sample_id_seed=len(samples),
        )
        if not generated:
            skipped_by_category[category] = skipped_by_category.get(category, 0) + 1
            return
        samples.extend(generated)
        built_by_category[category] = built_by_category.get(category, 0) + len(generated)

    completed = 0
    stock_jobs = [job for job in jobs if job[0] == "stock"]
    crypto_jobs = [job for job in jobs if job[0] == "crypto"]
    for category, symbol, name in stock_jobs:
        if progress:
            progress(completed, total, f"{timeframe} stock {completed + 1}/{total} {symbol}")
        consume(category, symbol, name, fetch_stock_ohlcv(symbol, timeframe, fetch_bars, log))
        completed += 1

    if crypto_jobs and binance_client is not None:
        concurrency = min(8, max(1, int(crypto_cfg.get("binance_concurrency", 4))))
        _log(
            log,
            f"[BINANCE][BATCH] jobs={len(crypto_jobs)}, concurrency={concurrency}, "
            f"interval_ms={binance_client.limiter.status()['request_interval_ms']}",
        )

        def fetch_crypto(job: Tuple[str, str, str]):
            _category, symbol, _name = job
            return binance_client.fetch_ohlcv(symbol, timeframe, fetch_bars, log)

        fatal_error: Optional[Exception] = None
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="binance-kline") as executor:
            future_map = {executor.submit(fetch_crypto, job): job for job in crypto_jobs}
            for future in as_completed(future_map):
                category, symbol, name = future_map[future]
                completed += 1
                if progress:
                    progress(completed, total, f"{timeframe} crypto {completed}/{total} {symbol}")
                try:
                    consume(category, symbol, name, future.result())
                except (BinanceRateLimitError, BinancePublicError) as exc:
                    fatal_error = fatal_error or exc
        if fatal_error is not None:
            details = report["categories"].setdefault("crypto", {})
            details["complete"] = False
            details["error"] = f"{type(fatal_error).__name__}: {fatal_error}"
            report["warnings"].append(f"Binance Crypto 批次未完整完成: {fatal_error}")
            _log(log, f"[BINANCE][ABORT] {type(fatal_error).__name__}: {fatal_error}")
    if progress:
        progress(total, total, f"{timeframe} 采集完成")
    for category, details in report["categories"].items():
        details["built"] = built_by_category.get(category, 0)
        details["skipped"] = skipped_by_category.get(category, 0)
    if binance_client is not None:
        report["binance_rate"] = binance_client.limiter.status()
    report["samples"] = len(samples)
    report["fetch_bars"] = fetch_bars
    report["adaptive_scale"] = adaptive
    return samples, report


# 兼容旧导入名；当前实现已是 AKShare + Binance Public 双数据源。
build_akshare_dataset = build_market_dataset


__all__ = [
    "STOCK_METRICS",
    "CRYPTO_METRICS",
    "list_stock_sectors",
    "select_stock_pool",
    "inspect_crypto_pool",
    "fetch_stock_ohlcv",
    "build_market_dataset",
    "build_akshare_dataset",
]

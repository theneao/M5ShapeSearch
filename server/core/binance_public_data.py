# -*- coding: utf-8 -*-
"""
Binance Spot 公共行情客户端与 Crypto 数据构建辅助。

创建时间：2026-08-28
作用：通过无需 API Key 的 /api/v3/exchangeInfo、/api/v3/ticker/24hr 和
      /api/v3/klines 获取 USDT 现货池与最新已收盘 K 线；统一执行请求权重限速、
      X-MBX-USED-WEIGHT 监测、429/418 Retry-After 阻断及 5xx 退避。
使用方式：get_binance_client(config, log) 返回进程级共享客户端；市场数据后台线程
          调用 select_crypto_pool() 和 fetch_ohlcv()，手绘查询不得直接调用本模块。

修改时间：2026-08-28
修改作用：为受控并发抓取使用线程本地 HTTP Session；所有线程仍共享同一个权重限速器。

修改时间：2026-08-28
修改作用：切换到 Binance 官方仅市场数据域名，并增加固定公开 GET 路径白名单；不读取、保存或发送 API Key/Secret。

修改时间：2026-08-29
修改作用：增加 5m/15m/30m/60m；服务端 60m 显示值在 Binance 请求时映射为官方 interval=1h。

修改时间：2026-08-29
修改作用：默认关闭逐请求 HTTP/KLINE 成功日志，只保留选池、批次、限流、重试和异常信息；加载进度由市场数据服务统一显示。

修改时间：2026-08-29
修改作用：Crypto 选池支持 24h 成交额、基础币成交量、成交笔数和涨跌幅；count=0 返回全部合格交易对。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import numpy as np
import requests


LogCallback = Callable[[str], None]


class BinancePublicError(RuntimeError):
    """Binance 公共行情请求失败。"""


class BinanceRateLimitError(BinancePublicError):
    """本地预算、429 或 418 导致当前批次必须停止。"""


class WeightedRateLimiter:
    """进程级滑动一分钟权重预算，并限制相邻请求速度。"""

    def __init__(self, weight_limit: int = 1200, interval_ms: int = 150):
        self._lock = threading.Lock()
        self._events: Deque[Tuple[float, int]] = deque()
        self._weight_limit = max(100, int(weight_limit))
        self._interval = max(0.05, int(interval_ms) / 1000.0)
        self._last_request = 0.0
        self._blocked_until = 0.0
        self._server_used_weight = 0

    def configure(self, weight_limit: int, interval_ms: int) -> None:
        with self._lock:
            self._weight_limit = min(4800, max(100, int(weight_limit)))
            self._interval = max(0.05, int(interval_ms) / 1000.0)

    def block_for(self, seconds: float) -> None:
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + max(1.0, seconds))

    def acquire(self, weight: int, log: Optional[LogCallback] = None) -> None:
        weight = max(1, int(weight))
        announced_wait = False
        while True:
            with self._lock:
                now = time.monotonic()
                if self._blocked_until > now:
                    remaining = self._blocked_until - now
                    raise BinanceRateLimitError(f"Binance 限流阻断中，约 {remaining:.1f}s 后可重试")
                while self._events and now - self._events[0][0] >= 60.0:
                    self._events.popleft()
                used = sum(item[1] for item in self._events)
                interval_wait = max(0.0, self._interval - (now - self._last_request))
                budget_wait = 0.0
                if used + weight > self._weight_limit and self._events:
                    budget_wait = max(0.0, 60.0 - (now - self._events[0][0]) + 0.05)
                wait_seconds = max(interval_wait, budget_wait)
                if wait_seconds <= 0.0:
                    timestamp = time.monotonic()
                    self._events.append((timestamp, weight))
                    self._last_request = timestamp
                    return
            if budget_wait > 0.0 and log and not announced_wait:
                log(
                    f"[BINANCE][RATE] 本地一分钟权重预算 {self._weight_limit} 已到，"
                    f"等待 {wait_seconds:.1f}s"
                )
                announced_wait = True
            time.sleep(min(wait_seconds, 1.0))

    def observe_headers(self, headers: Any, log: Optional[LogCallback] = None) -> None:
        raw = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
        try:
            used = int(raw)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._server_used_weight = used
        # Binance 当前公开上限为 6000/min；达到 80% 时主动等到下一自然分钟。
        if used >= 4800:
            pause = 60.5 - (time.time() % 60.0)
            self.block_for(pause)
            if log:
                log(f"[BINANCE][RATE] 服务端已用权重={used}/6000，暂停约 {pause:.1f}s")

    def status(self) -> Dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            recent = [(stamp, weight) for stamp, weight in self._events if now - stamp < 60.0]
            return {
                "local_weight": sum(weight for _, weight in recent),
                "local_limit": self._weight_limit,
                "server_used_weight_1m": self._server_used_weight,
                "request_interval_ms": int(self._interval * 1000),
                "blocked_seconds": round(max(0.0, self._blocked_until - now), 2),
            }


class BinancePublicClient:
    """仅提供公开市场数据，不包含签名、账户或交易接口。"""

    BASE_URL = "https://data-api.binance.vision"
    PUBLIC_PATHS = {
        "/api/v3/exchangeInfo",
        "/api/v3/ticker/24hr",
        "/api/v3/klines",
    }
    STABLE_BASES = {
        "USDT", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "USD1", "USDE", "USDS",
        "BFUSD", "RLUSD", "PYUSD", "EUR", "TRY",
    }

    def __init__(self, session: Optional[requests.Session] = None):
        self._provided_session = session
        self._thread_local = threading.local()
        if self._provided_session is not None:
            self._provided_session.headers.update({"User-Agent": "M5ShapeSearch/1.0 public-market-data"})
        self.limiter = WeightedRateLimiter()
        self.retry_count = 3
        self.max_retry_after_seconds = 60
        self._exchange_cache: Optional[Dict[str, Any]] = None
        self._exchange_cache_until = 0.0
        self._cache_lock = threading.Lock()

    def _session(self):
        if self._provided_session is not None:
            return self._provided_session
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": "M5ShapeSearch/1.0 public-market-data"})
            self._thread_local.session = session
        return session

    def configure(
        self,
        weight_limit: int = 1200,
        interval_ms: int = 150,
        retry_count: int = 3,
        max_retry_after_seconds: int = 60,
    ) -> None:
        self.limiter.configure(weight_limit, interval_ms)
        self.retry_count = min(5, max(0, int(retry_count)))
        self.max_retry_after_seconds = min(300, max(1, int(max_retry_after_seconds)))

    def _request_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]],
        weight: int,
        log: Optional[LogCallback],
    ) -> Any:
        if path not in self.PUBLIC_PATHS:
            raise BinancePublicError(f"拒绝非白名单 Binance 路径: {path}")
        last_error: Optional[Exception] = None
        for attempt in range(self.retry_count + 1):
            self.limiter.acquire(weight, log)
            try:
                response = self._session().get(
                    f"{self.BASE_URL}{path}", params=params, timeout=(5.0, 20.0)
                )
                self.limiter.observe_headers(response.headers, log)
                if response.status_code in {418, 429}:
                    try:
                        retry_after = float(response.headers.get("Retry-After", "60"))
                    except (TypeError, ValueError):
                        retry_after = 60.0
                    self.limiter.block_for(retry_after)
                    message = f"Binance HTTP {response.status_code}，Retry-After={retry_after:.0f}s"
                    if attempt < self.retry_count and retry_after <= self.max_retry_after_seconds:
                        if log:
                            log(f"[BINANCE][BACKOFF] {message}")
                        time.sleep(retry_after + 0.1)
                        continue
                    raise BinanceRateLimitError(message)
                response.raise_for_status()
                return response.json()
            except BinanceRateLimitError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt >= self.retry_count:
                    break
                delay = min(8.0, float(2 ** attempt))
                if log:
                    log(
                        f"[BINANCE][RETRY] {type(exc).__name__}: {exc}; "
                        f"{delay:.1f}s 后重试 {attempt + 1}/{self.retry_count}"
                    )
                time.sleep(delay)
        raise BinancePublicError(f"Binance 请求失败: {type(last_error).__name__}: {last_error}")

    def exchange_info(self, log: Optional[LogCallback] = None) -> Dict[str, Any]:
        with self._cache_lock:
            if self._exchange_cache is not None and time.monotonic() < self._exchange_cache_until:
                return self._exchange_cache
        payload = self._request_json("/api/v3/exchangeInfo", None, 20, log)
        if not isinstance(payload, dict):
            raise BinancePublicError("exchangeInfo 返回格式无效")
        with self._cache_lock:
            self._exchange_cache = payload
            self._exchange_cache_until = time.monotonic() + 6 * 3600
        return payload

    def select_crypto_pool(
        self,
        count: int,
        metric: str,
        order: str,
        quote_asset: str = "USDT",
        log: Optional[LogCallback] = None,
    ) -> Tuple[List[Tuple[str, str]], List[str]]:
        quote_asset = str(quote_asset or "USDT").upper()
        info = self.exchange_info(log)
        symbol_meta: Dict[str, Dict[str, Any]] = {}
        for item in info.get("symbols", []):
            if (
                item.get("status") == "TRADING"
                and item.get("quoteAsset") == quote_asset
                and item.get("isSpotTradingAllowed", True)
            ):
                symbol_meta[str(item.get("symbol", ""))] = item

        tickers = self._request_json("/api/v3/ticker/24hr", None, 80, log)
        if not isinstance(tickers, list):
            raise BinancePublicError("24hr ticker 返回格式无效")
        metric_fields = {
            "volume": ("quoteVolume", "24h_quote_volume"),
            "base_volume": ("volume", "24h_base_volume"),
            "trade_count": ("count", "24h_trade_count"),
            "change_pct": ("priceChangePercent", "24h_change_pct"),
        }
        warnings: List[str] = []
        if metric not in metric_fields:
            warnings.append(
                f"Binance Spot 公共接口不提供 {metric}，Crypto 排名已改用 24h quoteVolume"
            )
            metric = "volume"
        metric_field, metric_label = metric_fields[metric]
        ranked: List[Tuple[float, str, str]] = []
        for item in tickers:
            symbol = str(item.get("symbol", ""))
            meta = symbol_meta.get(symbol)
            if meta is None or str(meta.get("baseAsset", "")) in self.STABLE_BASES:
                continue
            try:
                rank_value = float(item.get(metric_field, 0.0))
            except (TypeError, ValueError):
                continue
            if not np.isfinite(rank_value) or rank_value < 0:
                continue
            base_asset = str(meta.get("baseAsset", symbol.removesuffix(quote_asset)))
            ranked.append((rank_value, symbol, f"{base_asset}/{quote_asset}"))
        ranked.sort(key=lambda value: value[0], reverse=(order != "bottom"))
        selected = ranked if int(count) <= 0 else ranked[:int(count)]
        pool = [
            (f"BINANCE:{symbol}", name)
            for _, symbol, name in selected
        ]
        if log:
            log(
                f"[BINANCE][POOL][OK] quote={quote_asset}, metric={metric_label}/{order}, "
                f"requested={count}, selected={len(pool)}"
            )
        return pool, warnings

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        log: Optional[LogCallback] = None,
    ) -> Optional[np.ndarray]:
        pair = str(symbol).split(":")[-1].upper()
        interval = "1h" if timeframe in {"1h", "60m"} else timeframe
        if interval not in {"5m", "15m", "30m", "1h", "4h", "1d", "1w"}:
            raise ValueError(f"Binance 不支持周期: {timeframe}")
        requested_limit = min(1000, max(2, int(limit) + 1))
        payload = self._request_json(
            "/api/v3/klines",
            {"symbol": pair, "interval": interval, "limit": requested_limit},
            2,
            log,
        )
        if not isinstance(payload, list):
            raise BinancePublicError(f"{pair} klines 返回格式无效")
        now_ms = int(time.time() * 1000)
        rows: List[List[float]] = []
        for item in payload:
            try:
                if len(item) < 7 or int(item[6]) > now_ms:
                    continue
                values = [float(item[index]) for index in (1, 2, 3, 4, 5, 7)]
                timestamp = int(item[0]) // 1000
            except (TypeError, ValueError, IndexError):
                continue
            if timestamp > 0 and np.isfinite(values).all() and min(values[:4]) > 0:
                rows.append([float(timestamp), *values])
        return np.asarray(rows[-int(limit):], dtype=np.float64) if len(rows) >= 2 else None


_CLIENT = BinancePublicClient()


def get_binance_client(config: Dict[str, Any]) -> BinancePublicClient:
    crypto = config.get("crypto", {})
    _CLIENT.configure(
        weight_limit=int(crypto.get("binance_weight_limit_per_minute", 1200)),
        interval_ms=int(crypto.get("binance_request_interval_ms", 150)),
        retry_count=int(crypto.get("binance_retry_count", 3)),
        max_retry_after_seconds=int(crypto.get("binance_max_retry_after_seconds", 60)),
    )
    return _CLIENT


__all__ = [
    "BinancePublicError",
    "BinanceRateLimitError",
    "WeightedRateLimiter",
    "BinancePublicClient",
    "get_binance_client",
]

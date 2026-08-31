# -*- coding: utf-8 -*-
"""
市场数据预加载、持久化与周期刷新服务。

创建时间：2026-08-28
作用：服务启动时立即加载已保存的 CPU 连续序列，后台按 K 线周期检查新周期，
      仅在需要时用 AKShare(A股)+Binance Public(Crypto) 重建影子数据并原子替换。
      手绘查询只搜索内存库。
使用方式：MarketDataService(manager).start()；参数页通过 update_config()、request_refresh() 控制。

修改时间：2026-08-28
修改作用：仅加载清单明确标记为受支持数据源的缓存，旧版未知/MOCK 缓存保留在磁盘但不进入搜索结果。

修改时间：2026-08-28
修改作用：启动和保存设置时立即应用多尺度查询范围，不必等下一轮行情刷新。

修改时间：2026-08-28
修改作用：Crypto 接入 Binance Public，并持久化保守权重预算、请求间隔和重试参数。

修改时间：2026-08-29
修改作用：新增 5m/15m/30m/60m，旧 1h 自动归一为 60m；分钟刷新 token 对 A 股按交易时段冻结，
          收盘、午休和周末不会反复拉取相同数据。

修改时间：2026-08-29
修改作用：加载过程改为结构化进度事件，终端每 5% 更新一次单行进度条；状态 API 仍保存实时精确进度。

修改时间：2026-08-29
修改作用：数量 0 改为处理全部候选；A 股与 Binance 分别校验可用排序指标，Crypto 支持成交额、
          基础币成交量、成交笔数和涨跌幅，不再把不支持的指标伪装成市值。

修改时间：2026-08-29
修改作用：周期刷新以新抓取数据的末根 K 线时间为准；没有新 K 线只记录已检查、不替换缓存。
          搜索请求遇到缺失周期时可自动追加周期并在后台建库。
使用方式：路由调用 ensure_timeframe()；状态接口通过 buckets/last_kline_ts 展示缓存看板。

修改时间：2026-08-30
修改作用：A 股实时排名失败时复用当前缓存标的检查新 K 线；失败任务按 15/30/60/120 分钟退避，
          避免东财或系统代理异常时每轮重复请求和刷屏。活动品类固定为 A 股与虚拟货币。
使用方式：自动刷新遵守退避时间；设置页手动强制刷新仍可立即重试，状态接口返回 retry_backoff。

修改时间：2026-08-30
修改作用：按需搜索不再无条件唤醒刷新线程；已运行、已排队和失败冷却分别返回明确状态及建议等待时间。
使用方式：匹配路由调用 ensure_timeframe()，客户端读取 state/queued/retry_after_seconds 决定等待或停止。

修改时间：2026-08-30
修改作用：按需请求可只补齐实际缺失的 A 股或 Crypto 桶；首次用户请求可越过一次旧失败退避，
          同一品类五分钟内去重，避免硬件轮询重复下载。
使用方式：路由调用 ensure_timeframe(timeframe, categories=missing, user_requested=True)。

修改时间：2026-08-31
修改作用：目标周期首次建库且实时选池失败时，可复用任意已建 A 股周期的去重标的池；
          不再要求该目标周期必须已有缓存，新浪快照偶发解析失败也能继续下载 K 线。
使用方式：刷新线程自动注入跨周期 _fallback_stock_pool，无需页面或客户端改动。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shape_search import CpuSearchConfig

from .akshare_data_builder import build_market_dataset, list_stock_sectors
from .cpu_shape_search_manager import CpuShapeSearchManager


log = logging.getLogger("market-data-service")

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "data_source": "AKSHARE+BINANCE_PUBLIC",
    "timeframes": ["1d"],
    "feature_window": 60,
    "fetch_bars": 300,
    "adaptive_scale": True,
    "refresh_check_seconds": 300,
    "stock": {
        "enabled": True,
        "count": 300,
        "rank_metric": "market_cap",
        "rank_order": "top",
        "sector": "all",
    },
    "crypto": {
        "enabled": True,
        "count": 300,
        "rank_metric": "volume",
        "rank_order": "top",
        "quote_asset": "USDT",
        "binance_request_interval_ms": 150,
        "binance_weight_limit_per_minute": 1200,
        "binance_concurrency": 4,
        "binance_retry_count": 3,
        "binance_max_retry_after_seconds": 60,
    },
}

VALID_TIMEFRAMES = ("5m", "15m", "30m", "60m", "4h", "1d", "1w")
TIMEFRAME_ALIASES = {"1h": "60m"}
VALID_STOCK_METRICS = ("market_cap", "volume", "volume_ratio")
VALID_CRYPTO_METRICS = ("volume", "base_volume", "trade_count", "change_pct")
VALID_ORDERS = ("top", "bottom")


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def normalize_timeframe(value: Any) -> str:
    """统一使用 60m；继续接受旧固件/旧配置发送的 1h。"""
    text = str(value)
    return TIMEFRAME_ALIASES.get(text, text)


def normalize_config(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """合并默认值并约束页面/API 参数边界。"""
    config = _deep_merge(DEFAULT_CONFIG, value or {})
    config["data_source"] = "AKSHARE+BINANCE_PUBLIC"
    candidates = [normalize_timeframe(item) for item in config.get("timeframes", [])]
    timeframes = [item for item in candidates if item in VALID_TIMEFRAMES]
    config["timeframes"] = list(dict.fromkeys(timeframes)) or ["1d"]
    config["feature_window"] = min(300, max(10, int(config.get("feature_window", 60))))
    config["fetch_bars"] = min(1000, max(config["feature_window"], int(config.get("fetch_bars", 300))))
    config["adaptive_scale"] = bool(config.get("adaptive_scale", True))
    config["refresh_check_seconds"] = min(3600, max(60, int(config.get("refresh_check_seconds", 300))))
    for category in ("stock", "crypto"):
        item = config[category]
        item["enabled"] = bool(item.get("enabled", True))
        item["count"] = min(3000, max(0, int(item.get("count", 300))))
        order = str(item.get("rank_order", "top"))
        item["rank_order"] = order if order in VALID_ORDERS else "top"
    stock_metric = str(config["stock"].get("rank_metric", "market_cap"))
    config["stock"]["rank_metric"] = (
        stock_metric if stock_metric in VALID_STOCK_METRICS else "market_cap"
    )
    config["stock"]["sector"] = str(config["stock"].get("sector", "all") or "all")
    crypto = config["crypto"]
    crypto_metric = str(crypto.get("rank_metric", "volume"))
    crypto["rank_metric"] = (
        crypto_metric if crypto_metric in VALID_CRYPTO_METRICS else "volume"
    )
    crypto["quote_asset"] = str(crypto.get("quote_asset", "USDT") or "USDT").upper()
    crypto["binance_request_interval_ms"] = min(
        2000, max(50, int(crypto.get("binance_request_interval_ms", 150)))
    )
    crypto["binance_weight_limit_per_minute"] = min(
        4800, max(100, int(crypto.get("binance_weight_limit_per_minute", 1200)))
    )
    crypto["binance_concurrency"] = min(
        8, max(1, int(crypto.get("binance_concurrency", 4)))
    )
    crypto["binance_retry_count"] = min(5, max(0, int(crypto.get("binance_retry_count", 3))))
    crypto["binance_max_retry_after_seconds"] = min(
        300, max(1, int(crypto.get("binance_max_retry_after_seconds", 60)))
    )
    return config


class MarketDataService:
    """用一个后台线程串行化数据刷新，查询始终使用最后一份完整缓存。"""

    CONFIG_FILENAME = "market_data_config.json"
    MANIFEST_FILENAME = "market_data_manifest.json"
    SECTORS_FILENAME = "stock_sectors.json"
    RETRY_BACKOFF_SECONDS = (15 * 60, 30 * 60, 60 * 60, 120 * 60)
    ON_DEMAND_RETRY_COOLDOWN_SECONDS = 5 * 60
    TIMEFRAME_SECONDS = {
        "5m": 5 * 60,
        "15m": 15 * 60,
        "30m": 30 * 60,
        "60m": 60 * 60,
        "4h": 4 * 60 * 60,
        "1d": 24 * 60 * 60,
        "1w": 7 * 24 * 60 * 60,
    }

    def __init__(self, manager: CpuShapeSearchManager):
        self.manager = manager
        self.data_dir = manager.data_dir
        self.config_path = self.data_dir / self.CONFIG_FILENAME
        self.manifest_path = self.data_dir / self.MANIFEST_FILENAME
        self.sectors_path = self.data_dir / self.SECTORS_FILENAME
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._refresh_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._force_refresh = False
        self._retry_failures: Dict[str, int] = {}
        self._retry_after: Dict[str, float] = {}
        self._pending_categories: Dict[str, set[str]] = {}
        self._active_categories: Dict[str, set[str]] = {}
        self._last_on_demand_attempt: Dict[str, float] = {}
        self._config = self._load_json(self.config_path, DEFAULT_CONFIG)
        self._config = normalize_config(self._config)
        self._manifest = self._load_json(self.manifest_path, {})
        self._status: Dict[str, Any] = {
            "state": "created",
            "message": "尚未启动",
            "progress": 0.0,
            "current_timeframe": None,
            "last_refresh_started": None,
            "last_refresh_finished": None,
            "last_error": None,
            "reports": {},
            "logs": [],
            "crypto_capability": (
                "Binance Spot 公共 K 线（无需 API Key）；默认本地预算 1200 weight/min，"
                "监控 X-MBX-USED-WEIGHT-1M 并遵守 429/418 Retry-After"
            ),
        }
        self._apply_search_config(self._config)

    def _apply_search_config(self, config: Dict[str, Any]) -> None:
        if config["adaptive_scale"]:
            search_min = min(24, config["fetch_bars"])
            search_max = config["fetch_bars"]
        else:
            search_min = search_max = min(config["feature_window"], config["fetch_bars"])
        self.manager.set_config(CpuSearchConfig(min_length=search_min, max_length=search_max))

    @staticmethod
    def _load_json(path: Path, fallback: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            log.warning("读取 %s 失败: %s", path, exc)
        return copy.deepcopy(fallback)

    @staticmethod
    def _save_json(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def start(self) -> List[str]:
        """同步加载本地缓存，随后启动非阻塞后台刷新。"""
        cache_source = str(self._manifest.get("data_source", "")).upper()
        cache_verified = (
            cache_source == "AKSHARE+BINANCE_PUBLIC"
            and int(self._manifest.get("schema_version", 0)) >= 2
            and bool(self._manifest.get("cache_committed", False))
        )
        loaded = self.manager.load_all(require_exist=False) if cache_verified else []
        ignored_legacy = self.manager.store_path.exists() and not cache_verified
        with self._lock:
            self._status.update({
                "state": "ready" if loaded else "waiting_refresh",
                "message": (
                    f"已加载已验证市场缓存: {loaded}"
                    if loaded else
                    "检测到旧版未知来源缓存，已忽略并等待后台构建"
                    if ignored_legacy else
                    "本地无已验证缓存，等待后台构建"
                ),
                "progress": 1.0 if loaded else 0.0,
            })
            self._status["legacy_cache_ignored"] = ignored_legacy
        if os.environ.get("MARKET_DATA_AUTO_REFRESH", "1") != "0":
            self._thread = threading.Thread(target=self._worker_loop, name="market-data-refresh", daemon=True)
            self._thread.start()
            self.request_refresh(force=not bool(loaded))
        return loaded

    def stop(self) -> None:
        self._stop_event.set()
        self._refresh_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self.manager.save_all()

    def get_config(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._config)

    def update_config(self, patch: Dict[str, Any], refresh: bool = True) -> Dict[str, Any]:
        with self._lock:
            self._config = normalize_config(_deep_merge(self._config, patch))
            self._apply_search_config(self._config)
            self._save_json(self.config_path, self._config)
            result = copy.deepcopy(self._config)
        if refresh:
            self.request_refresh(force=True)
        return result

    def status(self) -> Dict[str, Any]:
        with self._lock:
            result = copy.deepcopy(self._status)
            result["config"] = copy.deepcopy(self._config)
            result["manifest"] = copy.deepcopy(self._manifest)
            now = time.time()
            result["retry_backoff"] = {
                key: {
                    "failures": int(self._retry_failures.get(key, 0)),
                    "retry_at": int(retry_at),
                    "remaining_seconds": max(0, int(retry_at - now)),
                }
                for key, retry_at in self._retry_after.items()
                if retry_at > now
            }
        result["buckets"] = self.manager.buckets_status()
        result["last_kline_ts"] = {
            bucket: self.manager.bucket_latest_timestamp(*bucket.rsplit("_", 1))
            for bucket in result["buckets"]
        }
        result["search_ready"] = any(value > 0 for value in result["buckets"].values())
        return result

    def ensure_timeframe(
        self,
        timeframe: str,
        categories: Optional[List[str]] = None,
        user_requested: bool = False,
    ) -> Dict[str, Any]:
        """确保缺失桶进入队列；用户按需请求可有限度越过旧失败退避。"""
        timeframe = normalize_timeframe(timeframe)
        if timeframe not in VALID_TIMEFRAMES:
            raise ValueError(f"不支持的周期: {timeframe}")
        added = False
        with self._lock:
            if timeframe not in self._config["timeframes"]:
                self._config["timeframes"].append(timeframe)
                self._config = normalize_config(self._config)
                self._save_json(self.config_path, self._config)
                added = True
            config = copy.deepcopy(self._config)
            now = time.time()
            enabled = {
                category for category in ("stock", "crypto")
                if config.get(category, {}).get("enabled", True)
            }
            requested = enabled if categories is None else {
                str(category) for category in categories if str(category) in enabled
            }
            active = set(self._active_categories.get(timeframe, set())) & requested
            already_pending = set(self._pending_categories.get(timeframe, set())) & requested
            backoff_remaining = [
                max(1, int(retry_at - now))
                for category in requested
                for retry_at in [self._retry_after.get(f"{category}_{timeframe}", 0.0)]
                if retry_at > now
            ]

            due_categories = self._due_categories(timeframe, config, force=False) & requested
            queue_categories = due_categories - active - already_pending
            if user_requested:
                # 缓存为空就是明确的用户需求；即便周期 token 尚未到期也需要补齐。
                for category in requested - active - already_pending:
                    key = f"{category}_{timeframe}"
                    retry_at = self._retry_after.get(key, 0.0)
                    last_attempt = self._last_on_demand_attempt.get(key, 0.0)
                    may_override = (
                        retry_at <= now
                        or now - last_attempt >= self.ON_DEMAND_RETRY_COOLDOWN_SECONDS
                    )
                    if may_override:
                        queue_categories.add(category)
                        self._last_on_demand_attempt[key] = now
            if added and not queue_categories and not active and not already_pending:
                queue_categories.update(requested)
            if queue_categories:
                self._pending_categories.setdefault(timeframe, set()).update(queue_categories)

            pending = already_pending | queue_categories
            running = self._status.get("state") == "refreshing"

        if active:
            state = "refreshing"
            message = f"{timeframe} data is downloading"
            retry_after_seconds = 15
        elif pending:
            state = "refresh_queued"
            message = f"{timeframe} missing market data queued for download"
            retry_after_seconds = 15
        elif backoff_remaining:
            state = "retry_backoff"
            retry_after_seconds = max(backoff_remaining)
            message = f"Market source unavailable; retry in about {retry_after_seconds}s"
        else:
            state = "waiting_period"
            retry_after_seconds = self.TIMEFRAME_SECONDS.get(timeframe, 300)
            message = f"No searchable {timeframe} cache; waiting for next market interval"

        should_wake = bool(queue_categories)
        if should_wake:
            with self._lock:
                if not running:
                    self._status["state"] = "refresh_queued"
                    self._status["message"] = message
            self._refresh_event.set()
            self._append_log(
                f"[ON_DEMAND][QUEUE] timeframe={timeframe}, added={added}, "
                f"categories={sorted(queue_categories)}"
            )
        return {
            "timeframe": timeframe,
            "added": added,
            "queued": bool(pending),
            "requested_categories": sorted(requested),
            "state": state,
            "message": message,
            "retry_after_seconds": retry_after_seconds,
        }

    def request_refresh(self, force: bool = True) -> bool:
        with self._lock:
            already_running = self._status.get("state") == "refreshing"
            self._force_refresh = self._force_refresh or force
            if not already_running:
                self._status["state"] = "refresh_queued"
                self._status["message"] = "刷新请求已入队"
        self._refresh_event.set()
        return not already_running

    def list_sectors(self, refresh: bool = False) -> List[str]:
        if not refresh:
            cached = self._load_json(self.sectors_path, {}).get("items", [])
            if cached:
                return [str(item) for item in cached]
        sectors = list_stock_sectors()
        self._save_json(self.sectors_path, {"updated_at": int(time.time()), "items": sectors})
        return sectors

    def _append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        log.info(message)
        with self._lock:
            entries = self._status.setdefault("logs", [])
            entries.append(line)
            del entries[:-120]
            self._status["message"] = message

    @staticmethod
    def _period_token(timeframe: str, category: str = "crypto") -> str:
        timeframe = normalize_timeframe(timeframe)
        now = datetime.now()
        minute_periods = {"5m": 5, "15m": 15, "30m": 30, "60m": 60}
        if timeframe in minute_periods:
            period = minute_periods[timeframe]
            if category != "stock":
                return str(int(now.timestamp()) // (period * 60))

            # A 股每天 240 个交易分钟。午休/收盘/周末固定 token，避免全天重复抓取。
            clock_minutes = now.hour * 60 + now.minute
            session_day = now
            first_close = 9 * 60 + 30 + period
            if now.weekday() >= 5 or clock_minutes < first_close:
                session_day = now - timedelta(days=1)
                while session_day.weekday() >= 5:
                    session_day -= timedelta(days=1)
                return f"{session_day:%Y%m%d}-{240 // period:03d}"
            if clock_minutes <= 11 * 60 + 30:
                elapsed = clock_minutes - (9 * 60 + 30)
            elif clock_minutes < 13 * 60:
                elapsed = 120
            elif clock_minutes <= 15 * 60:
                elapsed = 120 + clock_minutes - 13 * 60
            else:
                elapsed = 240
            return f"{session_day:%Y%m%d}-{max(0, elapsed // period):03d}"
        if timeframe == "4h":
            if category == "stock":
                effective = now if now.hour >= 15 else now - timedelta(days=1)
                while effective.weekday() >= 5:
                    effective -= timedelta(days=1)
                return effective.strftime("%Y%m%d")
            return f"{now:%Y%m%d}-{now.hour // 4}"
        if timeframe == "1w":
            iso = now.isocalendar()
            if now.weekday() < 4 or (now.weekday() == 4 and now.hour < 16):
                iso = (now - timedelta(days=7)).isocalendar()
            return f"{iso.year}-W{iso.week:02d}"
        effective = now if now.hour >= 16 else now - timedelta(days=1)
        return effective.strftime("%Y%m%d")

    def _is_due(self) -> bool:
        with self._lock:
            config = copy.deepcopy(self._config)
        return any(self._due_categories(tf, config, force=False) for tf in config["timeframes"])

    def _due_categories(self, timeframe: str, config: Dict[str, Any], force: bool) -> set[str]:
        """按品类分别记录周期 token，避免 AKShare 失败时反复重抓已成功的 Binance 数据。"""
        if force:
            return {
                category for category in ("stock", "crypto")
                if config.get(category, {}).get("enabled", True)
            }
        with self._lock:
            tokens = dict(self._manifest.get("category_period_tokens", {}))
            retry_after = dict(self._retry_after)
        now = time.time()
        return {
            category
            for category in ("stock", "crypto")
            if config.get(category, {}).get("enabled", True)
            and tokens.get(f"{category}_{timeframe}") != self._period_token(timeframe, category)
            and retry_after.get(f"{category}_{timeframe}", 0.0) <= now
        }

    def _clear_retry_backoff(self, key: str) -> None:
        with self._lock:
            self._retry_failures.pop(key, None)
            self._retry_after.pop(key, None)

    def _schedule_retry_backoff(self, key: str) -> Tuple[int, int, int]:
        """记录连续失败并返回（失败次数、等待秒数、下次重试时间戳）。"""
        with self._lock:
            failures = self._retry_failures.get(key, 0) + 1
            delay = self.RETRY_BACKOFF_SECONDS[
                min(failures - 1, len(self.RETRY_BACKOFF_SECONDS) - 1)
            ]
            retry_at = int(time.time() + delay)
            self._retry_failures[key] = failures
            self._retry_after[key] = float(retry_at)
        return failures, delay, retry_at

    @staticmethod
    def _compact_refresh_error(error: Any) -> str:
        """终端只保留可判断的错误类型，不输出超长代理 URL。"""
        text = str(error or "未知错误")
        if "ProxyError" in text:
            return "ProxyError: 无法通过当前系统代理连接 A 股数据源"
        if "RemoteDisconnected" in text:
            return "RemoteDisconnected: A 股数据源提前断开连接"
        if "ConnectTimeout" in text or "ReadTimeout" in text:
            return "Timeout: A 股数据源连接超时"
        return text if len(text) <= 240 else text[:237] + "..."

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                interval = int(self._config.get("refresh_check_seconds", 300))
            triggered = self._refresh_event.wait(timeout=interval)
            self._refresh_event.clear()
            if self._stop_event.is_set():
                break
            with self._lock:
                force = self._force_refresh
                self._force_refresh = False
            if triggered or force or self._is_due():
                self._refresh_once(force=force)

    def _refresh_once(self, force: bool) -> None:
        with self._lock:
            config = copy.deepcopy(self._config)
            self._status.update({
                "state": "refreshing",
                "message": "开始 AKShare + Binance Public 后台刷新",
                "progress": 0.0,
                "last_refresh_started": int(time.time()),
                "last_error": None,
            })
        self._append_log(f"[REFRESH][START] force={force}, timeframes={config['timeframes']}")

        self._apply_search_config(config)

        reports: Dict[str, Any] = {}
        successful_category_tokens: Dict[str, str] = {}
        checked_category_tokens: Dict[str, str] = {}
        timeframes = list(config["timeframes"])
        last_console_percent = -1
        try:
            for tf_index, timeframe in enumerate(timeframes):
                with self._lock:
                    pending_categories = set(self._pending_categories.pop(timeframe, set()))
                # 用户单选市场触发的按需任务只处理缺失品类；手动 force 刷新仍处理全部启用品类。
                due_categories = (
                    self._due_categories(timeframe, config, force=force)
                    if force or not pending_categories
                    else pending_categories
                )
                if not due_categories:
                    continue
                with self._lock:
                    self._status["current_timeframe"] = timeframe
                    self._active_categories[timeframe] = set(due_categories)
                self._append_log(
                    f"[REFRESH][DUE] {timeframe} categories={sorted(due_categories)}"
                )

                def progress(done: int, total: int, message: str) -> None:
                    nonlocal last_console_percent
                    within = done / max(total, 1)
                    overall = (tf_index + within) / max(len(timeframes), 1)
                    with self._lock:
                        self._status["progress"] = round(overall, 4)
                        self._status["message"] = message
                    percent = min(100, max(0, int(overall * 100)))
                    if (
                        last_console_percent < 0
                        or percent >= last_console_percent + 5
                        or percent == 100
                    ):
                        last_console_percent = percent
                        log.info("[DATA][PROGRESS] %.4f|%s", overall, message)

                build_config = copy.deepcopy(config)
                build_config["_refresh_categories"] = sorted(due_categories)
                if "stock" in due_categories:
                    fallback_symbols = self.manager.bucket_symbols("stock", timeframe)
                    if not fallback_symbols:
                        seen_symbols: set[str] = set()
                        fallback_symbols = []
                        for fallback_timeframe in VALID_TIMEFRAMES:
                            for symbol, name in self.manager.bucket_symbols(
                                "stock", fallback_timeframe
                            ):
                                if symbol not in seen_symbols:
                                    seen_symbols.add(symbol)
                                    fallback_symbols.append((symbol, name))
                    build_config["_fallback_stock_pool"] = [
                        {"symbol": symbol, "name": name}
                        for symbol, name in fallback_symbols
                    ]
                samples, report = build_market_dataset(
                    build_config, timeframe, self._append_log, progress
                )
                reports[timeframe] = report
                complete_categories = {
                    category
                    for category, details in report.get("categories", {}).items()
                    if bool(details.get("complete", False))
                    and (int(details.get("built", 0)) > 0 or bool(details.get("clear", False)))
                }
                replace_categories: set[str] = set()
                freshness: Dict[str, Dict[str, int]] = {}
                for category in complete_categories:
                    self._clear_retry_backoff(f"{category}_{timeframe}")
                    details = report.get("categories", {}).get(category, {})
                    replacement_items = [item for item in samples if item.category == category]
                    old_by_symbol = self.manager.bucket_latest_timestamps(category, timeframe)
                    new_by_symbol: Dict[str, int] = {}
                    for item in replacement_items:
                        symbol = str(getattr(item, "symbol", ""))
                        new_by_symbol[symbol] = max(
                            new_by_symbol.get(symbol, 0), int(getattr(item, "end_ts", 0))
                        )
                    advanced = [
                        symbol for symbol, latest in new_by_symbol.items()
                        if latest > old_by_symbol.get(symbol, 0)
                    ]
                    old_latest = max(old_by_symbol.values(), default=0)
                    new_latest = max(new_by_symbol.values(), default=0)
                    clear = bool(details.get("clear", False))
                    should_replace = clear or force or not old_by_symbol or bool(advanced)
                    freshness[category] = {
                        "old": old_latest,
                        "new": new_latest,
                        "advanced_symbols": len(advanced),
                    }
                    if should_replace:
                        replace_categories.add(category)
                    # 完整抓取已证明当前周期没有更新时，也要推进检查 token，避免同一周期反复抓取。
                    if clear or int(details.get("built", 0)) > 0:
                        checked_category_tokens[f"{category}_{timeframe}"] = self._period_token(
                            timeframe, category
                        )
                if replace_categories:
                    replacement = [item for item in samples if item.category in replace_categories]
                    self.manager.replace_from_samples(replacement, timeframe, replace_categories)
                    for category in replace_categories:
                        successful_category_tokens[f"{category}_{timeframe}"] = self._period_token(
                            timeframe, category
                        )
                    self._append_log(
                        f"[REFRESH][COMMIT] {timeframe} categories={sorted(replace_categories)}, "
                        f"series={len(replacement)}, latest={freshness}"
                    )
                kept_categories = complete_categories - replace_categories
                if kept_categories:
                    self._append_log(
                        f"[REFRESH][NO_NEW_KLINE] {timeframe} categories={sorted(kept_categories)}, "
                        f"latest={freshness}，保留旧缓存"
                    )
                incomplete = due_categories - complete_categories
                if incomplete:
                    failures: Dict[str, Dict[str, Any]] = {}
                    retry_summary: Dict[str, Dict[str, int]] = {}
                    for category in sorted(incomplete):
                        details = report.get("categories", {}).get(category, {})
                        key = f"{category}_{timeframe}"
                        attempts, delay, retry_at = self._schedule_retry_backoff(key)
                        failures[category] = {
                            "source": details.get("source", "unknown"),
                            "selected": int(details.get("selected", 0)),
                            "built": int(details.get("built", 0)),
                            "skipped": int(details.get("skipped", 0)),
                            "error": self._compact_refresh_error(
                                details.get("error") or details.get("pool_error")
                            ),
                        }
                        retry_summary[category] = {
                            "failures": attempts,
                            "delay_seconds": delay,
                            "retry_at": retry_at,
                        }
                    self._append_log(
                        f"[REFRESH][INCOMPLETE] {timeframe} categories={sorted(incomplete)}, "
                        f"details={failures}, retry={retry_summary}"
                    )
                with self._lock:
                    self._active_categories.pop(timeframe, None)

            final_message = (
                "数据刷新完成" if successful_category_tokens else
                "已检查最新 K 线，缓存无需替换" if checked_category_tokens else
                "行情抓取未完整，保留旧缓存并稍后重试"
            )
            with self._lock:
                category_tokens = dict(self._manifest.get("category_period_tokens", {}))
                category_tokens.update(checked_category_tokens)
                period_tokens = dict(self._manifest.get("period_tokens", {}))
                for timeframe in timeframes:
                    active = [
                        category for category in ("stock", "crypto")
                        if config.get(category, {}).get("enabled", True)
                    ]
                    current = {
                        category: self._period_token(timeframe, category) for category in active
                    }
                    if all(
                        category_tokens.get(f"{category}_{timeframe}") == current[category]
                        for category in active
                    ):
                        period_tokens[timeframe] = "|".join(
                            f"{category}:{current[category]}" for category in active
                        )
                self._manifest = {
                    "schema_version": 2,
                    "data_source": "AKSHARE+BINANCE_PUBLIC",
                    "cache_committed": bool(
                        self._manifest.get("cache_committed", False)
                        or successful_category_tokens
                    ),
                    "updated_at": int(time.time()),
                    "period_tokens": period_tokens,
                    "category_period_tokens": category_tokens,
                    "config": copy.deepcopy(config),
                    "reports": copy.deepcopy(reports),
                }
                self._save_json(self.manifest_path, self._manifest)
                self._status.update({
                    "state": "ready" if any(self.manager.buckets_status().values()) else "degraded",
                    "message": final_message,
                    "progress": 1.0,
                    "current_timeframe": None,
                    "last_refresh_finished": int(time.time()),
                    "reports": reports,
                })
            self._append_log(
                f"[REFRESH][DONE] committed={sorted(successful_category_tokens)}, "
                f"checked={sorted(checked_category_tokens)}"
            )
            # _append_log 会同步终端最后一条信息；状态页仍应保留面向用户的最终结论。
            with self._lock:
                self._status["message"] = final_message
            if last_console_percent < 100:
                log.info("[DATA][PROGRESS] 1.0000|数据刷新完成")
        except Exception as exc:
            self._append_log(f"[REFRESH][ERROR] {type(exc).__name__}: {exc}")
            with self._lock:
                self._status.update({
                    "state": "degraded" if any(self.manager.buckets_status().values()) else "error",
                    "last_error": f"{type(exc).__name__}: {exc}",
                    "last_refresh_finished": int(time.time()),
                    "current_timeframe": None,
                })
        finally:
            with self._lock:
                self._active_categories.clear()


__all__ = [
    "DEFAULT_CONFIG",
    "VALID_TIMEFRAMES",
    "TIMEFRAME_ALIASES",
    "VALID_STOCK_METRICS",
    "VALID_CRYPTO_METRICS",
    "VALID_ORDERS",
    "normalize_config",
    "normalize_timeframe",
    "MarketDataService",
]

# -*- coding: utf-8 -*-
"""
统一预设策略、手绘策略和集合筛选服务。

创建时间：2026-09-15
作用：复用 Sequoia-X 的六个日 K 量价策略条件，并把当前项目的最新窗口形态搜索保存为可复用策略；
      多策略先独立求命中集合，再显式执行 intersection 或 union。
使用方式：StrategyService(manager, data_dir).screen(...)；手绘策略通过 save_sketch/delete_sketch 管理。

来源：sngyai/Sequoia-X commit 444c0db69ff36b46ef2b22ab265051d60c16029d（上游 README 声明 MIT）。
适配原则：只读取当前项目已提交的 OHLCV/turnover 内存快照；缺字段时报告 unavailable，不估算、不放宽条件。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
from scipy.stats import rankdata

from shape_search.models import MarketSeries
from shape_search.preprocessing import preprocess_sketch


UPSTREAM_URL = "https://github.com/sngyai/Sequoia-X"
UPSTREAM_COMMIT = "444c0db69ff36b46ef2b22ab265051d60c16029d"

PRESET_CATALOG: Tuple[Dict[str, Any], ...] = (
    {
        "id": "sequoia_turtle_trade",
        "name": "海龟突破",
        "short_name": "TURTLE",
        "description": "收盘突破前20日最高价，成交额过亿，且当日阳线真涨",
        "kind": "preset",
        "category_support": ["stock"],
        "timeframe_support": ["1d"],
        "minimum_bars": 21,
    },
    {
        "id": "sequoia_ma_volume",
        "name": "均线放量突破",
        "short_name": "MA+VOL",
        "description": "MA5 上穿 MA20，且当日成交量大于20日均量1.5倍",
        "kind": "preset",
        "category_support": ["stock"],
        "timeframe_support": ["1d"],
        "minimum_bars": 21,
    },
    {
        "id": "sequoia_high_tight_flag",
        "name": "高窄旗形",
        "short_name": "HTF",
        "description": "40日涨幅强、近10日高位收敛且缩量",
        "kind": "preset",
        "category_support": ["stock"],
        "timeframe_support": ["1d"],
        "minimum_bars": 40,
    },
    {
        "id": "sequoia_limit_up_shakeout",
        "name": "涨停洗盘",
        "short_name": "SHAKEOUT",
        "description": "昨日涨停，今日放量收阴且最低价不破昨收",
        "kind": "preset",
        "category_support": ["stock"],
        "timeframe_support": ["1d"],
        "minimum_bars": 3,
    },
    {
        "id": "sequoia_uptrend_limit_down",
        "name": "上升趋势跌停",
        "short_name": "UP-LIMITDN",
        "description": "MA20 高于 MA60 的上升趋势中出现放量跌停",
        "kind": "preset",
        "category_support": ["stock"],
        "timeframe_support": ["1d"],
        "minimum_bars": 61,
    },
    {
        "id": "sequoia_rps_breakout",
        "name": "RPS 强势突破",
        "short_name": "RPS90",
        "description": "120日涨幅位于全市场前10%，且收盘接近120日最高价",
        "kind": "preset",
        "category_support": ["stock"],
        "timeframe_support": ["1d"],
        "minimum_bars": 121,
    },
)


def _finite_tail(values: Optional[np.ndarray], count: int) -> Optional[np.ndarray]:
    if values is None or len(values) < count:
        return None
    tail = np.asarray(values[-count:], dtype=np.float64)
    return tail if np.isfinite(tail).all() else None


def _ohlc(market: MarketSeries, count: int) -> Optional[Tuple[np.ndarray, ...]]:
    if market.ohlc_values is None or len(market.ohlc_values) < count:
        return None
    values = np.asarray(market.ohlc_values[-count:], dtype=np.float64)
    if values.shape != (count, 4) or not np.isfinite(values).all():
        return None
    return values[:, 0], values[:, 1], values[:, 2], values[:, 3]


class StrategyService:
    """在同一份行情快照上执行规则策略与保存的最新形态策略。"""

    STORE_FILENAME = "saved_shape_strategies.json"
    MAX_SAVED_SKETCHES = 100

    def __init__(self, manager: Any, data_dir: str | Path):
        self.manager = manager
        self.path = Path(data_dir) / self.STORE_FILENAME
        self._lock = threading.RLock()
        self._saved = self._load()
        self._shape_cache: Dict[Tuple[Any, ...], Dict[str, float]] = {}

    def _load(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            items = payload.get("items", [])
            return [dict(item) for item in items if isinstance(item, dict)]
        except (OSError, ValueError, TypeError):
            return []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"schema_version": 1, "items": self._saved}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def catalog(self, compact: bool = False) -> Dict[str, Any]:
        with self._lock:
            saved = [dict(item) for item in self._saved]
        if compact:
            saved = [
                {key: value for key, value in item.items() if key != "points"}
                for item in saved
            ]
        return {
            "presets": [dict(item) for item in PRESET_CATALOG],
            "saved_sketches": saved,
            "combine_modes": ["intersection", "union"],
            "upstream": {"url": UPSTREAM_URL, "commit": UPSTREAM_COMMIT},
        }

    def save_sketch(
        self,
        name: str,
        points: Iterable,
        threshold: float = 0.65,
        max_results: int = 60,
    ) -> Dict[str, Any]:
        query = preprocess_sketch(points, self.manager.config, y_flip=False)
        normalized_points = np.column_stack([
            np.linspace(0.0, 1.0, len(query.raw), dtype=np.float64),
            query.raw,
        ])
        value_range = float(np.ptp(normalized_points[:, 1]))
        if value_range > 1e-12:
            normalized_points[:, 1] = (
                normalized_points[:, 1] - normalized_points[:, 1].min()
            ) / value_range
        else:
            normalized_points[:, 1] = 0.5
        with self._lock:
            if len(self._saved) >= self.MAX_SAVED_SKETCHES:
                raise ValueError(f"最多保存 {self.MAX_SAVED_SKETCHES} 个手绘策略")
            item = {
                "id": f"sketch_{uuid.uuid4().hex[:12]}",
                "name": str(name).strip()[:48] or f"手绘策略 {len(self._saved) + 1}",
                "short_name": f"SKETCH {len(self._saved) + 1}",
                "description": "保存的手绘形态，仅匹配每个标的最新结束窗口",
                "kind": "sketch",
                "category_support": ["stock", "crypto"],
                "timeframe_support": ["5m", "15m", "30m", "60m", "4h", "1d", "1w"],
                "threshold": round(float(np.clip(threshold, 0.0, 1.0)), 4),
                "max_results": max(1, min(int(max_results), 60)),
                "created_at": int(time.time()),
                "points": normalized_points.round(6).tolist(),
            }
            self._saved.append(item)
            self._save()
            self._shape_cache.clear()
            return dict(item)

    def delete_sketch(self, strategy_id: str) -> bool:
        with self._lock:
            before = len(self._saved)
            self._saved = [item for item in self._saved if item.get("id") != strategy_id]
            if len(self._saved) == before:
                return False
            self._save()
            self._shape_cache.clear()
            return True

    @staticmethod
    def _evaluate_preset(strategy_id: str, market: MarketSeries) -> Tuple[bool, Optional[str]]:
        if market.category != "stock" or market.timeframe != "1d":
            return False, "unsupported_market"

        if strategy_id == "sequoia_turtle_trade":
            data = _ohlc(market, 21)
            turnover = _finite_tail(market.turnover_values, 1)
            if data is None or turnover is None:
                return False, "missing_ohlc_or_turnover"
            open_, high, _low, close = data
            return bool(
                close[-1] > np.max(high[-21:-1])
                and turnover[-1] > 100_000_000
                and close[-1] > open_[-1]
                and close[-1] > close[-2]
            ), None

        if strategy_id == "sequoia_ma_volume":
            data = _ohlc(market, 21)
            volume = _finite_tail(market.volume_values, 21)
            if data is None or volume is None:
                return False, "missing_ohlcv"
            close = data[3]
            golden_cross = np.mean(close[-6:-1]) < np.mean(close[-21:-1]) and (
                np.mean(close[-5:]) > np.mean(close[-20:])
            )
            return bool(golden_cross and volume[-1] > np.mean(volume[-20:]) * 1.5), None

        if strategy_id == "sequoia_high_tight_flag":
            data = _ohlc(market, 40)
            volume = _finite_tail(market.volume_values, 21)
            if data is None or volume is None:
                return False, "missing_ohlcv"
            _open, high, low, _close = data
            high40, low40 = float(np.max(high)), float(np.min(low))
            high10, low10 = float(np.max(high[-10:])), float(np.min(low[-10:]))
            if low40 <= 0 or low10 <= 0:
                return False, "invalid_price"
            return bool(
                high40 / low40 > 1.6
                and high10 / low10 < 1.15
                and low10 >= high40 * 0.8
                and volume[-1] < np.mean(volume[-21:-1]) * 0.6
            ), None

        if strategy_id == "sequoia_limit_up_shakeout":
            data = _ohlc(market, 3)
            volume = _finite_tail(market.volume_values, 3)
            if data is None or volume is None:
                return False, "missing_ohlcv"
            open_, _high, low, close = data
            return bool(
                close[-2] >= close[-3] * 1.095
                and close[-1] < open_[-1]
                and volume[-1] > volume[-2] * 2.0
                and low[-1] >= close[-2]
            ), None

        if strategy_id == "sequoia_uptrend_limit_down":
            data = _ohlc(market, 61)
            volume = _finite_tail(market.volume_values, 20)
            if data is None or volume is None:
                return False, "missing_ohlcv"
            close = data[3]
            return bool(
                np.mean(close[-21:-1]) > np.mean(close[-61:-1])
                and close[-1] <= close[-2] * 0.905
                and volume[-1] > np.mean(volume) * 2.0
            ), None

        raise KeyError(strategy_id)

    @staticmethod
    def _evaluate_rps(markets: Iterable[MarketSeries]) -> Tuple[Dict[str, float], int]:
        usable: List[Tuple[MarketSeries, float, float]] = []
        unavailable = 0
        for market in markets:
            if market.category != "stock" or market.timeframe != "1d":
                continue
            data = _ohlc(market, 121)
            if data is None:
                unavailable += 1
                continue
            high, close = data[1], data[3]
            performance = float(close[-1] / close[-121] - 1.0)
            usable.append((market, performance, float(np.max(high[-120:]))))
        if not usable:
            return {}, unavailable
        ranks = rankdata([item[1] for item in usable], method="average") / len(usable) * 100.0
        matches = {
            market.symbol: float(rank / 100.0)
            for (market, _performance, rolling_high), rank in zip(usable, ranks)
            if rank >= 90.0 and float(market.source_values[-1]) >= rolling_high * 0.90
        }
        return matches, unavailable

    def _evaluate_sketch(
        self,
        item: Dict[str, Any],
        category: str,
        timeframe: str,
        markets: Tuple[MarketSeries, ...],
    ) -> Dict[str, float]:
        signature = (
            len(markets),
            sum(int(market.timestamps[-1]) for market in markets if len(market.timestamps)),
            max((int(market.timestamps[-1]) for market in markets if len(market.timestamps)), default=0),
        )
        key = (item["id"], category, timeframe, signature, item["threshold"], item["max_results"])
        with self._lock:
            cached = self._shape_cache.get(key)
            if cached is not None:
                return dict(cached)
        results, _total = self.manager.search(
            category=category,
            timeframe=timeframe,
            top_k=int(item["max_results"]),
            query_points=item["points"],
            latest_only=True,
        )
        matches = {
            result["symbol"]: float(result["match_score"])
            for result in results
            if float(result["match_score"]) >= float(item["threshold"])
        }
        with self._lock:
            self._shape_cache[key] = dict(matches)
        return matches

    def screen(
        self,
        strategy_ids: Iterable[str],
        combine: str,
        category: str,
        timeframe: str,
        limit: int = 100,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        ids, catalog = self.validate_request(strategy_ids, combine, category, timeframe)

        markets = self.manager.market_snapshot(category=category, timeframe=timeframe)
        supports = [set(catalog[item]["category_support"]) for item in ids]
        eligible_categories = (
            set.intersection(*supports) if combine == "intersection" else set.union(*supports)
        )
        if category != "all":
            eligible_categories &= {category}
        markets = tuple(
            market for market in markets if market.category in eligible_categories
        )
        if not markets:
            raise ValueError(f"{category}_{timeframe} 行情桶为空")

        search_category = (
            next(iter(eligible_categories)) if len(eligible_categories) == 1 else category
        )

        matches_by_strategy: Dict[str, Dict[str, float]] = {}
        unavailable: Dict[str, int] = {}
        for strategy_id in ids:
            item = catalog[strategy_id]
            if item["kind"] == "sketch":
                matches_by_strategy[strategy_id] = self._evaluate_sketch(
                    item, search_category, timeframe, markets
                )
                continue
            if strategy_id == "sequoia_rps_breakout":
                matches, missing_count = self._evaluate_rps(markets)
                matches_by_strategy[strategy_id] = matches
                unavailable[strategy_id] = missing_count
                continue
            matched: Dict[str, float] = {}
            missing_count = 0
            for market in markets:
                is_match, reason = self._evaluate_preset(strategy_id, market)
                if reason and reason != "unsupported_market":
                    missing_count += 1
                if is_match:
                    matched[market.symbol] = 1.0
            matches_by_strategy[strategy_id] = matched
            unavailable[strategy_id] = missing_count

        sets = [set(matches_by_strategy[item]) for item in ids]
        selected = set.intersection(*sets) if combine == "intersection" else set.union(*sets)
        market_by_symbol = {market.symbol: market for market in markets}
        rows: List[Dict[str, Any]] = []
        for symbol in selected:
            market = market_by_symbol.get(symbol)
            if market is None:
                continue
            matched_ids = [item for item in ids if symbol in matches_by_strategy[item]]
            scores = [matches_by_strategy[item][symbol] for item in matched_ids]
            close = np.asarray(market.source_values, dtype=np.float64)
            change_pct = float((close[-1] / close[-2] - 1.0) * 100.0) if len(close) > 1 else 0.0
            rows.append({
                "symbol": symbol,
                "name": market.symbol_name,
                "category": market.category,
                "timeframe": market.timeframe,
                "current_price": round(float(close[-1]), 8),
                "change_pct": round(change_pct, 4),
                "matched_strategy_ids": matched_ids,
                "matched_strategy_names": [catalog[item]["name"] for item in matched_ids],
                "coverage": len(matched_ids),
                "combined_score": round(float(np.mean(scores)), 6),
            })
        rows.sort(
            key=lambda item: (item["coverage"], item["combined_score"], item["change_pct"]),
            reverse=True,
        )
        total = len(rows)
        rows = rows[:max(1, min(int(limit), 200))]
        return {
            "total": total,
            "evaluated_series": len(markets),
            "combine": combine,
            "strategy_ids": ids,
            "unavailable_series": unavailable,
            "query_ms": int((time.perf_counter() - started) * 1000),
            "list": rows,
        }

    def validate_request(
        self,
        strategy_ids: Iterable[str],
        combine: str,
        category: str,
        timeframe: str,
    ) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        ids = list(dict.fromkeys(str(item) for item in strategy_ids if str(item)))
        if not ids:
            raise ValueError("至少选择一个策略")
        if combine not in {"intersection", "union"}:
            raise ValueError("combine 仅支持 intersection 或 union")
        if category not in {"all", "stock", "crypto"}:
            raise ValueError("category 仅支持 all/stock/crypto")

        catalog = {item["id"]: dict(item) for item in PRESET_CATALOG}
        with self._lock:
            catalog.update({item["id"]: dict(item) for item in self._saved})
        unknown = [item for item in ids if item not in catalog]
        if unknown:
            raise ValueError(f"未知策略: {unknown}")
        for strategy_id in ids:
            item = catalog[strategy_id]
            if timeframe not in item["timeframe_support"]:
                raise ValueError(f"策略 {item['name']} 不支持周期 {timeframe}")
            if category != "all" and category not in item["category_support"]:
                raise ValueError(f"策略 {item['name']} 不支持市场 {category}")
        return ids, catalog

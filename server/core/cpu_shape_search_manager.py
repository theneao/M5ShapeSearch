# -*- coding: utf-8 -*-
"""
CPU 连续序列形态搜索管理器。
========================================
创建时间：2026-08-28
作用：以兼容原 build_from_samples/search 接口的方式接入 Multi-scale NCC +
      Derivative NCC + Turning Point + Subsequence ShapeDTW，不再依赖 FAISS 作为主搜索方案。
使用方式：CpuShapeSearchManager(data_dir, config).build_from_samples(...);
          search(..., query_points=手绘点) 返回兼容 UI/API 的结果字典。

修改时间：2026-08-28
修改作用：连续数据存储增加可选 OHLC，并提供硬件详情页使用的 K 线分段查询。

修改时间：2026-08-28
修改作用：增加周线桶、读写锁与显式桶替换，支持后台增量刷新时继续接受手绘查询。
使用方式：数据服务调用 replace_from_samples(..., categories=...)；查询与保存由类内 RLock 保护。

修改时间：2026-08-28
修改作用：所有品类被显式清空时同步移除生成的 NPZ，避免下一次启动复活旧缓存。

修改时间：2026-08-29
修改作用：缓存桶增加 5m/15m/30m/60m，加载历史 NPZ 时把旧 1h 桶无损迁移为 60m。
"""
from __future__ import annotations
# 修改时间：2026-08-29
# 修改作用：活动市场只保留 A 股和虚拟货币；加载旧缓存时自动丢弃期货序列。
# 使用方式：服务启动时自动迁移旧 NPZ，无需手工清理数据目录。
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from shape_search import CpuSearchConfig, CpuShapeSearchEngine
from shape_search.models import MarketSeries
from shape_search.preprocessing import build_market_series

from .mock_shape_generator import ShapeSample


ACTIVE_CATEGORIES = ("crypto", "stock")

BUCKETS = [
    f"{category}_{timeframe}"
    for category in ACTIVE_CATEGORIES
    for timeframe in ("5m", "15m", "30m", "60m", "4h", "1d", "1w")
]


class CpuShapeSearchManager:
    """连续市场序列存储、持久化和 CPU 形态检索兼容层。"""

    STORE_FILENAME = "cpu_market_sequences.npz"

    def __init__(
        self,
        data_dir: str = "./data/cpu_shape_search",
        config: Optional[CpuSearchConfig] = None,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config = (config or CpuSearchConfig()).validate()
        self.engine = CpuShapeSearchEngine(self.config)
        self.market_series: List[MarketSeries] = []
        self._buckets_size: Dict[str, int] = {bucket: 0 for bucket in BUCKETS}
        self.last_diagnostics: Dict[str, float] = {}
        self._lock = threading.RLock()

    @property
    def store_path(self) -> Path:
        return self.data_dir / self.STORE_FILENAME

    def _refresh_status(self) -> None:
        self._buckets_size = {bucket: 0 for bucket in BUCKETS}
        for market in self.market_series:
            bucket = f"{market.category}_{market.timeframe}"
            self._buckets_size[bucket] = self._buckets_size.get(bucket, 0) + 1

    def set_config(self, config: CpuSearchConfig) -> None:
        """在后台刷新前原子替换查询尺度配置。"""
        validated = config.validate()
        with self._lock:
            self.config = validated
            self.engine = CpuShapeSearchEngine(validated)

    def _sample_to_market(
        self,
        sample: ShapeSample,
        timeframe: str,
        series_suffix: str,
    ) -> Optional[MarketSeries]:
        close_series = getattr(sample, "close_series", None)
        market_timestamps = getattr(sample, "market_timestamps", None)
        ohlc_series = getattr(sample, "ohlc_series", None)
        values_are_prices = bool(close_series)
        if close_series:
            values = np.asarray(close_series, dtype=np.float64)
        else:
            raw = np.asarray(sample.raw_points, dtype=np.float64)
            if raw.ndim != 2 or raw.shape[0] < 3 or raw.shape[1] < 2:
                return None
            values = raw[:, 1]
        if market_timestamps and len(market_timestamps) == len(values):
            timestamps = np.asarray(market_timestamps, dtype=np.int64)
        else:
            timestamps = np.linspace(
                int(sample.start_ts),
                int(sample.end_ts),
                len(values),
            ).astype(np.int64)
        try:
            return build_market_series(
                series_id=f"{sample.category}_{timeframe}|{sample.symbol}|{series_suffix}",
                symbol=sample.symbol,
                symbol_name=sample.symbol_name,
                category=sample.category,
                timeframe=timeframe,
                timestamps=timestamps,
                values=values,
                values_are_prices=values_are_prices,
                sample_id=sample.sample_id,
                close_price=sample.close_price,
                ohlc_values=np.asarray(ohlc_series, dtype=np.float64) if ohlc_series else None,
                metadata={
                    "shape_type": sample.shape_type,
                    "change_pct": float(sample.change_pct),
                    "values_are_prices": values_are_prices,
                },
                config=self.config,
            )
        except (ValueError, TypeError, FloatingPointError):
            return None

    def build_from_samples(self, samples: List[ShapeSample], timeframe: str = "1d") -> int:
        """
        保存连续序列而非预切全部窗口。

        真实数据的同一标的若存在多个最新尺度，只保留 K 线最多的一条；MOCK/旧样本
        没有 close_series 时保留为独立 benchmark 序列，避免丢失不同形态。
        """
        return self.replace_from_samples(
            samples,
            timeframe=timeframe,
            categories={sample.category for sample in samples},
        )

    def replace_from_samples(
        self,
        samples: List[ShapeSample],
        timeframe: str,
        categories: set[str],
    ) -> int:
        """原子替换指定品类×周期数据；即使 samples 为空也会清理旧桶。"""
        categories = {str(item) for item in categories}

        real_longest: Dict[Tuple[str, str], ShapeSample] = {}
        standalone: List[ShapeSample] = []
        for sample in samples:
            close_series = getattr(sample, "close_series", None)
            if close_series:
                key = (sample.category, sample.symbol)
                previous = real_longest.get(key)
                if previous is None or len(close_series) > len(getattr(previous, "close_series", []) or []):
                    real_longest[key] = sample
            else:
                standalone.append(sample)

        added: List[MarketSeries] = []
        for (_category, _symbol), sample in real_longest.items():
            market = self._sample_to_market(sample, timeframe, "continuous")
            if market is not None:
                added.append(market)
        for index, sample in enumerate(standalone):
            market = self._sample_to_market(sample, timeframe, f"sample-{sample.sample_id}-{index}")
            if market is not None:
                added.append(market)

        with self._lock:
            self.market_series = [
                market for market in self.market_series
                if not (market.timeframe == timeframe and market.category in categories)
            ]
            self.market_series.extend(added)
            self._refresh_status()
            self.save_all()
        return len(added)

    def save_all(self) -> None:
        """把可变长连续序列压平保存为一个不依赖 pickle 的压缩 NPZ。"""
        with self._lock:
            if not self.market_series:
                self.store_path.unlink(missing_ok=True)
                return
            metadata = []
            offsets = [0]
            all_values = []
            all_timestamps = []
            all_ohlc = []
            for market in self.market_series:
                values = np.asarray(market.source_values, dtype=np.float64)
                timestamps = np.asarray(market.timestamps, dtype=np.int64)
                all_values.append(values)
                all_timestamps.append(timestamps)
                if market.ohlc_values is not None and market.ohlc_values.shape == (len(values), 4):
                    all_ohlc.append(np.asarray(market.ohlc_values, dtype=np.float64))
                else:
                    all_ohlc.append(np.full((len(values), 4), np.nan, dtype=np.float64))
                offsets.append(offsets[-1] + len(values))
                metadata.append({
                    "series_id": market.series_id,
                    "symbol": market.symbol,
                    "symbol_name": market.symbol_name,
                    "category": market.category,
                    "timeframe": market.timeframe,
                    "sample_id": market.sample_id,
                    "close_price": market.close_price,
                    "metadata": market.metadata,
                })
            np.savez_compressed(
                self.store_path,
                metadata_json=np.asarray(json.dumps(metadata, ensure_ascii=False)),
                offsets=np.asarray(offsets, dtype=np.int64),
                values=np.concatenate(all_values).astype(np.float64),
                timestamps=np.concatenate(all_timestamps).astype(np.int64),
                ohlc=np.concatenate(all_ohlc).astype(np.float64),
            )

    def load_all(self, require_exist: bool = False) -> List[str]:
        """从连续序列 NPZ 恢复市场数据；旧 FAISS 文件不会进入新搜索链路。"""
        if not self.store_path.exists():
            if require_exist:
                raise FileNotFoundError(f"缺失 CPU 连续序列文件: {self.store_path}")
            return []
        with np.load(self.store_path, allow_pickle=False) as payload:
            metadata = json.loads(str(payload["metadata_json"].item()))
            offsets = np.asarray(payload["offsets"], dtype=np.int64)
            values = np.asarray(payload["values"], dtype=np.float64)
            timestamps = np.asarray(payload["timestamps"], dtype=np.int64)
            stored_ohlc = (
                np.asarray(payload["ohlc"], dtype=np.float64)
                if "ohlc" in payload.files else None
            )

        loaded: List[MarketSeries] = []
        ignored_inactive = False
        for index, item in enumerate(metadata):
            start = int(offsets[index])
            end = int(offsets[index + 1])
            if str(item.get("category", "")) not in ACTIVE_CATEGORIES:
                ignored_inactive = True
                continue
            timeframe = "60m" if item["timeframe"] == "1h" else item["timeframe"]
            market = build_market_series(
                series_id=item["series_id"],
                symbol=item["symbol"],
                symbol_name=item.get("symbol_name", ""),
                category=item["category"],
                timeframe=timeframe,
                timestamps=timestamps[start:end],
                values=values[start:end],
                values_are_prices=bool(item.get("metadata", {}).get("values_are_prices", False)),
                sample_id=int(item.get("sample_id", 0)),
                close_price=float(item.get("close_price", 0.0)),
                ohlc_values=(
                    stored_ohlc[start:end]
                    if stored_ohlc is not None
                    and np.isfinite(stored_ohlc[start:end]).all()
                    else None
                ),
                metadata=item.get("metadata", {}),
                config=self.config,
            )
            loaded.append(market)
        with self._lock:
            self.market_series = loaded
            self._refresh_status()
            active_buckets = [bucket for bucket, count in self._buckets_size.items() if count > 0]
        if ignored_inactive:
            self.save_all()
        return active_buckets

    @staticmethod
    def _normalized_points(values: np.ndarray) -> List[List[float]]:
        y = np.asarray(values, dtype=np.float64).reshape(-1)
        if len(y) == 0:
            return []
        value_range = float(np.ptp(y))
        normalized = np.full_like(y, 0.5) if value_range < 1e-12 else (y - y.min()) / value_range
        x = np.linspace(0.0, 1.0, len(y))
        return [[round(float(xv), 6), round(float(yv), 6)] for xv, yv in zip(x, normalized)]

    def search(
        self,
        query_vector: Optional[np.ndarray] = None,
        category: str = "all",
        timeframe: str = "1d",
        top_k: int = 10,
        query_stats: Optional[Any] = None,
        query_seq_128: Optional[np.ndarray] = None,
        use_hard_filter: bool = False,
        query_points: Optional[List] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """执行 CPU 多阶段搜索；旧向量/统计参数仅为调用兼容保留。"""
        del query_vector, query_stats, use_hard_filter
        if query_points is None:
            seq = np.asarray(query_seq_128, dtype=np.float64)
            if seq.ndim != 2 or seq.shape[0] < 3 or seq.shape[1] < 2:
                return [], 0
            query_points = seq[:, :2].tolist()

        with self._lock:
            candidates, diagnostics = self.engine.search(
                query_points,
                self.market_series,
                category=category,
                timeframe=timeframe,
                top_k=top_k,
                y_flip=False,
                log=lambda message: print(message, flush=True),
            )
            self.last_diagnostics = diagnostics
            market_by_id = {market.series_id: market for market in self.market_series}
        results: List[Dict[str, Any]] = []
        for rank, candidate in enumerate(candidates, start=1):
            market = market_by_id[candidate.series_id]
            start = max(0, int(candidate.refined_start or candidate.coarse_start))
            end = min(len(market.source_values), int(candidate.refined_end or candidate.coarse_end))
            if end - start < 2:
                continue
            segment = market.source_values[start:end]
            if bool(market.metadata.get("values_are_prices", False)):
                change_pct = float((segment[-1] / max(segment[0], 1e-12) - 1.0) * 100.0)
            else:
                change_pct = float(market.metadata.get("change_pct", 0.0))
            score_details = {
                "price_ncc": float(candidate.price_ncc),
                "derivative_ncc": float(candidate.derivative_ncc),
                "turning_score": float(candidate.turning_score),
                "stage1_score": float(candidate.stage1_score),
                "shapedtw_distance": float(candidate.shapedtw_distance or 0.0),
                "shapedtw_score": float(candidate.shapedtw_score or 0.0),
                "supporting_scales": float(candidate.supporting_scales),
            }
            results.append({
                "sample_id": int(market.sample_id),
                "category": market.category,
                "symbol": market.symbol,
                "symbol_name": market.symbol_name,
                "shape_type": "feature_match",
                "raw_points": self._normalized_points(segment),
                "start_ts": int(market.timestamps[start]),
                "end_ts": int(market.timestamps[end - 1]),
                "close_price": round(float(segment[-1]), 6),
                "change_pct": round(change_pct, 4),
                "match_score": round(float(candidate.final_score or 0.0), 6),
                "_score_method": "cpu_multiscale_shapedtw",
                "_score_details": {key: round(value, 6) for key, value in score_details.items()},
                "_matched_bucket": f"{market.category}_{market.timeframe}",
                "_coarse_start": int(candidate.coarse_start),
                "_coarse_end": int(candidate.coarse_end),
                "_refined_start": start,
                "_refined_end": end,
                "_coarse_scale": int(candidate.scale),
                "_raw_bars": int(end - start),
                "_warping_path": candidate.warping_path,
                "_rank": rank,
            })
        return results, int(diagnostics.get("ncc_candidates", 0.0))

    def get_kline(
        self,
        symbol: str,
        timeframe: str = "1d",
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        limit: int = 200,
    ) -> Optional[Dict[str, Any]]:
        """返回一个标的的紧凑 K 线；旧 close-only 数据会明确标记为非精确 OHLC。"""
        with self._lock:
            market = next(
                (
                    item for item in self.market_series
                    if item.symbol == symbol and item.timeframe == timeframe
                ),
                None,
            )
        if market is None:
            return None

        timestamps = np.asarray(market.timestamps, dtype=np.int64)
        mask = np.ones(len(timestamps), dtype=bool)
        if from_ts is not None:
            mask &= timestamps >= int(from_ts)
        if to_ts is not None:
            mask &= timestamps <= int(to_ts)
        selected = np.flatnonzero(mask)
        if len(selected) == 0:
            return {
                "symbol": market.symbol,
                "name": market.symbol_name,
                "category": market.category,
                "tf": market.timeframe,
                "has_more": False,
                "ohlc_exact": market.ohlc_values is not None,
                "bars": [],
            }

        limit = max(10, min(int(limit), 500))
        has_more = len(selected) > limit or int(selected[0]) > 0
        selected = selected[-limit:]
        exact = market.ohlc_values is not None and market.ohlc_values.shape == (len(timestamps), 4)
        bars = []
        for index in selected:
            close = float(market.source_values[index])
            if exact:
                open_, high, low, close = [float(value) for value in market.ohlc_values[index]]
            else:
                open_ = high = low = close
            bars.append({
                "t": int(timestamps[index]),
                "o": round(open_, 8),
                "h": round(high, 8),
                "l": round(low, 8),
                "c": round(close, 8),
                "v": 0.0,
            })
        return {
            "symbol": market.symbol,
            "name": market.symbol_name,
            "category": market.category,
            "tf": market.timeframe,
            "has_more": bool(has_more),
            "ohlc_exact": bool(exact),
            "bars": bars,
        }

    def buckets_status(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._buckets_size)

    def bucket_latest_timestamp(self, category: str, timeframe: str) -> int:
        """返回品类×周期缓存中最新一根 K 线时间；空桶返回 0。"""
        values = self.bucket_latest_timestamps(category, timeframe)
        return max(values.values(), default=0)

    def bucket_latest_timestamps(self, category: str, timeframe: str) -> Dict[str, int]:
        """返回品类×周期内每个标的的末根 K 线时间，用于检查任一标的是否更新。"""
        with self._lock:
            latest: Dict[str, int] = {}
            for market in self.market_series:
                if market.category != category or market.timeframe != timeframe:
                    continue
                if len(market.timestamps) > 0:
                    latest[market.symbol] = max(
                        latest.get(market.symbol, 0), int(market.timestamps[-1])
                    )
            return latest

    def bucket_symbols(self, category: str, timeframe: str) -> List[Tuple[str, str]]:
        """返回当前缓存标的池；实时排名暂时不可用时仍可刷新这些标的的新 K 线。"""
        with self._lock:
            symbols: Dict[str, str] = {}
            for market in self.market_series:
                if market.category == category and market.timeframe == timeframe:
                    symbols.setdefault(market.symbol, market.symbol_name)
            return sorted(symbols.items())

    def reset(self) -> None:
        """清空 CPU 内存序列并删除明确的单个 NPZ 文件。"""
        with self._lock:
            self.market_series.clear()
            self._refresh_status()
            if self.store_path.exists():
                self.store_path.unlink()

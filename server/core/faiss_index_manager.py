# -*- coding: utf-8 -*-
"""
FAISS 索引管理器
========================================
分桶策略：category × timeframe
默认桶：crypto_1d / crypto_1h / stock_1d / stock_1h / futures_1d / futures_1h
每桶使用 FAISS IndexFlatIP（内积=余弦相似度，因为向量已L2归一化）。
元数据存储在 SQLite 中，faiss_id → ShapeSample 详情。

修改记录：
- 2026-08-27：默认关闭峰谷/趋势硬规则筛选，改为特征向量召回后直接进行序列相似度精排；返回走势点供 UI 对比。
- 2026-08-28：禁止将 FAISS 粗召回内积当作最终相似度；序列缓存缺失时改用 raw_points 补做绝对多指标精排。
使用方式：search(...) 默认纯特征匹配；仅兼容旧流程时显式传 use_hard_filter=True。
"""
from __future__ import annotations
import os
import json
import sqlite3
import shutil
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
from dataclasses import asdict
from contextlib import contextmanager

import numpy as np
import faiss

from .shape_features import VECTOR_DIM
from .mock_shape_generator import ShapeSample


BUCKETS = [
    "crypto_1d", "crypto_1h", "crypto_4h",
    "stock_1d", "stock_1h", "stock_4h",
    "futures_1d", "futures_1h", "futures_4h",
]


def _bucket_of(category: str, timeframe: str) -> str:
    return f"{category}_{timeframe}"


class FaissIndexManager:
    """按 category×timeframe 分桶的 FAISS 管理器。"""

    def __init__(self, data_dir: str = "./data/faiss_indexes"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.indexes: Dict[str, faiss.Index] = {}
        self.meta_db = self.data_dir / "meta.db"
        self._init_meta_db()
        self._buckets_size: Dict[str, int] = {b: 0 for b in BUCKETS}
        # 内存缓存：{bucket: {faiss_id: dict}} ，搜索时 O(1) 取，彻底避免 SQLite IN 查询
        self._stats_cache: Dict[str, Dict[int, Dict]] = {b: {} for b in BUCKETS}
        # Soft-DTW 精排需要的 seq_128：{bucket: {faiss_id: ndarray(128,2)}}
        self._seq_cache: Dict[str, Dict[int, np.ndarray]] = {b: {} for b in BUCKETS}
        # Soft-DTW 精排需要的向量：{bucket: {faiss_id: ndarray(303,)}}
        self._vec_cache: Dict[str, Dict[int, np.ndarray]] = {b: {} for b in BUCKETS}
        # 精排融合分数缓存（search 方法内部使用）
        self._last_fused_scores: Dict[int, float] = {}
        self._last_score_details: Dict[int, Dict[str, float]] = {}

    # ------------------------------------------------------------
    # SQLite 元数据管理（用 SQLite 存详细信息，轻量无依赖）
    # ------------------------------------------------------------
    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.meta_db)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init_meta_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS shape_samples (
                    row_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    faiss_id    INTEGER NOT NULL,
                    bucket      TEXT    NOT NULL,
                    sample_json TEXT    NOT NULL,
                    peak        INTEGER NOT NULL DEFAULT 0,
                    valley      INTEGER NOT NULL DEFAULT 0,
                    max_idx     INTEGER NOT NULL DEFAULT 0,
                    min_idx     INTEGER NOT NULL DEFAULT 0,
                    trend_slope REAL    NOT NULL DEFAULT 0,
                    trend_r2    REAL    NOT NULL DEFAULT 0,
                    volatility  REAL    NOT NULL DEFAULT 0,
                    displacement REAL   NOT NULL DEFAULT 0,
                    first_half_mean REAL NOT NULL DEFAULT 0,
                    second_half_mean REAL NOT NULL DEFAULT 0,
                    start_slope REAL    NOT NULL DEFAULT 0,
                    end_slope   REAL    NOT NULL DEFAULT 0
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_bucket_fid ON shape_samples(bucket, faiss_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pv ON shape_samples(bucket, peak, valley, trend_slope)")

    # ------------------------------------------------------------
    # 构建/保存/加载
    # ------------------------------------------------------------
    def build_from_samples(self, samples: List[ShapeSample],
                           timeframe: str = "1d") -> int:
        """samples 默认写入 {category}_{timeframe} 桶。返回总条数。"""
        # 按 category 分组
        grouped: Dict[str, List[ShapeSample]] = {}
        for s in samples:
            bucket = _bucket_of(s.category, timeframe)
            grouped.setdefault(bucket, []).append(s)

        total = 0
        with self._conn() as c:
            for bucket, sps in grouped.items():
                if not sps:
                    continue
                vecs = np.vstack([s.vector for s in sps]).astype(np.float32)
                index = faiss.IndexFlatIP(VECTOR_DIM)
                index.add(vecs)
                self.indexes[bucket] = index
                self._buckets_size[bucket] = index.ntotal

                # 先从每个样本重新提取16维统计（因为seq_128经过完整pipeline）
                stats_list = []
                for s in sps:
                    # 用样本里已有的seq_128重算统计，速度更快
                    from .shape_features import extract_16d_stats
                    stats_list.append(extract_16d_stats(s.seq_128))

                # 写 SQLite（faiss_id = 0..ntotal-1）
                rows = []
                for idx, (s, st) in enumerate(zip(sps, stats_list)):
                    d = asdict(s)
                    d["raw_points"] = [[round(float(x), 5), round(float(y), 5)]
                                       for x, y in s.raw_points]
                    # 持久化已经特征流水线处理的序列，保证索引重载后的
                    # 精排曲线与建索引时完全一致，不受原始窗口长度影响。
                    d["_feature_points"] = [
                        [round(float(x), 6), round(float(y), 6)]
                        for x, y in s.seq_128
                    ]
                    d["seq_128"] = None
                    d["vector"] = None
                    pk = int(st.peak_count); vl = int(st.valley_count)
                    mx = int(st.max_point_idx); mn = int(st.min_point_idx)
                    ts = float(st.trend_slope); r2 = float(st.trend_r2)
                    vo = float(st.volatility); dp = float(st.total_displacement)
                    fm = float(st.first_half_mean); sm = float(st.second_half_mean)
                    ss = float(st.start_slope); es = float(st.end_slope)
                    rows.append((
                        idx, bucket,
                        json.dumps(d, ensure_ascii=False),
                        pk, vl, mx, mn, ts, r2, vo, dp, fm, sm, ss, es,
                    ))
                    # ⭐同时写内存缓存，搜索时 O(1) 取
                    self._stats_cache[bucket][idx] = {
                        "peak": pk, "valley": vl, "trend_slope": ts, "displacement": dp
                    }
                    # 同时缓存 seq_128 和向量给 Soft-DTW 精排（均为 float32 压缩存，内存占用 < 2MB/桶/10k条）
                    self._seq_cache[bucket][idx] = s.seq_128.astype(np.float32)
                    self._vec_cache[bucket][idx] = vecs[idx].astype(np.float32)
                c.executemany(
                    "INSERT INTO shape_samples(faiss_id,bucket,sample_json,peak,valley,"
                    "max_idx,min_idx,trend_slope,trend_r2,volatility,displacement,"
                    "first_half_mean,second_half_mean,start_slope,end_slope) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                total += len(sps)
        self.save_all()
        return total

    def save_all(self):
        for bucket, index in self.indexes.items():
            fp = self.data_dir / f"{bucket}.faiss"
            faiss.write_index(index, str(fp))

    def load_all(self, require_exist: bool = False) -> List[str]:
        """加载全部已存在的桶索引 + 把统计特征从 SQLite 载入内存缓存。返回加载了的桶名列表。"""
        loaded = []
        for bucket in BUCKETS:
            fp = self.data_dir / f"{bucket}.faiss"
            if fp.exists():
                self.indexes[bucket] = faiss.read_index(str(fp))
                self._buckets_size[bucket] = self.indexes[bucket].ntotal
                loaded.append(bucket)
            elif require_exist:
                raise FileNotFoundError(f"缺失索引文件: {fp}")
        # 从 SQLite 载入统计特征到内存缓存
        with self._conn() as c:
            cur = c.execute(
                "SELECT bucket, faiss_id, peak, valley, trend_slope, displacement FROM shape_samples"
            )
            for r in cur.fetchall():
                b = r["bucket"]
                if b not in self._stats_cache:
                    self._stats_cache[b] = {}
                self._stats_cache[b][int(r["faiss_id"])] = {
                    "peak": int(r["peak"]),
                    "valley": int(r["valley"]),
                    "trend_slope": float(r["trend_slope"]),
                    "displacement": float(r["displacement"]),
                }
        return loaded

    # ------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------
    def search(self,
               query_vector: np.ndarray,
               category: str = "all",
               timeframe: str = "1d",
               top_k: int = 10,
               query_stats: Optional[Any] = None,
               query_seq_128: Optional[np.ndarray] = None,
               use_hard_filter: bool = False) -> Tuple[List[Dict[str, Any]], int]:
        """
        返回 (结果列表, 总命中候选数)。category="all" 并发查全部品类桶。
        默认只使用 303D 特征向量召回 + 序列 DTW 融合精排，不按固定形态分类筛选。
        use_hard_filter=True 仅用于兼容旧的峰谷/趋势规则流程。
        """
        if query_vector.ndim == 1:
            query_vector = query_vector.reshape(1, -1).astype(np.float32)
        else:
            query_vector = query_vector.astype(np.float32)

        # 如果没传 query_stats，自动从 query_seq_128 提取（或从 stats 实例）
        q_peak = None; q_valley = None; q_trend = None; q_disp = None
        if query_stats is not None and hasattr(query_stats, "peak_count"):
            q_peak = int(query_stats.peak_count)
            q_valley = int(query_stats.valley_count)
            q_trend = float(query_stats.trend_slope)
            q_disp = float(query_stats.total_displacement)
        elif query_seq_128 is not None:
            from .shape_features import extract_16d_stats
            st = extract_16d_stats(query_seq_128)
            q_peak = int(st.peak_count)
            q_valley = int(st.valley_count)
            q_trend = float(st.trend_slope)
            q_disp = float(st.total_displacement)

        # 选择要查的桶
        if category == "all":
            cats = ["crypto", "stock", "futures"]
        else:
            cats = [category]
        buckets_to_search = [_bucket_of(c, timeframe) for c in cats]
        buckets_to_search = [b for b in buckets_to_search if b in self.indexes and self.indexes[b].ntotal > 0]
        if not buckets_to_search:
            return [], 0

        all_scores = []
        all_ids = []
        all_buckets = []
        # 粗召回放大：top_k × 40 倍（保证FAISS阶段就把"目标形态同类型"候选都召回进来）
        recall_multiplier = 40
        for bucket in buckets_to_search:
            index = self.indexes[bucket]
            k = min(index.ntotal, max(top_k * recall_multiplier, 100))
            scores, ids = index.search(query_vector, k)
            for s, fid in zip(scores[0], ids[0]):
                if fid < 0:
                    continue
                all_scores.append(float(s))
                all_ids.append(int(fid))
                all_buckets.append(bucket)
        if not all_scores:
            return [], 0

        # ------------------------------------------------------------
        # ⭐⭐⭐ 一级硬规则过滤（内存字典O(1)，彻底消除 SQLite 查询延迟）⭐⭐⭐
        # 宽松版：
        #  - 峰谷数 ±2 容错
        #  - 趋势方向只过滤"极显著相反"（阈值 0.01 = 大约 1% 量级明显反向）
        #  - 位移符号只过滤"极显著相反"（阈值 0.1 = 大约 10% 量级明显反向）
        # ------------------------------------------------------------
        total_candidates = len(all_scores)
        filter_mask = np.ones(total_candidates, dtype=bool)
        if use_hard_filter and (q_peak is not None or q_valley is not None or q_trend is not None):
            for pos in range(total_candidates):
                b = all_buckets[pos]
                fid = all_ids[pos]
                bucket_cache = self._stats_cache.get(b, {})
                row = bucket_cache.get(fid)
                if row is None:
                    filter_mask[pos] = False
                    continue
                # 1) 峰/谷数容错 ±2
                if q_peak is not None:
                    if abs(row["peak"] - q_peak) > 2:
                        filter_mask[pos] = False; continue
                if q_valley is not None:
                    if abs(row["valley"] - q_valley) > 2:
                        filter_mask[pos] = False; continue
                # 2) 趋势方向一致性（仅过滤明显反向，>0.01 ≈ 1% 总幅度）
                cand_trend = row["trend_slope"]
                if q_trend is not None and abs(q_trend) > 0.005:
                    if q_trend > 0 and cand_trend < -0.01:
                        filter_mask[pos] = False; continue
                    if q_trend < 0 and cand_trend >  0.01:
                        filter_mask[pos] = False; continue
                # 3) 首尾位移符号一致（仅过滤明显反向，>0.1）
                cand_disp = row["displacement"]
                if q_disp is not None and abs(q_disp) > 0.05:
                    if q_disp > 0 and cand_disp < -0.1:
                        filter_mask[pos] = False; continue
                    if q_disp < 0 and cand_disp >  0.1:
                        filter_mask[pos] = False; continue

        kept = np.where(filter_mask)[0]
        if len(kept) == 0:
            # 如果硬过滤后一条没剩，放宽条件：只按原 FAISS 排序（兜底）
            kept = np.arange(total_candidates)

        # 按原得分排序（向量内积，越大越好）
        kept_scores = np.array(all_scores)[kept]
        order = np.argsort(-kept_scores)
        kept_sorted = kept[order]

        # ------------------------------------------------------------
        # 绝对多指标精排
        # ------------------------------------------------------------
        # FAISS 内积只用于粗召回，不具备“97.2% 形态相似”的语义。
        # 只要提供了查询序列，所有有资格进入最终 TopK 的候选都必须完成
        # DTW + RMSE + 导数 DTW + 相关系数的绝对标尺评分。
        self._last_fused_scores = {}
        self._last_score_details = {}
        metadata_by_pos: Dict[int, Dict[str, Any]] = {}

        if query_seq_128 is not None:
            from .shape_features import calibrated_shape_similarity_batch

            # 自适应放缩会让同一标的出现多个候选，因此精排池需要明显大于 TopK。
            rerank_k = min(len(kept_sorted), max(400, top_k * 40))
            rerank_positions = kept_sorted[:rerank_k]
            query_y = np.asarray(query_seq_128, dtype=np.float64)[:, 1]
            calibrated_positions: List[int] = []
            candidate_y_list: List[np.ndarray] = []
            missing_sequence = 0
            prefetched_meta = self._fetch_meta_many([
                (all_buckets[int(pos)], all_ids[int(pos)])
                for pos in rerank_positions
            ])

            for raw_pos in rerank_positions:
                pos = int(raw_pos)
                bucket = all_buckets[pos]
                fid = all_ids[pos]
                meta = prefetched_meta.get((bucket, fid))
                if not meta:
                    continue
                metadata_by_pos[pos] = meta

                cached_seq = self._seq_cache.get(bucket, {}).get(fid)
                if cached_seq is not None:
                    candidate_y = np.asarray(cached_seq, dtype=np.float64)[:, 1]
                else:
                    # 直接加载已有 FAISS 索引时内存里没有 seq_128；
                    # 新索引优先读取持久化的特征序列，旧索引则用 raw_points
                    # 作为可视曲线回退，绝不把 FAISS 内积当精排分数。
                    feature_points = meta.get("_feature_points") or []
                    feature_array = np.asarray(feature_points, dtype=np.float64)
                    if feature_array.ndim == 2 and feature_array.shape[0] >= 2 and feature_array.shape[1] >= 2:
                        rebuilt_seq = feature_array[:, :2]
                    else:
                        raw_points = meta.get("raw_points") or []
                        try:
                            raw_array = np.asarray(raw_points, dtype=np.float64)
                        except (TypeError, ValueError):
                            missing_sequence += 1
                            continue
                        if raw_array.ndim != 2 or raw_array.shape[0] < 2 or raw_array.shape[1] < 2:
                            missing_sequence += 1
                            continue
                        rebuilt_seq = raw_array[:, :2]
                    self._seq_cache.setdefault(bucket, {})[fid] = np.asarray(
                        rebuilt_seq,
                        dtype=np.float32,
                    )
                    candidate_y = np.asarray(rebuilt_seq, dtype=np.float64)[:, 1]

                if len(candidate_y) < 2 or not np.all(np.isfinite(candidate_y)):
                    missing_sequence += 1
                    continue
                calibrated_positions.append(pos)
                candidate_y_list.append(candidate_y)

            if calibrated_positions:
                scores, score_details = calibrated_shape_similarity_batch(
                    query_y,
                    candidate_y_list,
                )
                valid_positions: List[int] = []
                for pos, score, score_detail in zip(
                    calibrated_positions,
                    scores,
                    score_details,
                ):
                    if not np.isfinite(score):
                        missing_sequence += 1
                        continue
                    score_detail["faiss_cosine"] = float(
                        max(-1.0, min(1.0, all_scores[pos]))
                    )
                    self._last_fused_scores[pos] = float(score)
                    self._last_score_details[pos] = score_detail
                    valid_positions.append(pos)
                calibrated_positions = valid_positions

            # 关键保证：有 query_seq_128 时，未精排候选不得混入最终结果。
            kept_sorted = np.array(
                sorted(
                    calibrated_positions,
                    key=lambda p: self._last_fused_scores[p],
                    reverse=True,
                ),
                dtype=np.int64,
            )
            print(
                f"[SEARCH][RERANK] recalled={total_candidates}，evaluated={rerank_k}，"
                f"calibrated={len(calibrated_positions)}，missing_sequence={missing_sequence}",
                flush=True,
            )

        results = []
        seen_symbols: set = set()
        for pos in kept_sorted:
            if len(results) >= top_k:
                break
            bucket = all_buckets[pos]
            fid = all_ids[pos]
            meta = metadata_by_pos.get(int(pos)) or self._fetch_meta(bucket, fid)
            if not meta:
                continue
            detail: Dict[str, Any] = meta  # _fetch_meta 直接返回 sample dict
            sym_key = (detail["category"], detail["symbol"])
            if sym_key in seen_symbols:
                continue
            seen_symbols.add(sym_key)
            # 有查询序列时 kept_sorted 只会包含已完成精排的候选。
            fused = self._last_fused_scores.get(pos)
            if fused is not None:
                match_score = float(fused)
                ip_score = float(max(-1.0, min(1.0, all_scores[pos])))
                score_method = "absolute_multimetric"
            else:
                ip = all_scores[pos]
                # 仅对没有 query_seq_128 的兼容调用保留，UI 匹配不会走此分支。
                match_score = float(max(0.0, min(1.0, ip)))
                ip_score = match_score
                score_method = "faiss_recall_only"
            detail["match_score"] = round(match_score, 4)
            detail["_raw_ip"] = round(ip_score, 5)
            detail["_matched_bucket"] = bucket
            detail["_score_method"] = score_method
            # 内部精排序列不需要在 API/UI 结果中重复传输。
            detail.pop("_feature_points", None)
            score_detail = self._last_score_details.get(pos)
            if score_detail is not None:
                detail["_score_details"] = {
                    key: round(float(value), 6) for key, value in score_detail.items()
                }
            if q_peak is not None:
                st_meta = self._stats_cache.get(bucket, {}).get(fid, {})
                if st_meta:
                    detail["_cand_peak"] = st_meta.get("peak", -1)
                    detail["_cand_valley"] = st_meta.get("valley", -1)
                    detail["_cand_trend"] = round(st_meta.get("trend_slope", 0), 5)
            results.append(detail)
        results.sort(key=lambda x: -x["match_score"])
        return results, total_candidates

    def _fetch_meta_many(
        self,
        entries: List[Tuple[str, int]],
    ) -> Dict[Tuple[str, int], Dict[str, Any]]:
        """在一个 SQLite 连接中批量读取精排候选，避免逐条建立连接。"""
        grouped: Dict[str, List[int]] = {}
        for bucket, faiss_id in entries:
            grouped.setdefault(bucket, []).append(int(faiss_id))

        fetched: Dict[Tuple[str, int], Dict[str, Any]] = {}
        with self._conn() as c:
            for bucket, faiss_ids in grouped.items():
                unique_ids = list(dict.fromkeys(faiss_ids))
                # SQLite 默认变量数有上限，分块保持兼容。
                for start in range(0, len(unique_ids), 800):
                    chunk = unique_ids[start:start + 800]
                    placeholders = ",".join("?" for _ in chunk)
                    rows = c.execute(
                        "SELECT faiss_id, sample_json FROM shape_samples "
                        f"WHERE bucket=? AND faiss_id IN ({placeholders})",
                        [bucket, *chunk],
                    ).fetchall()
                    for row in rows:
                        sample = json.loads(row["sample_json"])
                        sample.pop("seq_128", None)
                        sample.pop("vector", None)
                        fetched[(bucket, int(row["faiss_id"]))] = sample
        return fetched

    def _fetch_meta(self, bucket: str, faiss_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute(
                "SELECT sample_json FROM shape_samples WHERE bucket=? AND faiss_id=?",
                (bucket, faiss_id)
            ).fetchone()
            if not row:
                return None
            j = json.loads(row["sample_json"])
            # seq_128/vector 未写入 SQLite；raw_points 保留给 UI 绘制对比图和缩略图。
            j.pop("seq_128", None)
            j.pop("vector", None)
            return j

    def buckets_status(self) -> Dict[str, int]:
        return dict(self._buckets_size)

    def reset(self):
        """清空全部索引与元数据（用于重建）。"""
        self.indexes.clear()
        self._buckets_size = {b: 0 for b in BUCKETS}
        if self.meta_db.exists():
            self.meta_db.unlink()
        for fp in self.data_dir.glob("*.faiss"):
            fp.unlink()

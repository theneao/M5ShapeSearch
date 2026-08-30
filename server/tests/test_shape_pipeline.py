# -*- coding: utf-8 -*-
"""
本地脚本：快速验证「特征提取 → FAISS构建 → 搜索」完整链路（无需启动HTTP服务）。
适合在CI/本地快速迭代特征工程算法。
用法：
    cd server
    python tests/test_shape_pipeline.py
"""
from __future__ import annotations
import sys, os, time, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from core import (
    generate_samples,
    FaissIndexManager,
    full_pipeline,
    SHAPE_TYPES, SHAPE_CN_NAMES,
)
from tests.test_shape_match_api import mock_handdraw


def main():
    data_dir = "./tests/mock/_tmp_faiss"
    shutil.rmtree(data_dir, ignore_errors=True)

    t0 = time.perf_counter()
    samples = generate_samples(per_type_count=10, categories=("crypto","stock","futures"), augment_per_clean=6)
    print(f"[STEP1] 生成样本 {len(samples)} 条，耗时 {time.perf_counter()-t0:.2f}s")

    t0 = time.perf_counter()
    mgr = FaissIndexManager(data_dir=data_dir)
    total = mgr.build_from_samples(samples, timeframe="1d")
    print(f"[STEP2] 构建 FAISS 索引 1d桶={total}，耗时 {time.perf_counter()-t0:.2f}s  桶状态：{mgr.buckets_status()}")

    print(f"[STEP3] 10形态 × 5次扰动 = 50次匹配测试：")
    hit_top1 = 0
    hit_top10_total = 0
    max_q_ms = 0.0
    N_PER = 5
    per_type_top10 = {}
    for st in SHAPE_TYPES:
        local10 = 0
        for k in range(N_PER):
            pts = mock_handdraw(st, n=180, noise=0.03, deform=0.08)
            s128, stats, vec = full_pipeline(pts)
            tq = time.perf_counter()
            results, total_candidates = mgr.search(vec, category="all", timeframe="1d",
                                                   top_k=10, query_stats=stats,
                                                   query_seq_128=s128)
            q_ms = (time.perf_counter() - tq) * 1000
            max_q_ms = max(max_q_ms, q_ms)
            top1_st = results[0]["shape_type"] if results else "-"
            same_in_10 = sum(1 for r in results if r["shape_type"] == st)
            hit_top10_total += same_in_10
            local10 += same_in_10
            if top1_st == st:
                hit_top1 += 1
        per_type_top10[st] = local10
        print(f"   * {SHAPE_CN_NAMES[st]:<8} Top10同形态命中 {local10}/{10*N_PER}")

    total_tests = len(SHAPE_TYPES) * N_PER
    top1_ratio = hit_top1/total_tests
    top10_ratio = hit_top10_total/(total_tests*10)
    print()
    print(f"[RESULT] Top1 命中率：{hit_top1}/{total_tests} = {top1_ratio*100:.1f}%  (目标 >=80%)")
    print(f"[RESULT] Top10 同形态命中数：{hit_top10_total}/{total_tests*10} = {top10_ratio*100:.1f}%  (目标 >=60%)")
    print(f"[RESULT] 单次查询耗时 最大={max_q_ms:.2f}ms  (目标 <60ms)")
    # 保存/加载一次验证
    mgr2 = FaissIndexManager(data_dir=data_dir)
    loaded = mgr2.load_all()
    print(f"[INFO] 索引磁盘持久化 OK，加载桶：{loaded}")
    shutil.rmtree(data_dir, ignore_errors=True)

    ok = top1_ratio >= 0.8 and top10_ratio >= 0.6 and max_q_ms < 60
    print(f"\n[FINAL] 全链路本地算法验证: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

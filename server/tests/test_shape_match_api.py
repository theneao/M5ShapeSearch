# -*- coding: utf-8 -*-
"""
模拟手绘形态匹配测试脚本
========================================
功能：
1. 生成 10 大类典型手绘曲线（带扰动，模拟真实手绘不完美）
2. POST /api/v1/shape/match 发送给服务端
3. 验证：时延 < 180ms ； Top10 中同形态命中率 ≥ 60% ；Top1 命中正确形态
4. 输出美观的测试报告表格

用法：
    # 先启动服务（另一个终端）
    pip install -r requirements.txt
    python main.py
    # 跑测试
    cd tests
    python test_shape_match_api.py

修改时间：2026-08-28
修改作用：把联调辅助函数移出 pytest 的 test_ 命名空间，避免被误判为需要 fixture 的测试用例。
"""
from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import json
from typing import List, Tuple, Dict
import numpy as np

try:
    import requests
except ImportError:
    print("请先安装: pip install requests numpy")
    sys.exit(1)

# 服务端地址，支持环境变量覆盖
BASE_URL = os.environ.get("MATCH_SERVER", "http://127.0.0.1:8000")

from core.mock_shape_generator import (
    SHAPE_TYPES, SHAPE_CN_NAMES, _SHAPE_GENS,
)


# ============================================================
# 模拟手绘数据生成（加扰动 + 变形，模拟用户画得不完全准确）
# ============================================================
def mock_handdraw(shape_type: str, n: int = 200, noise: float = 0.035,
                  deform: float = 0.08) -> List[Tuple[float, float]]:
    """模拟用户手绘：加噪声 + 时间轴压缩/拉伸 + 高度压缩。"""
    rng = np.random.RandomState(seed=sum(ord(c) for c in shape_type) + 7)
    amp = 0.75 + rng.uniform(-0.1, 0.1)
    ys = _SHAPE_GENS[shape_type](n, amp, noise)
    ys = np.clip(ys, 0, 1)
    # x轴轻微非线性变形（模拟手绘速度不一致）
    xs_raw = np.linspace(0, 1, n)
    warp = np.sin(xs_raw * rng.randint(2, 5)) * deform
    xs = np.clip(xs_raw + warp, 0, 1)
    xs = np.sort(xs)
    # 随机丢 10% 的点（模拟触屏采样不均匀）
    keep = rng.rand(n) > 0.1
    xs = xs[keep]
    ys = ys[keep]
    # 高度随机缩放 0.8~1.1，位置平移 ±0.1（模拟用户大小/位置画不准）
    scale = 0.85 + rng.rand() * 0.35
    shift = rng.uniform(-0.1, 0.1)
    ys = np.clip(ys * scale + shift, 0, 1)
    return [(round(float(x), 5), round(float(y), 5)) for x, y in zip(xs, ys)]


def send_match(points: List[Tuple[float, float]], category: str = "all",
               timeframe: str = "1d", limit: int = 10) -> Tuple[dict, float, int]:
    """发送请求并返回 (resp_json, http_rtt_ms, http_status)。"""
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            f"{BASE_URL}/api/v1/shape/match",
            json={
                "points": points,
                "category": category,
                "timeframe": timeframe,
                "limit": limit,
            },
            timeout=3.0,
            headers={"Content-Type": "application/json",
                     "Accept-Encoding": "gzip"},
        )
        rtt = (time.perf_counter() - t0) * 1000
        if resp.status_code != 200:
            return {"_error": resp.text}, rtt, resp.status_code
        return resp.json(), rtt, resp.status_code
    except Exception as e:
        return {"_error": str(e)}, (time.perf_counter()-t0)*1000, 0


# ============================================================
# 单形态完整测试
# ============================================================
def run_one_shape(shape_type: str) -> Dict:
    points = mock_handdraw(shape_type)
    resp, rtt_ms, status = send_match(points)
    ok = status == 200 and "_error" not in resp and resp.get("code") == 0
    top_shape_types = []
    top_scores = []
    top_matches = []
    same_as_query_in_top10 = 0
    top1_match_correct = False
    query_ms = 0
    if ok:
        data = resp.get("data", {})
        query_ms = data.get("query_ms", 0)
        top_matches = data.get("list", [])
        for it in top_matches:
            st = it.get("shape_type", "")
            sc = it.get("match_score", 0)
            top_shape_types.append(st)
            top_scores.append(sc)
            if st == shape_type:
                same_as_query_in_top10 += 1
        if top_shape_types and top_shape_types[0] == shape_type:
            top1_match_correct = True
    return {
        "shape_type": shape_type,
        "shape_cn": SHAPE_CN_NAMES.get(shape_type, shape_type),
        "query_points": len(points),
        "http_rtt_ms": round(rtt_ms, 1),
        "server_query_ms": query_ms,
        "http_ok": ok,
        "http_status": status,
        "top1_shape": top_shape_types[0] if top_shape_types else "-",
        "top1_score": round(top_scores[0], 4) if top_scores else 0,
        "top1_correct": top1_match_correct,
        "same_shape_in_top10": same_as_query_in_top10,
        "top10_hit_rate_pct": round(same_as_query_in_top10 / max(1,min(10,len(top_matches)))*100, 1),
        "top10_all_types": top_shape_types,
        "top10_all_scores": [round(s,3) for s in top_scores],
        "error": resp.get("_error") if "_error" in resp else "",
    }


def print_report(results: List[Dict]):
    ok_all = sum(1 for r in results if r["http_ok"])
    top1_hit = sum(1 for r in results if r["top1_correct"])
    avg_top10_pct = np.mean([r["top10_hit_rate_pct"] for r in results if r["http_ok"]])
    avg_rtt = np.mean([r["http_rtt_ms"] for r in results if r["http_ok"]])
    max_rtt = np.max([r["http_rtt_ms"] for r in results if r["http_ok"]])
    avg_server_q = np.mean([r["server_query_ms"] for r in results if r["http_ok"]])
    max_server_q = np.max([r["server_query_ms"] for r in results if r["http_ok"]])

    sep = "─" * 95
    print("\n" + sep)
    print(f"📊 形态匹配全量测试报告 （服务端 {BASE_URL}）")
    print(sep)
    hdr = f"| {'形态类型':<10}| {'中文名':<10}| {'点数':>4}|" \
          f"{'HTTP-RTT':>8}ms|{'查询耗时':>8}ms|{'Top1命中':>7}|{'Top1匹配分':>9}|" \
          f"{'Top10命中率':>10}|"
    print(hdr)
    print(sep)
    for r in results:
        mark1 = "✅" if r["top1_correct"] else "❌"
        pct = f"{r['top10_hit_rate_pct']:.1f}%"
        print(f"| {r['shape_type']:<10}| {r['shape_cn']:<10}| {r['query_points']:>4}|"
              f"{r['http_rtt_ms']:>8.1f} |{r['server_query_ms']:>8} |"
              f"{mark1:^7} |{r['top1_score']:>9.4f} |{pct:>10} |")
    print(sep)
    print()
    print(f"  ✅ HTTP成功: {ok_all}/{len(results)}")
    print(f"  🎯 Top1 形态正确命中: {top1_hit}/{len(results)}  ({top1_hit/len(results)*100:.1f}%)")
    print(f"  🎯 Top10 内相同形态平均占比: {avg_top10_pct:.1f}%")
    print(f"  ⚡ HTTP RTT  平均={avg_rtt:.1f}ms  最大={max_rtt:.1f}ms"
          + ("  ✅<180ms" if max_rtt < 180 else "  ❌超时"))
    print(f"  ⚡ 服务端查询 平均={avg_server_q:.1f}ms  最大={max_server_q:.1f}ms"
          + ("  ✅<60ms" if max_server_q < 60 else "  ⚠️ 需优化"))

    # 判定验收
    all_pass = (
        ok_all == len(results)
        and top1_hit / len(results) >= 0.8
        and avg_top10_pct >= 60.0
        and max_rtt < 180
        and max_server_q < 60
    )
    print()
    if all_pass:
        print("🎉 全部验收指标通过 ✅✅✅")
    else:
        print("⚠️  部分指标未达标，需要进一步优化：")
        if top1_hit/len(results) < 0.8: print("   ❌ Top1命中率需≥80%，建议增大样本/特征区分度")
        if avg_top10_pct < 60: print("   ❌ Top10同形态命中率需≥60%")
        if max_rtt >= 180: print("   ❌ HTTP RTT 需<180ms")
        if max_server_q >= 60: print("   ❌ 服务端查询需<60ms")
    print()
    return all_pass


def main():
    print(f"🚀 连接服务端: {BASE_URL}")
    # 健康检查
    try:
        r = requests.get(f"{BASE_URL}/api/v1/health", timeout=3).json()
        print(f"✅ 健康检查通过，索引桶：{r['data']['buckets']}")
    except Exception as e:
        print(f"❌ 无法连接服务端：{e}，请先执行 `cd server && pip install -r requirements.txt && python main.py`")
        sys.exit(2)

    # 10 类形态各跑 1 次
    results = []
    for st in SHAPE_TYPES:
        r = run_one_shape(st)
        results.append(r)
        # 实时输出一个简表行
        mark = "✅" if r["top1_correct"] else "❌"
        print(f"   {mark} {r['shape_cn']:<8} → Top1={r['top1_shape']:<12} "
              f"得分={r['top1_score']:.3f}  RTT={r['http_rtt_ms']:.0f}ms  Q={r['server_query_ms']}ms")

    return 0 if print_report(results) else 1


if __name__ == "__main__":
    sys.exit(main())

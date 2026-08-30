#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
形态匹配服务 - 完整端到端演示脚本
========================================
功能：
1. 启动 FAISS 服务
2. 生成各类型形态样本
3. 执行多轮匹配演示
4. 输出性能报告和可视化建议

使用：
    python demo_end_to_end.py [--server-url http://localhost:8000] [--verbose]
"""
import sys
import os
import argparse
import time
import json
from typing import List, Tuple, Dict, Any
import numpy as np

try:
    import requests
except ImportError:
    print("[ERROR] 需要 requests 库，请先执行: pip install requests")
    sys.exit(1)


class ShapeMatchDemo:
    """形态匹配演示类"""

    def __init__(self, server_url: str = "http://127.0.0.1:8000", verbose: bool = False):
        self.server_url = server_url
        self.verbose = verbose
        self.results = []
        self.timings = {"http_rtt": [], "query_ms": []}

    def health_check(self) -> bool:
        """健康检查"""
        try:
            r = requests.get(f"{self.server_url}/api/v1/health", timeout=3)
            if r.status_code == 200:
                data = r.json()
                buckets = data.get("data", {}).get("buckets", {})
                print(f"[OK] 服务健康。索引桶: {dict(list(buckets.items())[:3])}...")
                return True
        except Exception as e:
            print(f"[FAIL] 服务不可用: {e}")
            return False
        return False

    def generate_test_shapes(self) -> Dict[str, List[Tuple[float, float]]]:
        """生成10种测试形态"""
        shapes = {}

        # 1. 直线上升
        xs = np.linspace(0, 1, 100)
        ys = np.linspace(0.2, 0.8, 100)
        shapes["uptrend"] = [(float(x), float(y)) for x, y in zip(xs, ys)]

        # 2. 直线下降
        ys_down = np.linspace(0.8, 0.2, 100)
        shapes["downtrend"] = [(float(x), float(y)) for x, y in zip(xs, ys_down)]

        # 3. W型双底
        ys_w = np.concatenate([
            np.linspace(0.5, 0.2, 25),
            np.linspace(0.2, 0.5, 25),
            np.linspace(0.5, 0.2, 25),
            np.linspace(0.2, 0.5, 25)
        ])
        shapes["w_bottom"] = [(float(x), float(y)) for x, y in zip(xs, ys_w)]

        # 4. M型双头
        ys_m = np.concatenate([
            np.linspace(0.2, 0.8, 25),
            np.linspace(0.8, 0.5, 25),
            np.linspace(0.5, 0.8, 25),
            np.linspace(0.8, 0.2, 25)
        ])
        shapes["m_top"] = [(float(x), float(y)) for x, y in zip(xs, ys_m)]

        # 5. V型反弹
        ys_v = np.concatenate([
            np.linspace(0.8, 0.1, 50),
            np.linspace(0.1, 0.8, 50)
        ])
        shapes["v_rebound"] = [(float(x), float(y)) for x, y in zip(xs, ys_v)]

        # 6. 倒V回落
        ys_inv_v = np.concatenate([
            np.linspace(0.2, 0.9, 50),
            np.linspace(0.9, 0.2, 50)
        ])
        shapes["inverted_v"] = [(float(x), float(y)) for x, y in zip(xs, ys_inv_v)]

        # 7. 三角收敛
        ys_tri_con = np.concatenate([
            np.linspace(0.3, 0.5, 50),
            np.linspace(0.7, 0.5, 50)
        ])
        shapes["triangle_con"] = [(float(x), float(y)) for x, y in zip(xs, ys_tri_con)]

        # 8. 三角发散
        ys_tri_div = np.concatenate([
            np.linspace(0.5, 0.2, 50),
            np.linspace(0.5, 0.8, 50)
        ])
        shapes["triangle_div"] = [(float(x), float(y)) for x, y in zip(xs, ys_tri_div)]

        # 9. 横盘震荡
        ys_sideways = 0.5 + 0.2 * np.sin(np.linspace(0, 6*np.pi, 100))
        shapes["sideways"] = [(float(x), float(y)) for x, y in zip(xs, ys_sideways)]

        # 10. 阶梯上涨
        ys_ladder = np.concatenate([
            np.linspace(0.2, 0.35, 25),
            np.linspace(0.35, 0.35, 10),
            np.linspace(0.35, 0.5, 25),
            np.linspace(0.5, 0.5, 10),
            np.linspace(0.5, 0.65, 25),
            np.linspace(0.65, 0.65, 5)
        ])
        shapes["ladder_up"] = [(float(x), float(y)) for x, y in zip(xs[:len(ys_ladder)], ys_ladder)]

        return shapes

    def match_shape(self, points: List[Tuple[float, float]], shape_name: str = "test") -> Dict[str, Any]:
        """调用匹配 API"""
        try:
            t0 = time.perf_counter()
            r = requests.post(
                f"{self.server_url}/api/v1/shape/match",
                json={
                    "points": points,
                    "category": "all",
                    "timeframe": "1d",
                    "limit": 5
                },
                timeout=5
            )
            http_rtt = (time.perf_counter() - t0) * 1000
            self.timings["http_rtt"].append(http_rtt)

            if r.status_code != 200:
                return {"error": f"HTTP {r.status_code}", "http_rtt": http_rtt}

            data = r.json()
            if data.get("code") != 0:
                return {"error": data.get("msg", "API error"), "http_rtt": http_rtt}

            query_ms = data["data"].get("query_ms", 0)
            items = data["data"].get("list", [])
            self.timings["query_ms"].append(query_ms)

            return {
                "http_rtt": http_rtt,
                "query_ms": query_ms,
                "top1": items[0] if items else None,
                "top5": items[:5],
                "shape_name": shape_name,
                "num_candidates": data["data"].get("total", 0)
            }
        except Exception as e:
            return {"error": str(e), "http_rtt": 0}

    def run_demo(self):
        """运行完整演示"""
        print("\n" + "="*80)
        print("🚀 M5StopWatch 形态匹配服务 - 完整演示")
        print("="*80 + "\n")

        # 1. 健康检查
        print("[STEP 1] 健康检查...")
        if not self.health_check():
            print("[ERROR] 服务不可用，请先启动服务")
            return False
        print()

        # 2. 生成测试形态
        print("[STEP 2] 生成10种标准形态...")
        shapes = self.generate_test_shapes()
        print(f"[OK] 生成 {len(shapes)} 种形态")
        print()

        # 3. 逐个匹配
        print("[STEP 3] 执行形态匹配..." + (" (详细模式)" if self.verbose else ""))
        print("-" * 80)

        for shape_name, points in shapes.items():
            result = self.match_shape(points, shape_name)
            self.results.append(result)

            if "error" in result:
                status = "❌"
                msg = f"Error: {result['error']}"
            else:
                top1 = result["top1"]
                if top1:
                    is_correct = top1["shape_type"] == shape_name
                    status = "✅" if is_correct else "❌"
                    msg = f"Top1={top1['shape_type']:<15} score={top1['match_score']:.4f}"
                else:
                    status = "⚠️"
                    msg = "No results"

            print(f"  {status} {shape_name:<15} → {msg:<50} RTT={result.get('http_rtt', 0):.0f}ms")

        print("-" * 80)
        print()

        # 4. 性能总结
        print("[STEP 4] 性能统计")
        print("-" * 80)

        valid_results = [r for r in self.results if "error" not in r]
        if valid_results:
            avg_http = np.mean([r["http_rtt"] for r in valid_results])
            max_http = np.max([r["http_rtt"] for r in valid_results])
            avg_query = np.mean([r["query_ms"] for r in valid_results])
            max_query = np.max([r["query_ms"] for r in valid_results])

            print(f"  请求数: {len(valid_results)}/{len(self.results)}")
            print(f"  HTTP RTT:     平均={avg_http:.1f}ms  最大={max_http:.1f}ms  (目标 <180ms) {'✅' if max_http<180 else '❌'}")
            print(f"  查询耗时:     平均={avg_query:.1f}ms  最大={max_query:.1f}ms  (目标 <60ms) {'✅' if max_query<60 else '❌'}")

            # 准确率
            correct = sum(1 for r in valid_results if r["top1"] and r["top1"]["shape_type"] == r["shape_name"])
            accuracy = correct / len(valid_results) * 100
            print(f"  Top1 命中率:  {correct}/{len(valid_results)} = {accuracy:.1f}%  (目标 ≥80%) {'✅' if accuracy>=80 else '❌'}")

        print("-" * 80)
        print()

        # 5. 建议
        print("[STEP 5] 优化建议")
        print("-" * 80)
        if valid_results:
            if accuracy < 80:
                print("  ❌ Top1 准确率未达标 (<80%)，建议：")
                print("     • 增加样本数量 (per_type_count: 120 → 200)")
                print("     • 增加数据增强倍数 (augment_per_clean: 6 → 10)")
                print("     • 调优向量权重 (peak_weight: 50 → 60)")
                print("     • 考虑采用 IVF-PQ 二级索引")
            else:
                print("  ✅ Top1 准确率达标！")

            if max_query >= 60:
                print("  ⚠️  查询耗时接近阈值，考虑：")
                print("     • 减小 recall_multiplier (40 → 30)")
                print("     • 减少 RERANK_K (30 → 20)")
                print("     • 启用 Redis 缓存")

            if max_http >= 180:
                print("  ⚠️  HTTP RTT 超限，考虑：")
                print("     • 增加服务实例 (w 参数)")
                print("     • 启用 Gzip 压缩")
                print("     • 使用 CDN")

        print("-" * 80)
        print()

        print("🎉 演示完成！\n")
        return True


def main():
    parser = argparse.ArgumentParser(description="形态匹配服务演示")
    parser.add_argument("--server-url", default="http://127.0.0.1:8000", help="服务器地址")
    parser.add_argument("--verbose", action="store_true", help="详细输出")
    args = parser.parse_args()

    demo = ShapeMatchDemo(server_url=args.server_url, verbose=args.verbose)
    success = demo.run_demo()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

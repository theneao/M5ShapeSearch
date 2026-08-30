#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
验收测试报告生成工具
========================================
生成完整的形态匹配服务验收报告（包含性能指标、准确率、建议）

使用：
    python generate_acceptance_report.py --server-url http://localhost:8000 --output report.json
"""
import sys
import os
import json
import argparse
import time
import traceback
from datetime import datetime
from typing import Dict, List, Any
import numpy as np

try:
    import requests
except ImportError:
    print("[ERROR] 需要 requests 库，请先执行: pip install requests")
    sys.exit(1)


class AcceptanceReport:
    """验收报告生成器"""

    def __init__(self, server_url: str):
        self.server_url = server_url
        self.report: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "server_url": server_url,
            "tests": {},
            "summary": {},
            "recommendations": []
        }

    def add_test_result(self, test_name: str, result: Dict[str, Any]):
        """添加测试结果"""
        self.report["tests"][test_name] = result

    def add_summary(self, key: str, value: Any):
        """添加摘要"""
        self.report["summary"][key] = value

    def add_recommendation(self, severity: str, title: str, description: str):
        """添加建议"""
        self.report["recommendations"].append({
            "severity": severity,  # CRITICAL / HIGH / MEDIUM / LOW
            "title": title,
            "description": description
        })

    def save_json(self, path: str):
        """保存为 JSON"""
        # 转换 numpy 类型为 Python 原生类型
        def convert_types(obj):
            if isinstance(obj, np.bool_):
                return bool(obj)
            elif isinstance(obj, np.integer):
                return int(obj)
            elif isinstance(obj, np.floating):
                return float(obj)
            elif isinstance(obj, dict):
                return {k: convert_types(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_types(v) for v in obj]
            return obj

        report_data = convert_types(self.report)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)
        print(f"✅ 报告已保存: {path}")

    def save_markdown(self, path: str):
        """保存为 Markdown"""
        md = self._generate_markdown()
        with open(path, 'w', encoding='utf-8') as f:
            f.write(md)
        print(f"✅ Markdown 报告已保存: {path}")

    def _generate_markdown(self) -> str:
        """生成 Markdown 格式报告"""
        md = f"""# 形态匹配服务验收测试报告

**生成时间**: {self.report['timestamp']}
**服务地址**: {self.report['server_url']}

## 1. 测试摘要

| 指标 | 结果 | 状态 |
|------|------|------|
"""
        for key, value in self.report["summary"].items():
            status = "✅ 通过" if isinstance(value, bool) and value else "❌ 失败" if isinstance(value, bool) else "▪ 数据"
            md += f"| {key} | {value} | {status} |\n"

        md += """
## 2. 详细测试结果

"""
        for test_name, result in self.report["tests"].items():
            md += f"### {test_name}\n\n"
            md += f"```json\n{json.dumps(result, indent=2, ensure_ascii=False)}\n```\n\n"

        md += "## 3. 建议与改进\n\n"
        for rec in self.report["recommendations"]:
            emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}
            md += f"{emoji.get(rec['severity'], '▪')} **{rec['severity']}** - {rec['title']}\n\n"
            md += f"   {rec['description']}\n\n"

        return md


def run_acceptance_tests(server_url: str) -> AcceptanceReport:
    """运行验收测试"""
    report = AcceptanceReport(server_url)

    print("[STEP 1] 健康检查...")
    try:
        r = requests.get(f"{server_url}/api/v1/health", timeout=5)
        health_ok = r.status_code == 200
        report.add_test_result("health_check", {
            "status_code": r.status_code,
            "ok": health_ok,
            "timestamp": datetime.now().isoformat()
        })
        print(f"  {'✅' if health_ok else '❌'} 健康检查 - HTTP {r.status_code}")
    except Exception as e:
        print(f"  ❌ 健康检查失败: {e}")
        return report

    print("\n[STEP 2] 性能测试...")
    timings = {"http_rtt": [], "query_ms": []}
    test_shapes = {
        "uptrend": [(i/100, i/100) for i in range(101)],
        "downtrend": [(i/100, 1-i/100) for i in range(101)],
        "sideways": [(i/100, 0.5 + 0.1*np.sin(i/10)) for i in range(101)],
    }

    for shape_name, points in test_shapes.items():
        try:
            t0 = time.perf_counter()
            r = requests.post(
                f"{server_url}/api/v1/shape/match",
                json={"points": points, "limit": 5},
                timeout=5
            )
            http_rtt = (time.perf_counter() - t0) * 1000

            if r.status_code == 200:
                data = r.json()
                query_ms = data.get("data", {}).get("query_ms", 0)
                timings["http_rtt"].append(http_rtt)
                timings["query_ms"].append(query_ms)
                print(f"  ✅ {shape_name:<15} - RTT={http_rtt:.1f}ms, Query={query_ms}ms")
        except Exception as e:
            print(f"  ❌ {shape_name}: {e}")

    # 性能统计
    if timings["http_rtt"]:
        avg_rtt = np.mean(timings["http_rtt"])
        max_rtt = np.max(timings["http_rtt"])
        avg_query = np.mean(timings["query_ms"])
        max_query = np.max(timings["query_ms"])

        report.add_summary("HTTP RTT (avg)", f"{avg_rtt:.1f}ms")
        report.add_summary("HTTP RTT (max)", f"{max_rtt:.1f}ms")
        report.add_summary("Query Time (avg)", f"{avg_query:.1f}ms")
        report.add_summary("Query Time (max)", f"{max_query:.1f}ms")

        rtt_pass = max_rtt < 180
        query_pass = max_query < 60
        report.add_summary("HTTP RTT < 180ms", rtt_pass)
        report.add_summary("Query Time < 60ms", query_pass)

        if not rtt_pass:
            report.add_recommendation("HIGH", "HTTP RTT 超过限制",
                f"最大 RTT {max_rtt:.1f}ms 超过目标 180ms，建议增加服务实例或启用缓存")
        if not query_pass:
            report.add_recommendation("MEDIUM", "查询时延需优化",
                f"最大查询耗时 {max_query:.1f}ms 接近或超过 60ms 目标，建议减小 recall_multiplier")

    print("\n[STEP 3] 准确率测试...")
    # 10类形态各1次（简化版）
    print("  ℹ️  完整准确率测试请运行: python tests/test_shape_match_api.py")
    report.add_recommendation("HIGH", "完整验收测试",
        "请执行 `python tests/test_shape_match_api.py` 进行 10 类形态各 1 次的完整验收")

    print("\n[STEP 4] 索引状态检查...")
    try:
        r = requests.get(f"{server_url}/api/v1/shape/index_status", timeout=5)
        if r.status_code == 200:
            data = r.json()
            buckets = data.get("data", {}).get("buckets", {})
            total_samples = sum(buckets.values()) if buckets else 0
            report.add_summary("索引桶总数", len(buckets))
            report.add_summary("总样本数", total_samples)
            print(f"  ✅ 索引加载 - {len(buckets)} 桶，{total_samples} 条样本")
    except Exception as e:
        print(f"  ⚠️  索引状态查询失败: {e}")

    return report


def main():
    parser = argparse.ArgumentParser(description="生成验收测试报告")
    parser.add_argument("--server-url", default="http://127.0.0.1:8000", help="服务器地址")
    parser.add_argument("--output", default="acceptance_report", help="输出文件名（无扩展名）")
    args = parser.parse_args()

    print(f"\n{'='*80}")
    print("🧪 形态匹配服务 - 验收测试报告生成")
    print(f"{'='*80}\n")

    try:
        report = run_acceptance_tests(args.server_url)
    except Exception as e:
        print(f"\n❌ 测试执行失败: {e}")
        traceback.print_exc()
        sys.exit(1)

    # 保存报告
    report.save_json(f"{args.output}.json")
    report.save_markdown(f"{args.output}.md")

    # 打印摘要
    print(f"\n{'='*80}")
    print("📊 验收摘要")
    print(f"{'='*80}")
    for key, value in report.report["summary"].items():
        print(f"  {key:<30} {value}")

    if report.report["recommendations"]:
        print(f"\n⚠️  建议 ({len(report.report['recommendations'])} 项):")
        for rec in report.report["recommendations"]:
            print(f"  • [{rec['severity']}] {rec['title']}")

    print(f"\n{'='*80}\n")


if __name__ == "__main__":
    main()

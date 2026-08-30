# -*- coding: utf-8 -*-
"""
CPU 形态搜索离线 CLI。

创建时间：2026-08-28
作用：从 JSON 读取手绘点，在持久化连续序列库中搜索并输出可复现的各子分、边界和 warping path。
使用方式：python -m shape_search.cli --query query.json --data-dir ./data/cpu_shape_search --top-k 20

修改时间：2026-08-28
修改作用：离线检索周期增加 1w，与 FastAPI、手表和 Web 页面保持一致。

修改时间：2026-08-29
修改作用：离线检索周期增加 5m/15m/30m/60m，并保留 1h 兼容输入。
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Any

from core.cpu_shape_search_manager import CpuShapeSearchManager
from .config import CpuSearchConfig


def _load_query(path: Path):
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("points", payload.get("query"))
    if not isinstance(payload, list):
        raise ValueError("query JSON must be a point list or an object containing 'points'")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="CPU-only sketch shape search")
    parser.add_argument("--query", required=True, help="UTF-8 JSON sketch point file")
    parser.add_argument("--data-dir", default="./data/cpu_shape_search")
    parser.add_argument("--category", default="all", choices=["all", "stock", "crypto", "futures"])
    parser.add_argument(
        "--timeframe", default="1d",
        choices=["5m", "15m", "30m", "60m", "4h", "1d", "1w", "1h"],
    )
    parser.add_argument("--min-length", type=int, default=24)
    parser.add_argument("--max-length", type=int, default=300)
    parser.add_argument("--scale-ratio", type=float, default=1.30)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--output", help="optional UTF-8 JSON output path")
    args = parser.parse_args()

    config = CpuSearchConfig(
        min_length=args.min_length,
        max_length=args.max_length,
        scale_ratio=args.scale_ratio,
    ).validate()
    manager = CpuShapeSearchManager(args.data_dir, config=config)
    loaded = manager.load_all(require_exist=True)
    points = _load_query(Path(args.query))
    timeframe = "60m" if args.timeframe == "1h" else args.timeframe
    results, recalled = manager.search(
        category=args.category,
        timeframe=timeframe,
        top_k=max(1, args.top_k),
        query_points=points,
    )
    output = {
        "algorithm": "cpu_multiscale_shapedtw_v0.1",
        "loaded_buckets": loaded,
        "recalled_candidates": recalled,
        "diagnostics": manager.last_diagnostics,
        "results": results,
    }
    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

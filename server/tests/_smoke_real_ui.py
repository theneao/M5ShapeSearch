# -*- coding: utf-8 -*-
"""新的真实数据风格参数面板的冒烟测试
1) build_index 调用新的 build_dataset_from_symbol_pool(n_symbols=50 × 3品类) → 总样本 ≈ 4.1 万量级
2) 模拟 sketchpad 直线上升 → Top1=直线上升 + Top10 同形态 ≥ 4
"""
import os, sys, math, time, tempfile, shutil
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# fake gr.Progress so build_index works w/o Gradio
class _FakeProgress:
    def __call__(self, x, desc=""): pass

import visual_match_demo as V


def draw_shape(size=(800, 400), margin=40, f=None):
    img = Image.new("RGBA", size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    if f is None:
        t = np.linspace(0, 1, 160)
        ys = 0.9 * t + 0.05
    else:
        ys = f()
    W, H = size
    xs = np.linspace(0, 1, len(ys))
    pts = [(margin + x * (W - 2 * margin),
            margin + (1 - y) * (H - 2 * margin)) for x, y in zip(xs, ys)]
    draw.line(pts, fill=(31, 119, 180, 255), width=3)
    return img


def draw_downtrend():
    t = np.linspace(0, 1, 160); return 0.95 - 0.9 * t


if __name__ == "__main__":
    tmpdir = Path(tempfile.mkdtemp())
    old_tmpdir = V._TMPDIR
    V._TMPDIR = tmpdir
    try:
        print("[T1] build_index: n_symbols=5, window=60, step=10, MOCK × 3品类 × 1d")
        status, bt, sk = V.build_index(
            5, 60, 10, "MOCK", True, True, True, "1d", 42, progress=_FakeProgress())
        assert "[OK]" in status, status
        for ln in status.splitlines()[:6]:
            print("  ", ln)
        mgr = V._MGR
        bs = mgr.buckets_status()
        total = sum(bs.values())
        print(f"   9桶总计形态数：{total} （按 N=5×3≈15标的 × ~274窗 ≈ 4,110 目标）")
        assert total >= 1000, f"MOCK N=5 总形态太少：{total}"

        print("[T2] 模拟 sketchpad 直线上升 → Top1=直线上升，Top10 同形态 ≥ 4")
        img = draw_shape()
        df, dfq, tbl, st = V.do_match(img, "1d", True, True, True, True, 10)
        assert df is not None and tbl is not None
        for ln in st.splitlines():
            print("  ", ln)
        print("   Top5:")
        for r in tbl[:5]:
            print("    ", r)
        top1 = tbl[0][1]
        same10 = sum(1 for r in tbl if r[1] == "直线上升")
        print(f"   Top1={top1}; Top10直线上升={same10}/10")
        assert top1 == "直线上升", top1
        assert same10 >= 4, same10

        print("[T3] 模拟 sketchpad 直线下降 → Top1=直线下降")
        img2 = draw_shape(f=draw_downtrend)
        _, _, tbl2, _ = V.do_match(img2, "1d", True, True, True, True, 10)
        top1_2 = tbl2[0][1]
        print("   Top1 下降:", top1_2)
        for r in tbl2[:3]: print("    ", r)
        assert top1_2 == "直线下降", top1_2

        print("\n[ALL TESTS PASSED] ✅")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        V._TMPDIR = old_tmpdir

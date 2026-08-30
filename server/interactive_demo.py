#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
形态匹配服务 - 交互式 Gradio 演示 UI
========================================
完整的可视化演示界面，支持：
1. 左侧参数面板：选择数据源、品类、时间框架、构建索引
2. 中间手绘画板：用户手绘形态，自动触发匹配
3. 右侧结果区：Top10 线图对比、结果表格、耗时统计

启动方式：
    python interactive_demo.py

浏览器自动打开：http://127.0.0.1:7860
"""
import os
import sys
import time
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Tuple, List

import numpy as np
import gradio as gr
from PIL import Image, ImageDraw

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import (
    FaissIndexManager,
    full_pipeline,
    SHAPE_CN_NAMES,
    VECTOR_DIM,
    build_dataset_from_symbol_pool,
)

# ============================================================
# 全局状态
# ============================================================
_MGR: Optional[FaissIndexManager] = None
_LAST_DATA_SOURCE: str = "MOCK"
_TMPDIR: Path = Path(tempfile.mkdtemp(prefix="interactive_demo_", dir=str(Path(__file__).parent / "tests" / "mock")))

print(f"[INFO] 临时目录: {_TMPDIR}")


# ============================================================
# 工具函数：图像转点
# ============================================================
def _sketch_to_points(
    sketch_img: Optional[Image.Image],
    y_flip: bool = True,
    min_points: int = 20
) -> List[List[float]]:
    """将 Gradio 手绘画板 PIL 图像转换为点序列 (x,y) ∈ [0,1]×[0,1]"""
    if sketch_img is None:
        return []

    arr = np.array(sketch_img.convert("RGBA"))
    W, H = arr.shape[1], arr.shape[0]
    alpha = arr[..., 3]
    mask = alpha > 30

    if not mask.any():
        return []

    # 提取笔迹点
    ys, xs = np.where(mask)
    order = np.argsort(xs)
    xs = xs[order].astype(np.float64)
    ys = ys[order].astype(np.float64)

    # 按 x 分箱去重（避免垂直线重复）
    bins = np.linspace(xs.min(), xs.max(), 90)
    bin_ids = np.digitize(xs, bins[1:-1])
    uniq_x, uniq_y = [], []
    for b in np.unique(bin_ids):
        sel = bin_ids == b
        uniq_x.append(xs[sel].mean())
        uniq_y.append(np.median(ys[sel]))

    xn = np.array(uniq_x)
    yn = np.array(uniq_y)

    # 不足 min_points 则线性插值补充
    if len(xn) < min_points:
        t = np.linspace(0, 1, min_points)
        tt = np.linspace(0, 1, len(xn)) if len(xn) >= 2 else np.array([0.0, 1.0])
        if len(xn) >= 2:
            xn = np.interp(t, tt, xn)
            yn = np.interp(t, tt, yn)
        else:
            xn = np.linspace(0, 1, min_points)
            yn = np.full(min_points, 0.5)

    # 归一化到 [0,1]
    xn = (xn - xn.min()) / max(xn.max() - xn.min(), 1e-6)
    yn = (yn - yn.min()) / max(yn.max() - yn.min(), 1e-6)

    if y_flip:
        yn = 1.0 - yn

    pts = np.stack([xn, yn], axis=1)
    return pts.tolist()


# ============================================================
# 核心函数 1：构建数据集和索引
# ============================================================
def build_index(
    n_symbols: int,
    window_days: int,
    step_days: int,
    data_source: str,
    cat_crypto: bool,
    cat_stock: bool,
    cat_futures: bool,
    timeframe: str,
    seed: int,
    progress=gr.Progress()
) -> Tuple[str, List, gr.update]:
    """构建数据集和 FAISS 索引"""
    global _MGR, _LAST_DATA_SOURCE

    # 选择品类
    cats = []
    if cat_crypto:
        cats.append("crypto")
    if cat_stock:
        cats.append("stock")
    if cat_futures:
        cats.append("futures")

    if not cats:
        return "[ERROR] 请至少勾选一个品类", None, gr.update(interactive=False)

    cats_tup = tuple(cats)

    # 清空旧索引
    d = _TMPDIR / "faiss_demo"
    shutil.rmtree(d, ignore_errors=True)

    # 进度 1: 生成/采集数据
    progress(0.1, desc="🔄 正在生成/采集标的样本...")
    t0 = time.perf_counter()

    try:
        samples = build_dataset_from_symbol_pool(
            n_symbols=int(n_symbols),
            categories=cats_tup,
            timeframe=timeframe,
            window=int(window_days),
            step=int(step_days),
            data_source=data_source,
            seed=int(seed),
        )
    except Exception as e:
        return f"[ERROR] 数据构建失败: {e}", None, gr.update(interactive=False)

    t_gen = time.perf_counter() - t0

    # 进度 2: 构建索引
    progress(0.6, desc=f"🔄 已生成 {len(samples)} 条样本，正在构建 FAISS 索引...")
    t1 = time.perf_counter()

    try:
        mgr = FaissIndexManager(data_dir=str(d))
        total = mgr.build_from_samples(samples, timeframe=timeframe)
        _MGR = mgr
        _LAST_DATA_SOURCE = data_source
    except Exception as e:
        return f"[ERROR] 索引构建失败: {e}", None, gr.update(interactive=False)

    t_idx = time.perf_counter() - t1

    # 进度完成
    progress(1.0, desc="✅ 完成！")

    # 生成状态报告
    data_source_label = {
        "MOCK": "MOCK（数学曲线，等价于真实数据规模）",
        "AKSHARE": "AKShare（真实 A股/期货/加密货币，无需 Key）",
        "BINANCE": "Binance（真实加密货币 K 线）",
        "TUSHARE": "Tushare（真实 A 股日线，需 Token）",
    }.get(data_source, data_source)

    approx_symbols = n_symbols * len(cats)

    status = f"""✅ **数据集构建完成！**

**数据源**: {data_source_label}
**标的池**: {n_symbols} × {len(cats)}品类 ≈ {approx_symbols} 只
**滑窗**: {window_days}根K线/窗口, 步长{step_days} → {len(samples)}条形态
**时间框架**: {timeframe}
**构建耗时**: 采集{t_gen:.1f}s + 索引{t_idx:.2f}s = {t_gen+t_idx:.1f}s
**索引桶**: {mgr.buckets_status()}
**向量维度**: {VECTOR_DIM}D

👉 **现在可以在右侧画板手绘形态，松手后立即匹配 Top10！**
"""

    buckets_table = [
        [b, mgr.buckets_status().get(b, 0)]
        for b in sorted(mgr.buckets_status().keys())
    ]

    return status, buckets_table, gr.update(interactive=True)


# ============================================================
# 核心函数 2：手绘 → 匹配
# ============================================================
def do_match(
    sketch_img: Optional[Image.Image],
    timeframe: str,
    cat_all: bool,
    cat_crypto: bool,
    cat_stock: bool,
    cat_futures: bool,
    top_k: int,
) -> Tuple[Optional[dict], Optional[dict], Optional[List], str]:
    """匹配手绘形态"""
    global _MGR

    if _MGR is None:
        return None, None, "❌ 请先点击【🔨 构建数据】完成准备"

    # 图像转点
    pts = _sketch_to_points(sketch_img, y_flip=True)
    if len(pts) < 10:
        return None, None, "⚠️ 画板为空或点数太少，请用鼠标在画板上画出一条曲线"

    # 特征提取
    try:
        seq_128, stats, vec = full_pipeline(pts, y_flip=False)
    except Exception as e:
        return None, None, f"❌ 特征提取失败: {e}"

    # 选择品类
    if cat_all:
        cat_q = "all"
    else:
        chosen = []
        if cat_crypto:
            chosen.append("crypto")
        if cat_stock:
            chosen.append("stock")
        if cat_futures:
            chosen.append("futures")
        if not chosen:
            chosen = ["crypto", "stock", "futures"]
        cat_q = chosen[0] if len(chosen) == 1 else "all"

    # 搜索
    t0 = time.perf_counter()
    res, total_cand = _MGR.search(
        vec,
        category=cat_q,
        timeframe=timeframe,
        top_k=int(top_k),
        query_stats=stats,
        query_seq_128=seq_128,
    )
    ms = (time.perf_counter() - t0) * 1000

    if not res:
        return None, None, "⚠️ 没有匹配结果（可能时间框架/品类没数据）"

    # 构建结果表格
    tbl_rows = []
    rows_plot = []

    from collections import Counter
    label_counter = Counter()

    for i, r in enumerate(res[:top_k]):
        sc = float(r.get("match_score", r.get("_raw_ip", 0.0)))
        st = r.get("shape_type", "?")
        label_counter[st] += 1

        # 获取 128 点序列用于绘图
        b = r.get("_matched_bucket")
        ys_128 = None
        try:
            # 尝试从缓存获取
            for idx_cand, sq in _MGR._seq_cache.get(b, {}).items():
                meta = _MGR._fetch_meta(b, idx_cand)
                if meta and meta.get("sample_id") == r.get("sample_id"):
                    ys_128 = sq[:, 1].astype(float).tolist()
                    break
        except Exception:
            pass

        if ys_128 is None:
            ys_128 = [0.5] * 128

        # 添加到绘图数据
        label = f"Top{i+1}: {SHAPE_CN_NAMES.get(st, st)}"
        for j, y in enumerate(ys_128):
            rows_plot.append({"index": j, "value": y, "type": label})

        # 添加到结果表
        tbl_rows.append([
            i + 1,
            SHAPE_CN_NAMES.get(st, st),
            f"{sc * 100:.1f}%",
            r.get("category", "?"),
            r.get("symbol", "?"),
            f"{r.get('change_pct', 0.0):+.2f}%",
        ])

    # 构建查询曲线绘图数据
    qy = seq_128[:, 1].astype(float).tolist()
    rows_plot_query = []
    for j, y in enumerate(qy):
        rows_plot_query.append({"index": j, "value": y, "type": "你的手绘"})

    # 生成状态信息
    sc1 = float(res[0].get("match_score", res[0].get("_raw_ip", 0.0)))
    st1 = SHAPE_CN_NAMES.get(res[0].get("shape_type", "?"), res[0].get("shape_type", "?"))
    vote_winner, vote_count = label_counter.most_common(1)[0] if label_counter else ("?", 0)
    vote_cn = SHAPE_CN_NAMES.get(vote_winner, vote_winner)

    source_note = (
        "（真实数据下仅为弱分类标注）"
        if _LAST_DATA_SOURCE != "MOCK"
        else "（MOCK 数据 10 类真值标注）"
    )

    status = f"""✅ **匹配完成！** 耗时 {ms:.1f}ms

**Top1 最相似**: 【{st1}】 置信 {sc1*100:.1f}%
**Top{top_k} 投票**: 【{vote_cn}】 获得 {vote_count}/{top_k} 票

**查询特征**:
  • 峰值数: {stats.peak_count}
  • 谷值数: {stats.valley_count}
  • 趋势斜率: {stats.trend_slope:+.4f}
  • 位移: {stats.total_displacement:+.4f}

**搜索范围**: {cat_q} × {timeframe} × Top{top_k}
**候选池**: 粗召回 {total_cand} 条 → 硬过滤 → DTW 精排

**说明**: {source_note}
"""

    # 返回 Plotly 格式的图表数据
    plot_data_top = {
        "data": [
            {
                "x": list(range(128)),
                "y": qy,
                "type": "scatter",
                "name": "你的手绘",
                "line": {"color": "#FF6B6B", "width": 2},
            }
        ] + [
            {
                "x": list(range(128)),
                "y": [row["value"] for row in rows_plot if row["type"] == label][:128],
                "type": "scatter",
                "name": label,
            }
            for label in [r["type"] for r in set((r["type"] for r in rows_plot))]
        ],
        "layout": {
            "title": "手绘形态 vs 匹配候选（Top10 对比）",
            "xaxis": {"title": "时间点索引 (0-127)"},
            "yaxis": {"title": "归一化价格 (0-1)"},
            "hovermode": "x unified",
        }
    }

    return plot_data_top, tbl_rows, status


# ============================================================
# Gradio 应用主程序
# ============================================================
def create_app():
    """创建 Gradio 应用"""

    with gr.Blocks(title="形态匹配演示") as app:

        # ============ 标题 ============
        gr.Markdown("""
# 🎯 M5StopWatch 形态匹配云服务 - 交互式演示

**完整工作流程**：
1. **左侧参数面板** → 选择数据源、品类、时间框架
2. **点击【🔨 构建数据】** → 生成数据集并构建 FAISS 索引
3. **右侧手绘画板** → 用鼠标/笔画出任意形态曲线
4. **松手后自动匹配** → 展示 Top10 最相似的形态，显示匹配度和对比图

---
        """)

        with gr.Row():
            # ============ 左侧：参数面板 ============
            with gr.Column(scale=1):
                gr.Markdown("### ⚙️ 参数设置")

                # 数据源选择
                data_source = gr.Radio(
                    choices=["MOCK", "AKSHARE", "BINANCE", "TUSHARE"],
                    value="AKSHARE",
                    label="📊 数据源",
                    info="MOCK: 数学曲线 | AKSHARE: 真实多资产(无需Key) | BINANCE: 真实币价 | TUSHARE: 真实股价(需Token)"
                )

                # 标的池规模
                n_symbols = gr.Slider(
                    minimum=1,
                    maximum=100,
                    value=10,
                    step=1,
                    label="🏢 标的池规模",
                    info="每品类标的数量"
                )

                # 滑窗参数
                window_days = gr.Slider(
                    minimum=10,
                    maximum=120,
                    value=60,
                    step=10,
                    label="📈 K线窗口",
                    info="每条形态的K线根数"
                )

                step_days = gr.Slider(
                    minimum=1,
                    maximum=30,
                    value=10,
                    step=1,
                    label="📊 步长",
                    info="滑窗步进"
                )

                # 品类选择
                gr.Markdown("**🎲 品类**")
                cat_crypto = gr.Checkbox(value=True, label="加密货币 (Crypto)")
                cat_stock = gr.Checkbox(value=True, label="股票 (Stock)")
                cat_futures = gr.Checkbox(value=False, label="期货 (Futures)")

                # 时间框架
                timeframe = gr.Radio(
                    choices=["1h", "4h", "1d"],
                    value="1d",
                    label="⏱️ 时间框架",
                )

                seed = gr.Slider(
                    minimum=0,
                    maximum=9999,
                    value=42,
                    step=1,
                    label="🎲 随机种子 (仅 MOCK 数据用)",
                    info="MOCK 数据源时保证可重复性；真实数据源时忽略此参数",
                    visible=True,  # 默认可见
                )

                # 构建按钮
                build_btn = gr.Button(
                    "🔨 构建数据",
                    variant="primary",
                    size="lg"
                )

                # 构建结果
                build_status = gr.Markdown("")
                buckets_table = gr.Dataframe(
                    headers=["时间桶", "样本数"],
                    label="索引桶状态",
                )

            # ============ 中间：手绘画板 ============
            with gr.Column(scale=1):
                gr.Markdown("### ✍️ 手绘形态")
                gr.Markdown("用鼠标/笔在下方画板上绘制任意形态曲线，松手后自动匹配 Top10")

                sketch = gr.Sketchpad(
                    label="手绘画板",
                    scale=1,
                    show_label=False,
                    sources=[],
                    height=400,
                )

                # 清空按钮
                clear_btn = gr.Button("🗑️ 清空画板", size="sm")
                clear_btn.click(lambda: None, outputs=sketch)

            # ============ 右侧：结果区 ============
            with gr.Column(scale=1):
                gr.Markdown("### 📊 匹配结果")

                # 筛选选项
                with gr.Row():
                    cat_all = gr.Checkbox(value=True, label="全品类", scale=1)
                    top_k = gr.Slider(
                        minimum=1,
                        maximum=50,
                        value=10,
                        step=1,
                        label="Top K",
                        scale=1,
                    )

                # 结果图表
                plot = gr.Plot(label="形态对比图")

                # 结果表格
                result_table = gr.Dataframe(
                    headers=["排名", "形态名", "匹配度", "品类", "符号", "涨跌%"],
                    label="Top10 匹配结果",
                    interactive=False,
                )

                # 状态信息
                match_status = gr.Markdown("")

        # ============ 事件绑定 ============

        # 构建数据按钮
        build_btn.click(
            fn=build_index,
            inputs=[
                n_symbols, window_days, step_days, data_source,
                cat_crypto, cat_stock, cat_futures, timeframe, seed
            ],
            outputs=[build_status, buckets_table, sketch],
        )

        # 手绘完成自动匹配
        sketch.change(
            fn=do_match,
            inputs=[sketch, timeframe, cat_all, cat_crypto, cat_stock, cat_futures, top_k],
            outputs=[plot, result_table, match_status],
        )

        return app


if __name__ == "__main__":
    import sys
    import io
    import socket

    # 修复 Windows 编码
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    # 找到空闲端口
    def find_free_port(start_port=7860, max_attempts=10):
        for port in range(start_port, start_port + max_attempts):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(('127.0.0.1', port))
                sock.close()
                return port
            except OSError:
                continue
        return start_port

    port = find_free_port()

    print("\n" + "="*80)
    print("[INFO] Starting interactive demo UI")
    print("="*80)
    print(f"\n[INFO] Browser URL: http://127.0.0.1:{port}\n")

    app = create_app()
    app.launch(
        server_name="127.0.0.1",
        server_port=port,
        share=False,
        show_error=True,
    )

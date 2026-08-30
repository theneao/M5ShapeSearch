# -*- coding: utf-8 -*-
"""可视化形态匹配 Demo（Gradio 单文件）
页面三栏：
    1) 左侧参数面板 → 数据量/品类/时间框架 + 构建按钮
    2) 中间手绘画板 → 用户绘制后自动触发匹配
    3) 右侧结果区 → Top10 线形图 + 结果表格 + 耗时
启动：
    D:\anaconda3\envs\shape-server\python.exe visual_match_demo.py
"""
import os
import sys
import time
import shutil
import tempfile
from pathlib import Path

import numpy as np
import gradio as gr
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import (
    FaissIndexManager, full_pipeline, SHAPE_CN_NAMES, VECTOR_DIM,
)

# ---------------------------------------------------------------------
# 全局状态（Gradio demo 内共享）
# ---------------------------------------------------------------------
_MGR: FaissIndexManager | None = None
_LAST_DATA_SOURCE: str = "MOCK"
_TMPDIR: Path = Path(tempfile.mkdtemp(prefix="vis_demo_", dir=str(Path(__file__).parent / "tests" / "mock")))

# 快速识别：将 PIL 画板 → (N,2) 点序列（x ∈ [0,1], y ∈ [0,1]）
def _sketch_to_points(sketch_img: Image.Image | None,
                       y_flip: bool = True,
                       min_points: int = 20) -> list[list[float]]:
    if sketch_img is None:
        return []
    arr = np.array(sketch_img.convert("RGBA"))
    W, H = arr.shape[1], arr.shape[0]
    alpha = arr[..., 3]
    mask = alpha > 30
    if not mask.any():
        return []
    ys, xs = np.where(mask)
    order = np.argsort(xs)
    xs = xs[order].astype(np.float64)
    ys = ys[order].astype(np.float64)
    # 去重（按 x 分箱，每箱取 y 中值，避免垂直叠线）
    bins = np.linspace(xs.min(), xs.max(), 90)
    bin_ids = np.digitize(xs, bins[1:-1])
    uniq_x = []
    uniq_y = []
    for b in np.unique(bin_ids):
        sel = bin_ids == b
        uniq_x.append(xs[sel].mean())
        uniq_y.append(np.median(ys[sel]))
    xn = np.array(uniq_x); yn = np.array(uniq_y)
    if len(xn) < min_points:
        # 不足则线性插值
        t = np.linspace(0, 1, min_points)
        tt = np.linspace(0, 1, len(xn)) if len(xn) >= 2 else np.array([0.0, 1.0])
        xn = np.interp(t, tt, np.interp(tt, tt, xn)) if len(xn) >= 2 else np.linspace(0, 1, min_points)
        yn = np.interp(t, tt, np.interp(tt, tt, yn)) if len(yn) >= 2 else np.full(min_points, 0.5)
    xn = (xn - xn.min()) / max(xn.max() - xn.min(), 1e-6)
    yn = (yn - yn.min()) / max(yn.max() - yn.min(), 1e-6)
    if y_flip:
        yn = 1.0 - yn
    pts = np.stack([xn, yn], axis=1)
    return pts.tolist()


# ---------------------------------------------------------------------
# 动作 1：构建数据（只暴露"标的池数量 N"，不再有"每形态干净样本数 / 扰动倍数"）
# ---------------------------------------------------------------------
def build_index(n_symbols: int, window_days: int, step_days: int, data_source: str,
                cat_crypto: bool, cat_stock: bool, cat_futures: bool,
                timeframe: str, seed: int, progress=gr.Progress()):
    global _MGR, _LAST_DATA_SOURCE
    cats = []
    if cat_crypto: cats.append("crypto")
    if cat_stock:  cats.append("stock")
    if cat_futures:cats.append("futures")
    if not cats:
        return "[ERR] 请至少勾选一个品类（加密货币 / 股票 / 期货）", None, None
    cats_tup = tuple(cats)
    d = _TMPDIR / "faiss_demo"
    shutil.rmtree(d, ignore_errors=True)
    progress(0.05, desc="正在采集 / 生成标的样本...")
    t0 = time.perf_counter()

    from core import build_dataset_from_symbol_pool
    samples = build_dataset_from_symbol_pool(
        n_symbols=int(n_symbols),
        categories=cats_tup,
        timeframe=timeframe,
        window=int(window_days),
        step=int(step_days),
        data_source=data_source,
        seed=int(seed),
        verbose=True,
    )

    t_gen = time.perf_counter() - t0
    progress(0.55, desc=f"生成 {len(samples)} 条滑窗样本 OK。正在构建 FAISS 索引...")
    t1 = time.perf_counter()
    mgr = FaissIndexManager(data_dir=str(d))
    total = mgr.build_from_samples(samples, timeframe=timeframe)
    t_idx = time.perf_counter() - t1
    _MGR = mgr
    _LAST_DATA_SOURCE = data_source
    data_source_label = {
        "MOCK": "MOCK（基于真实标的池规模的等价数学曲线，生产未接真实数据前用）",
        "AKSHARE": "AKShare（真实 A股/期货/加密货币，无需 API Key）",
        "BINANCE": "BINANCE Public API（真实加密货币 K 线，无需 API Key）",
        "TUSHARE": "TUSHARE Pro（真实 A 股日线，需环境变量 TUSHARE_TOKEN）",
    }.get(data_source, data_source)
    approx_symbols = n_symbols * len(cats)
    status = (
        f"[OK] 数据集构建完成！\n"
        f"  • 数据源：**{data_source_label}**\n"
        f"  • 标的池规模：{n_symbols} 只 / 品类  ×  {len(cats)} 品类（估算 ≈ {approx_symbols} 只标的）\n"
        f"  • 滑窗：{window_days} 根K线 / 窗口，步长 {step_days} 根K线  →  总形态：{len(samples)} 条\n"
        f"  • 时间框架：{timeframe}（FAISS 9 桶分桶 key）\n"
        f"  • 生成 / 采集耗时：{t_gen:.1f}s　|　构建索引：{t_idx:.2f}s　|　合计 {t_gen+t_idx:.1f}s\n"
        f"  • 9 桶大小：{mgr.buckets_status()}\n"
        f"  • 向量维度：{VECTOR_DIM}D（303v3，已 L2 归一化）\n"
        f"\n👉 现在可在中间画板 手绘形态 → 松手立即匹配 Top10"
    )
    buckets_table = [
        [b, mgr.buckets_status().get(b, 0)] for b in sorted(mgr.buckets_status().keys())
    ]
    return status, buckets_table, gr.update(interactive=True)


# ---------------------------------------------------------------------
# 动作 2：手绘 → 匹配 Top10
# ---------------------------------------------------------------------
def do_match(sketch_img: Image.Image | None, timeframe: str, cat_all: bool,
             cat_crypto: bool, cat_stock: bool, cat_futures: bool, top_k: int):
    global _MGR
    if _MGR is None:
        return (
            None, None, None,
            "[ERR] 请先点击【开始构建数据】完成数据集准备",
        )
    pts = _sketch_to_points(sketch_img, y_flip=True)
    if len(pts) < 10:
        return None, None, None, "[WARN] 画板为空或点数太少，请用鼠标/笔在画板上画出一条曲线"

    try:
        seq_128, stats, vec = full_pipeline(pts, y_flip=False)
    except Exception as e:
        return None, None, None, f"特征提取失败：{e!r}"

    if cat_all:
        cat_q = "all"
    else:
        chosen = []
        if cat_crypto: chosen.append("crypto")
        if cat_stock: chosen.append("stock")
        if cat_futures: chosen.append("futures")
        if not chosen: chosen = ["crypto", "stock", "futures"]
        cat_q = chosen[0] if len(chosen) == 1 else "all"

    t0 = time.perf_counter()
    res, total_cand = _MGR.search(
        vec, category=cat_q, timeframe=timeframe, top_k=top_k,
        query_stats=stats, query_seq_128=seq_128,
    )
    ms = (time.perf_counter() - t0) * 1000
    if not res:
        return None, None, None, "[WARN] 候选为空（可能时间框架 / 品类没匹配上）"

    import pandas as pd
    from collections import Counter
    tbl_rows = []
    rows_plot = []
    # TopK 弱标签投票（注意：真实数据 shape_type 是"最接近 10 类之一"的弱分类标注，非真值）
    label_counter = Counter()
    for i, r in enumerate(res[:top_k]):
        sc = float(r.get("match_score", r.get("_raw_ip", 0.0)))
        st = r.get("shape_type", "?")
        label_counter[st] += 1
        b = r.get("_matched_bucket")
        ys_128 = None
        fid = None
        sid = r.get("sample_id")
        if b and sid is not None:
            for idx_cand, sq in _MGR._seq_cache.get(b, {}).items():
                meta = _MGR._fetch_meta(b, idx_cand)
                if meta and meta.get("sample_id") == sid:
                    fid = idx_cand
                    ys_128 = sq[:, 1].astype(float).tolist()
                    break
        if ys_128 is None and b and fid is not None:
            try:
                with _MGR._conn() as c:
                    row = c.execute(
                        "SELECT sample_json FROM shape_samples WHERE bucket=? AND faiss_id=?",
                        (b, fid)).fetchone()
                    if row:
                        raw = __import__("json").loads(row["sample_json"]).get("raw_points")
                        if raw:
                            s128, _, _ = full_pipeline(raw, y_flip=False)
                            ys_128 = s128[:, 1].astype(float).tolist()
            except Exception:
                pass
        if ys_128 is None:
            ys_128 = [0.5] * 128
        label = f"#{i+1} {SHAPE_CN_NAMES.get(st, st)}"
        for j, y in enumerate(ys_128):
            rows_plot.append({"x": j, "y": y, "候选": label})
        tbl_rows.append([
            i + 1,
            SHAPE_CN_NAMES.get(st, st),
            f"{sc * 100:.1f}%",
            f"{r.get('category','?')} / {r.get('symbol','?')}",
            r.get("symbol_name", ""),
            f"{r.get('change_pct', 0.0):+.2f}%",
        ])
    df = pd.DataFrame(rows_plot)
    qy = seq_128[:, 1].astype(float).tolist()
    df_q = pd.DataFrame({"x": list(range(128)), "y": qy, "序列": ["你手绘的形态"] * 128})

    sc1 = float(res[0].get("match_score", res[0].get("_raw_ip", 0.0)))
    st1 = SHAPE_CN_NAMES.get(res[0].get("shape_type", "?"), res[0].get("shape_type", "?"))
    # TopK 投票：如果多个候选形态一致，给出"投票形态"更直观
    vote_winner, vote_count = label_counter.most_common(1)[0]
    vote_cn = SHAPE_CN_NAMES.get(vote_winner, vote_winner)
    source_note = (
        "弱分类标注（按 peak/valley/trend 最接近的 10 类之一，非真值打标）"
        if _LAST_DATA_SOURCE and _LAST_DATA_SOURCE != "MOCK"
        else "MOCK 数学形态打标（10 类真值）"
    )
    status = (
        f"[OK] 匹配完成！耗时 {ms:.1f}ms（FAISS粗召回候选 {total_cand} → 硬过滤 → 32D-DTW精排）\n"
        f"  • Top1 最相似候选形态：【{st1}】 置信 {sc1*100:.1f}%  |  Top{top_k} 投票最多形态：【{vote_cn}】 {vote_count}/{top_k}\n"
        f"  • 形态名解释：{source_note}\n"
        f"  • 查询向量特征：峰值 {stats.peak_count}，谷值 {stats.valley_count}，趋势斜率 {stats.trend_slope:+.4f}\n"
        f"  • 搜索范围：{cat_q}  ×  {timeframe}  ×  Top{top_k}"
    )
    return df, df_q, tbl_rows, status


# ---------------------------------------------------------------------
# Gradio 页面
# ---------------------------------------------------------------------
def main():
    css = """
    #sketch-col .gradio-sketchpad { min-height: 420px !important; }
    .small-hint { color: #888; font-size: 12px; }
    """
    with gr.Blocks(title="形态匹配可视化测试台") as demo:
        gr.Markdown(
            """
            # 🖊️ 形态匹配可视化测试台
            **三步搞定**：① 左栏设置参数 → 点【开始构建数据】　② 中间画板 手绘一条曲线（建议 60~180 交易日跨度形态）
            　③ 右栏自动展示 **Top10 叠图 + 表格**，观察同形态命中率 & 耗时
            """
        )
        with gr.Row():
            # ---------------- 左栏：参数面板 ----------------
            with gr.Column(scale=3, min_width=380):
                gr.Markdown("### ⚙️ 数据采集 & 索引构建参数（已移除「预制形态」概念）")
                with gr.Row():
                    n_symbols = gr.Slider(minimum=3, maximum=2000, value=50, step=1,
                                           label="标的池数量（每品类 N 只）",
                                           info="例：N=50 × 3品类 ≈ 150只标的 ≈ 50×274×3 ≈ 4.1万条形态窗口")
                    window_days = gr.Slider(minimum=20, maximum=180, value=60, step=5,
                                            label="形态滑窗长度（每形态取 N 根K线）",
                                            info="设备端手绘默认 ≈ 60 根交易日 ≈ 3 个月")
                with gr.Row():
                    step_days = gr.Slider(minimum=1, maximum=30, value=10, step=1,
                                          label="滑窗步长（每隔 N 根K线 切一个窗）",
                                          info="步长=1 最全≈274窗；步长=10 推荐≈27窗/标的")
                    top_k = gr.Slider(minimum=5, maximum=20, value=10, step=1, label="匹配 TopK",
                                      info="展示 & 用于 DTW 精排的候选数")
                with gr.Row():
                    seed = gr.Number(value=20260820, label="随机种子 (MOCK)", precision=0,
                                     info="仅 MOCK 数据源生效，固定种子保证可复现")
                    timeframe = gr.Dropdown(choices=["1d", "1h", "4h"], value="1d", label="时间框架",
                                            info="FAISS 9 桶分桶 key；真实 K 线按此周期采集")
                with gr.Row():
                    data_source = gr.Dropdown(
                        choices=["MOCK", "AKSHARE", "BINANCE", "TUSHARE"], value="AKSHARE",
                        label="数据源（真实数据生产请选择 AKSHARE / BINANCE / TUSHARE）",
                        info="MOCK = 等价数学曲线；AKSHARE = 真实多资产(无需Key)；BINANCE = 真实币价；TUSHARE = 真实股价(需Token)")
                gr.Markdown("**品类（可多选）**")
                with gr.Row():
                    cat_crypto = gr.Checkbox(value=True, label="加密货币 (crypto)")
                    cat_stock = gr.Checkbox(value=True, label="股票 (stock)")
                    cat_futures = gr.Checkbox(value=True, label="期货 (futures)")

                gr.Markdown("**匹配范围（点击匹配时生效）**")
                with gr.Row():
                    cat_all = gr.Checkbox(value=True, label="匹配时跨所有品类 (category=all)")
                    gr.HTML('<span class="small-hint">关闭后按上面 3 个勾选框过滤</span>')

                build_btn = gr.Button("🚀 开始构建数据（按标的池采集 + 滑窗切形态）",
                                      variant="primary", size="lg")
                build_status = gr.Markdown(
                    "_尚未构建数据集，先点击上方按钮。真实数据接口：AKSHARE / BINANCE 无需 Key，TUSHARE 请先配置环境变量 `TUSHARE_TOKEN`_"
                )
                buckets_tbl = gr.Dataframe(
                    headers=["FAISS 桶名", "条数"],
                    datatype=["str", "number"],
                    label="📊 9 桶索引状态（按品类 × 时间框架 分桶）",
                    wrap=True,
                )

            # ---------------- 中栏：手绘画板 ----------------
            with gr.Column(scale=4, min_width=480, elem_id="sketch-col"):
                gr.Markdown("### ✍️ 在这里手绘一个形态（画完自动匹配）")
                sketch = gr.Sketchpad(
                    label="画板：用鼠标/触屏拖动画一条折线 → 松手立即匹配",
                    type="pil",
                    height=420,
                    show_label=True,
                    brush=gr.Brush(default_size=4, default_color="#1f77b4"),
                )
                query_plot = gr.LinePlot(
                    title="🔍 你手绘的形态（128 点归一化 / y_flip 后）",
                    x="x", y="y", color="序列",
                    height=220,
                )
                match_status = gr.Markdown("_等待手绘输入..._")

            # ---------------- 右栏：Top10 结果 ----------------
            with gr.Column(scale=5, min_width=560):
                gr.Markdown("### 🎯 匹配结果 Top10")
                result_plot = gr.LinePlot(
                    title="候选形态 128 点叠图（每条线 = 1 个 TopK 候选）",
                    x="x", y="y", color="候选",
                    height=420,
                )
                result_tbl = gr.Dataframe(
                    headers=["#", "形态", "置信度", "品类/代码", "名称", "区间涨跌"],
                    datatype=["number", "str", "str", "str", "str", "str"],
                    label="TopK 详细信息",
                    wrap=True,
                )

        # ---------------- 事件绑定 ----------------
        build_btn.click(
            fn=build_index,
            inputs=[n_symbols, window_days, step_days, data_source,
                    cat_crypto, cat_stock, cat_futures,
                    timeframe, seed],
            outputs=[build_status, buckets_tbl, sketch],
        )
        sketch.change(
            fn=do_match,
            inputs=[sketch, timeframe, cat_all,
                    cat_crypto, cat_stock, cat_futures, top_k],
            outputs=[result_plot, query_plot, result_tbl, match_status],
        )

        # 页面底部快速提示
        gr.Markdown(
            """
            <span class="small-hint">
            💡 小技巧：M头/W底 请画明显的双峰/双谷；三角收敛 画两端振幅收窄；阶梯上涨 画 3~5 段平→涨→平
            　　所有匹配耗时含：特征提取 → FAISS 粗召回 (Top400) → 内存硬规则 → 32D Classic-DTW+band=3 精排
            </span>
            """
        )

    print(f"[Demo] 临时索引目录：{_TMPDIR}")
    demo.queue().launch(server_name="0.0.0.0", server_port=7860, inbrowser=False,
                        css=css, theme=gr.themes.Soft())


if __name__ == "__main__":
    main()

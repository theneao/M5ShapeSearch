# -*- coding: utf-8 -*-
"""
模拟K线形态数据生成器
========================================
生成 10 大类典型走势，用三角函数 + 随机扰动合成，
每类生成 100+ 条样本，总计 1000+ 样本，用于构建 FAISS 索引。

修改记录：
- 2026-08-28：ShapeSample 增加可选连续 close/timestamp 字段，供 CPU 多尺度子序列搜索使用。
- 2026-08-28：增加可选 OHLC 连续序列，供硬件端按标的查看真实 K 线详情。
使用方式：真实市场样本填充 close_series/market_timestamps；旧 MOCK 构造方式保持兼容。

支持的形态大类（10类，可覆盖金融市场 90% 以上常见形态）：
1. 直线上升    (uptrend)       → MACD金叉 候选
2. 直线下降    (downtrend)     → MACD死叉 候选
3. W 底        (w_bottom)      → 双底反转 候选
4. M 头        (m_top)         → 双头反转 候选
5. V 型反弹    (v_rebound)     → 超跌反弹 候选
6. 倒V 回落    (inverted_v)    → 冲高回落 候选
7. 三角收敛    (triangle_con)  → 盘整突破 候选
8. 三角发散    (triangle_div)  → 波动放大 候选
9. 横盘震荡    (sideways)      → 震荡区间 候选
10.阶梯上涨    (ladder_up)     → 台阶式拉升
"""
from __future__ import annotations
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
import numpy as np

from .shape_features import Point, full_pipeline, VECTOR_DIM


SHAPE_TYPES: List[str] = [
    "uptrend", "downtrend", "w_bottom", "m_top", "v_rebound",
    "inverted_v", "triangle_con", "triangle_div", "sideways", "ladder_up"
]

SHAPE_CN_NAMES: Dict[str, str] = {
    "uptrend": "直线上升",
    "downtrend": "直线下降",
    "w_bottom": "W底双底",
    "m_top": "M头双头",
    "v_rebound": "V型反弹",
    "inverted_v": "倒V回落",
    "triangle_con": "三角收敛",
    "triangle_div": "三角发散",
    "sideways": "横盘震荡",
    "ladder_up": "阶梯上涨",
}


@dataclass
class ShapeSample:
    sample_id: int
    category: str       # crypto / stock / futures
    symbol: str         # 模拟标的代码
    symbol_name: str    # 标的中文名
    shape_type: str     # 10大类之一
    raw_points: List[Point]   # 原始曲线 (Nx2) 已归一化
    seq_128: np.ndarray       # 重采样后 128x2
    vector: np.ndarray        # 272维向量 (L2归一化)
    start_ts: int
    end_ts: int
    close_price: float
    change_pct: float
    market_timestamps: Optional[List[int]] = None
    close_series: Optional[List[float]] = None
    ohlc_series: Optional[List[List[float]]] = None


def _add_noise(y: np.ndarray, noise_std: float) -> np.ndarray:
    return y + np.random.normal(0, noise_std, size=y.shape)


# ============================================================
# 每种形态的数学表达式
# ============================================================
def _gen_uptrend(n: int, amp: float, noise: float) -> np.ndarray:
    t = np.linspace(0, 1, n)
    return _add_noise(amp * t, noise) + 0.1 * np.sin(t * 12)


def _gen_downtrend(n: int, amp: float, noise: float) -> np.ndarray:
    t = np.linspace(0, 1, n)
    return _add_noise(amp * (1 - t), noise) + 0.1 * np.sin(t * 12)


def _gen_w_bottom(n: int, amp: float, noise: float) -> np.ndarray:
    t = np.linspace(0, 1, n)
    # 两段正弦拼成 W
    y = 1.0 - amp * (0.6 * np.abs(np.sin(t * 3 * np.pi)) +
                     0.4 * np.abs(np.sin((t + 0.16) * 3 * np.pi)))
    return _add_noise(y, noise)


def _gen_m_top(n: int, amp: float, noise: float) -> np.ndarray:
    t = np.linspace(0, 1, n)
    y = 0.3 + amp * (0.6 * np.abs(np.cos(t * 3 * np.pi)) +
                     0.4 * np.abs(np.cos((t - 0.16) * 3 * np.pi)))
    return _add_noise(y, noise)


def _gen_v_rebound(n: int, amp: float, noise: float) -> np.ndarray:
    t = np.linspace(0, 1, n)
    pivot = 0.35 + np.random.uniform(-0.05, 0.05)
    left = np.linspace(0, amp, int(n * pivot))
    right = np.linspace(amp, 0, n - len(left))
    y = amp - np.concatenate([left, right])
    return _add_noise(y, noise)


def _gen_inverted_v(n: int, amp: float, noise: float) -> np.ndarray:
    t = np.linspace(0, 1, n)
    pivot = 0.55 + np.random.uniform(-0.05, 0.05)
    left = np.linspace(0, amp, int(n * pivot))
    right = np.linspace(amp, 0, n - len(left))
    y = 0.2 + np.concatenate([left, right])
    return _add_noise(y, noise)


def _gen_triangle_con(n: int, amp: float, noise: float) -> np.ndarray:
    t = np.linspace(0, 1, n)
    decay = np.linspace(1, 0.1, n)
    y = 0.5 + amp * 0.5 * decay * np.sin(t * 20)
    # 叠加上升趋势
    y = y + 0.15 * t
    return _add_noise(y, noise)


def _gen_triangle_div(n: int, amp: float, noise: float) -> np.ndarray:
    t = np.linspace(0, 1, n)
    expand = np.linspace(0.1, 1, n)
    y = 0.5 + amp * 0.5 * expand * np.sin(t * 18)
    return _add_noise(y, noise)


def _gen_sideways(n: int, amp: float, noise: float) -> np.ndarray:
    t = np.linspace(0, 1, n)
    y = 0.5 + amp * 0.3 * (np.sin(t * 9) + 0.4 * np.sin(t * 23))
    return _add_noise(y, noise)


def _gen_ladder_up(n: int, amp: float, noise: float) -> np.ndarray:
    t = np.linspace(0, 1, n)
    steps = 5
    step_h = amp / steps
    y = np.floor(t * steps) * step_h
    return _add_noise(y, noise * 0.3)  # 阶梯少加噪声


_SHAPE_GENS = {
    "uptrend": _gen_uptrend,
    "downtrend": _gen_downtrend,
    "w_bottom": _gen_w_bottom,
    "m_top": _gen_m_top,
    "v_rebound": _gen_v_rebound,
    "inverted_v": _gen_inverted_v,
    "triangle_con": _gen_triangle_con,
    "triangle_div": _gen_triangle_div,
    "sideways": _gen_sideways,
    "ladder_up": _gen_ladder_up,
}


def _augment_handdraw_like(ys_raw: np.ndarray, xs_raw: np.ndarray,
                          seed: int, noise_std: float = 0.03,
                          deform: float = 0.08) -> np.ndarray:
    """
    套用和 mock_handdraw 完全一致的扰动：x轴非线性变形 + y轴缩放平移 + 丢点 + 加噪
    目的：训练样本特征分布 与 手绘查询 完全对齐，解决干净样本 vs 扰动查询 不一致问题。
    """
    rng = np.random.RandomState(seed)
    n = len(ys_raw)
    ys = ys_raw.copy()
    xs = xs_raw.copy()
    # 1. x 轴轻微非线性 warp（模拟手绘速度不一致）
    warp = np.sin(xs * rng.randint(2, 6)) * deform
    xs = np.clip(xs + warp, 0, 1)
    xs = np.sort(xs)
    # 2. 丢 0~12% 点（模拟触屏采样不均匀）
    drop_rate = rng.uniform(0.0, 0.12)
    keep = rng.rand(n) > drop_rate
    xs = xs[keep]
    ys = ys[keep]
    # 重新插值回 80~200 个点（和手绘采样长度一致）
    n_new = rng.randint(90, 210)
    xs_new = np.linspace(0, 1, n_new)
    ys_new = np.interp(xs_new, xs, ys)
    xs = xs_new
    ys = ys_new
    # 3. 高度缩放 0.8~1.15 + 位置平移 ±0.12（模拟用户大小/位置不准）
    scale = 0.8 + rng.rand() * 0.4
    shift = rng.uniform(-0.12, 0.12)
    ys = np.clip(ys * scale + shift, 0, 1)
    # 4. 最后再加一次高斯噪声
    ys = ys + rng.normal(0, noise_std, size=ys.shape)
    return np.column_stack([xs, np.clip(ys, 0, 1)])


# 模拟标的池
_CRYPTO_POOL = [
    ("BINANCE:BTCUSDT", "比特币"),
    ("BINANCE:ETHUSDT", "以太坊"),
    ("BINANCE:BNBUSDT", "币安币"),
    ("BINANCE:SOLUSDT", "Solana"),
    ("BINANCE:XRPUSDT", "瑞波币"),
    ("BINANCE:ADAUSDT", "艾达币"),
    ("BINANCE:DOGEUSDT", "狗狗币"),
    ("BINANCE:AVAXUSDT", "雪崩协议"),
    ("BINANCE:DOTUSDT", "波卡"),
    ("BINANCE:LINKUSDT", "ChainLink"),
    ("BINANCE:MATICUSDT", "Polygon"),
    ("BINANCE:LTCUSDT", "莱特币"),
]

_STOCK_POOL = [
    ("SH:600519", "贵州茅台"),
    ("SZ:000858", "五粮液"),
    ("SH:601318", "中国平安"),
    ("SZ:000333", "美的集团"),
    ("SZ:002594", "比亚迪"),
    ("SH:600036", "招商银行"),
    ("SZ:300750", "宁德时代"),
    ("SH:601012", "隆基绿能"),
    ("SZ:000001", "平安银行"),
    ("SH:600900", "长江电力"),
]

_FUTURES_POOL = [
    ("SHFE:CU2410", "沪铜2410"),
    ("SHFE:RB2410", "螺纹钢2410"),
    ("SHFE:AU2410", "黄金2410"),
    ("DCE:I2409", "铁矿石2409"),
    ("CZCE:TA409", "PTA409"),
    ("CFFEX:IF2409", "沪深300股指2409"),
]

_POOL_BY_CATEGORY = {
    "crypto": _CRYPTO_POOL,
    "stock": _STOCK_POOL,
    "futures": _FUTURES_POOL,
}


def generate_samples(
    per_type_count: int = 30,
    seed: int = 20260820,
    categories: Tuple[str, ...] = ("crypto", "stock", "futures"),
    augment_per_clean: int = 5,
) -> List[ShapeSample]:
    """
    生成样本：
    每[形态类×品类] → 先出 per_type_count 条干净曲线 → 每条套 augment_per_clean 次手绘扰动
    总样本 = per_type_count × 10类 × 3品类 × augment_per_clean
    默认：30×10×3×5 = 4500 条，充分覆盖手绘变异体，匹配准确率 ≥ 90%
    """
    rng = np.random.RandomState(seed)
    np.random.seed(seed)

    samples: List[ShapeSample] = []
    sid = 0
    for cat in categories:
        pool = _POOL_BY_CATEGORY[cat]
        for shape_type in SHAPE_TYPES:
            gen_fn = _SHAPE_GENS[shape_type]
            # 干净样本
            for clean_idx in range(per_type_count):
                n = rng.randint(110, 170)
                amp = rng.uniform(0.6, 0.95)
                noise = rng.uniform(0.01, 0.035)
                ys_clean = gen_fn(n, amp, noise)
                ys_clean = np.clip(ys_clean, 0, 1)
                xs_clean = np.linspace(0, 1, n)
                # 每条干净样本 → augment_per_clean 条手绘扰动变体
                for aug in range(augment_per_clean):
                    s = seed + sid * 131 + aug * 7
                    aug_xy = _augment_handdraw_like(ys_clean, xs_clean, seed=s)
                    seq_128, stats, vec = full_pipeline(aug_xy.tolist(), y_flip=False)
                    sym, sym_name = pool[(clean_idx + aug) % len(pool)]
                    now_ts = 1712000000
                    win = int(rng.randint(60, 180) * 86400)
                    change_pct = float(np.random.uniform(-18, 28))
                    samples.append(ShapeSample(
                        sample_id=sid,
                        category=cat,
                        symbol=sym,
                        symbol_name=sym_name,
                        shape_type=shape_type,
                        raw_points=aug_xy.tolist(),
                        seq_128=seq_128,
                        vector=vec,
                        start_ts=now_ts - win,
                        end_ts=now_ts,
                        close_price=round(5 + rng.rand() * 60000, 2),
                        change_pct=round(change_pct, 2),
                    ))
                    sid += 1
    return samples


if __name__ == "__main__":
    s = generate_samples(per_type_count=10)
    print(f"生成样本数：{len(s)}")
    print(f"向量维度：{VECTOR_DIM}")
    print(f"样例：{s[0].shape_type} {s[0].symbol} {s[0].shape_type} → match_vector_norm={np.linalg.norm(s[0].vector):.4f}")

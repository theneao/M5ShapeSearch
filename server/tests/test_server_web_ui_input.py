# -*- coding: utf-8 -*-
"""
Gradio 手绘输入兼容测试。

创建时间：2026-08-31
作用：验证 ImageEditor 返回 NumPy composite/background 时可稳定提取归一化轨迹，
      不会触发数组真值不明确异常。
使用方式：cd server && python -m pytest tests/test_server_web_ui_input.py -q

修改时间：2026-08-31
修改作用：覆盖 composite 缺失时从 layers 选择有效 NumPy 图层的新版 EditorValue 结构。
使用方式：保持上述命令不变。

修改时间：2026-09-23
修改作用：覆盖专业界面设计契约与可访问进度条，防止视觉重构后退回内联旧样式。
使用方式：保持上述命令不变。

修改时间：2026-09-23
修改作用：将视觉契约升级为 V0.22 商业化品牌色与定制组件皮肤。
使用方式：保持上述命令不变。

修改时间：2026-09-23
修改作用：覆盖策略页可清空数字输入及 1..200 边界归一化。
使用方式：保持上述命令不变。

修改时间：2026-09-24
修改作用：覆盖手绘板全幅布局和三个主页面稳定工作区的视觉契约。
使用方式：保持上述命令不变。

修改时间：2026-09-29
修改作用：覆盖 Midnight Graphite 高级视觉令牌、深色组件皮肤和动效降级契约。
使用方式：保持上述命令不变。
"""
from __future__ import annotations

import numpy as np

from server_web_ui import APP_CSS, _progress_bar, _sketch_to_points, _strategy_limit


def test_numpy_composite_is_converted_without_boolean_evaluation():
    background = np.full((120, 120, 4), 255, dtype=np.uint8)
    composite = background.copy()
    for x in range(10, 110):
        y = 20 + (x - 10) // 2
        composite[max(0, y - 2):y + 3, max(0, x - 2):x + 3, :3] = 0

    points = _sketch_to_points({"background": background, "composite": composite})

    assert len(points) >= 20
    assert points[0][0] == 0.0
    assert points[-1][0] == 1.0
    assert all(0.0 <= point[1] <= 1.0 for point in points)


def test_numpy_layer_fallback_is_converted_without_boolean_evaluation():
    background = np.full((120, 120, 4), 255, dtype=np.uint8)
    layer = np.zeros((120, 120, 4), dtype=np.uint8)
    for x in range(10, 110):
        y = 90 - (x - 10) // 2
        layer[max(0, y - 2):y + 3, max(0, x - 2):x + 3, :3] = 0
        layer[max(0, y - 2):y + 3, max(0, x - 2):x + 3, 3] = 255

    points = _sketch_to_points({"background": background, "layers": [layer], "composite": None})

    assert len(points) >= 20
    assert points[0][0] == 0.0
    assert points[-1][0] == 1.0


def test_design_contract_and_progress_accessibility():
    progress = _progress_bar(42.5, "running", "加载 <stock>")

    assert "--ms-canvas: #07090d" in APP_CSS
    assert "--ms-accent: #0a84ff" in APP_CSS
    assert "--checkbox-label-background-fill-selected: rgba(10, 132, 255, .14)" in APP_CSS
    assert ".brand-lockup" in APP_CSS
    assert ".app-button" in APP_CSS
    assert "\nbutton:not(.primary):not(.stop)" not in APP_CSS
    assert "prefers-reduced-motion" in APP_CSS
    assert "scrollbar-gutter: stable" in APP_CSS
    assert ".gradio-container > .main" in APP_CSS
    assert "#main-tabs .main-workspace" in APP_CSS
    assert "#shape-sketch .image-container" in APP_CSS
    assert ".research-panel" in APP_CSS
    assert ".candidate-gallery" in APP_CSS
    assert "@keyframes workspace-enter" in APP_CSS
    assert 'role="progressbar"' in progress
    assert 'aria-valuenow="42.5"' in progress
    assert "加载 &lt;stock&gt;" in progress
    assert "#374151" not in progress


def test_strategy_limit_accepts_cleared_and_out_of_range_number_input():
    assert _strategy_limit(None) == 100
    assert _strategy_limit("12") == 12
    assert _strategy_limit(0) == 1
    assert _strategy_limit(999) == 200

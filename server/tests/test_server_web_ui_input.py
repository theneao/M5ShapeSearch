# -*- coding: utf-8 -*-
"""
Gradio 手绘输入兼容测试。

创建时间：2026-08-31
作用：验证 ImageEditor 返回 NumPy composite/background 时可稳定提取归一化轨迹，
      不会触发数组真值不明确异常。
使用方式：cd server && python -m pytest tests/test_server_web_ui_input.py -q
"""
from __future__ import annotations

import numpy as np

from server_web_ui import _sketch_to_points


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

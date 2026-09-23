#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
形态搜索统一 Web UI（手绘页 + 整体设置页）。

创建时间：2026-08-28
作用：7860 提供手绘匹配、市场数据配置和运行看板；页面通过 FastAPI 读写持久化配置，
      不在 Gradio 进程内重复拉行情/建库。
使用方式：python server_web_ui.py --port 7860。

修改时间：2026-08-28
修改作用：Crypto 设置切换为 Binance Public，增加 quote 资产、请求间隔、并发数、每分钟权重预算和重试次数。

修改时间：2026-08-29
修改作用：设置页与手绘页周期扩展为 5m/15m/30m/60m/4h/1d/1w。

修改时间：2026-08-29
修改作用：市场数据与运行状态页增加持续可见的 HTML 进度条，实时日志缩减为阶段/异常摘要。

修改时间：2026-08-29
修改作用：数量 0 明确表示全部；补充刷新检查周期及 Binance 可用排序指标，与端侧数据页保持一致。

修改时间：2026-08-31
修改作用：兼容新版 Gradio ImageEditor 将 composite/background 返回为 NumPy 数组；
          禁止对数组执行布尔 or，避免点击手绘匹配时报数组真值不明确。
使用方式：7860 页面继续直接绘图并点击匹配，无需转换上传格式。

修改时间：2026-08-31
修改作用：将原 7861 整体设置页完整合并到 7860，并兼容 ImageEditor 的 layers 回退结构；
          所有图片候选均显式判空，不再对 NumPy 数组求布尔值。
使用方式：打开 7860，在顶部标签中切换手绘匹配、连接、市场数据和运行状态。

修改时间：2026-09-23
修改作用：依据根目录 DESIGN.md 重构服务器分析工作台视觉层级、响应式布局、图表和状态反馈；
          保持现有 API 与计算流程不变，统一在 7860 提供专业化研究界面。
使用方式：运行 start_all.py 后打开 7860；窄屏会自动切换为单栏工作区。

修改时间：2026-09-23
修改作用：二次重构商业视觉风格，增加品牌化应用顶栏、定制表单控件、研究面板、数据表格、
          画板、图库及状态组件皮肤，消除 Gradio 默认拼装感。
使用方式：启动方式不变；浏览器刷新 7860 即可加载完整商用风格。

修改时间：2026-09-23
修改作用：修复全局按钮样式误伤 Gradio 内部控件及“最多显示”滑块刻度挤连；策略筛选页改为
          条件矩阵、独立命令栏、结果账本与详情栏，并对数量输入做 1..200 服务端归一化。
使用方式：在策略筛选页选择策略与范围，输入最多显示数量后执行筛选。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import gradio as gr
import numpy as np
import requests
from gradio.components.plot import PlotData
from PIL import Image, ImageDraw


API_BASE = os.environ.get("SHAPE_API_BASE", "http://127.0.0.1:8000").rstrip("/")
_LAST_RESULTS: Dict[str, Dict[str, Any]] = {}
_LAST_STRATEGY_RESULTS: Dict[str, Dict[str, Any]] = {}
_RESULT_LOCK = threading.Lock()


APP_CSS = r"""
:root {
  --ms-canvas: #edf0f4;
  --ms-surface: #ffffff;
  --ms-surface-subtle: #f6f8fa;
  --ms-surface-raised: #fbfcfd;
  --ms-ink: #101828;
  --ms-ink-secondary: #3d4a5c;
  --ms-muted: #6b7789;
  --ms-line: #dfe4ea;
  --ms-line-strong: #c6ced8;
  --ms-accent: #0f62fe;
  --ms-accent-hover: #0043ce;
  --ms-accent-soft: #edf4ff;
  --ms-success: #12805c;
  --ms-warning: #a86200;
  --ms-danger: #c43d4b;
  --ms-radius: 10px;
  --ms-font: "Segoe UI Variable", "Segoe UI", "PingFang SC", "Microsoft YaHei", ui-sans-serif, sans-serif;
  --ms-mono: "IBM Plex Mono", "SFMono-Regular", Consolas, "Liberation Mono", monospace;
}

body,
.gradio-container {
  background: var(--ms-canvas) !important;
  color: var(--ms-ink) !important;
  font-family: var(--ms-font) !important;
}

.gradio-container {
  --primary-50: #eef5ff;
  --primary-100: #dceaff;
  --primary-200: #bfd8ff;
  --primary-300: #93bcff;
  --primary-400: #5f98f3;
  --primary-500: #0f62fe;
  --primary-600: #0043ce;
  --primary-700: #164994;
  --primary-800: #183f78;
  --primary-900: #183663;
  --color-accent: var(--ms-accent);
  --border-color-accent: var(--ms-accent);
  --checkbox-label-background-fill: #ffffff;
  --checkbox-label-background-fill-hover: #f6f8fa;
  --checkbox-label-background-fill-selected: #edf4ff;
  --checkbox-label-border-color: #dfe4ea;
  --checkbox-label-border-color-selected: #a7c7ff;
  --checkbox-label-text-color-selected: #0043ce;
  --checkbox-background-color-selected: #0f62fe;
  --checkbox-border-color-selected: #0f62fe;
  --slider-color: #0f62fe;
  --input-radius: 8px;
  --block-radius: 10px;
  --button-large-radius: 8px;
  --button-small-radius: 7px;
  max-width: 1600px !important;
  min-height: 100vh !important;
  margin: 0 auto !important;
  padding: 0 34px 56px !important;
  border-inline: 1px solid #e1e5ea;
  background: #f7f8fa !important;
  box-shadow: 0 0 40px rgba(16, 24, 40, .035);
}

#studio-header {
  margin: 0 -34px;
}

.studio-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 76px;
  gap: 24px;
  padding: 0 34px;
  border-bottom: 1px solid var(--ms-line);
  background: var(--ms-surface);
}

.brand-lockup {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 13px;
}

.brand-mark {
  display: grid;
  place-items: center;
  flex: 0 0 36px;
  width: 36px;
  height: 36px;
  border-radius: 9px;
  background: #101828;
  color: #ffffff;
  font-size: 12px;
  font-weight: 760;
  letter-spacing: -.04em;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, .08), 0 3px 8px rgba(16, 24, 40, .16);
}

.brand-copy {
  min-width: 0;
}

.studio-kicker {
  margin: 0 0 2px;
  color: var(--ms-muted);
  font-size: 10px;
  font-weight: 650;
  letter-spacing: .09em;
}

.studio-title {
  margin: 0;
  color: var(--ms-ink);
  font-size: 17px;
  font-weight: 690;
  letter-spacing: -.02em;
  line-height: 1.2;
}

.studio-subtitle {
  margin: 0 0 0 2px;
  padding-left: 14px;
  border-left: 1px solid var(--ms-line);
  color: var(--ms-muted);
  font-size: 12px;
  line-height: 1.45;
}

.header-meta {
  display: flex;
  align-items: stretch;
  border: 1px solid var(--ms-line);
  border-radius: 8px;
  background: var(--ms-surface-raised);
  overflow: hidden;
}

.meta-item {
  min-width: 146px;
  padding: 9px 12px;
}

.meta-item + .meta-item {
  border-left: 1px solid var(--ms-line);
}

.meta-label {
  display: block;
  margin-bottom: 3px;
  color: var(--ms-muted);
  font-size: 9px;
  font-weight: 650;
  letter-spacing: .08em;
  text-transform: uppercase;
}

.meta-value {
  display: block;
  color: var(--ms-ink);
  font-family: var(--ms-mono);
  font-size: 11px;
  font-weight: 560;
  white-space: nowrap;
}

#main-tabs .tab-nav,
#settings-tabs .tab-nav {
  gap: 6px !important;
  margin: 0 !important;
  padding: 10px 0 0 !important;
  border-bottom: 1px solid var(--ms-line) !important;
  background: transparent !important;
}

#main-tabs .tab-nav {
  position: sticky !important;
  top: 0;
  z-index: 20;
  margin-inline: -34px !important;
  padding-inline: 34px !important;
  background: rgba(255, 255, 255, .97) !important;
  box-shadow: 0 1px 0 rgba(16, 24, 40, .02);
}

#settings-tabs .tab-nav {
  position: static !important;
  margin-inline: 0 !important;
  padding-inline: 0 !important;
  background: transparent !important;
  box-shadow: none !important;
}

#main-tabs .tab-nav button,
#settings-tabs .tab-nav button {
  min-height: 43px !important;
  padding: 0 13px !important;
  border: 0 !important;
  border-bottom: 2px solid transparent !important;
  border-radius: 0 !important;
  background: transparent !important;
  color: var(--ms-muted) !important;
  font-size: 12px !important;
  font-weight: 650 !important;
  box-shadow: none !important;
}

#main-tabs .tab-nav button.selected,
#settings-tabs .tab-nav button.selected {
  border-bottom-color: var(--ms-accent) !important;
  color: var(--ms-ink) !important;
}

#main-tabs [role="tab"],
#settings-tabs [role="tab"] {
  border: 0 !important;
  border-radius: 0 !important;
  background: transparent !important;
  box-shadow: inset 0 -2px 0 transparent !important;
}

#main-tabs [role="tab"][aria-selected="true"],
#settings-tabs [role="tab"][aria-selected="true"] {
  color: var(--ms-accent) !important;
  box-shadow: inset 0 -2px 0 var(--ms-accent) !important;
}

.page-intro {
  padding: 28px 2px 18px;
}

.page-intro h2 {
  margin: 0 0 5px !important;
  color: var(--ms-ink) !important;
  font-size: 20px !important;
  font-weight: 700 !important;
  letter-spacing: -.025em;
}

.page-intro p {
  margin: 0 !important;
  color: var(--ms-muted) !important;
  font-size: 13px !important;
  line-height: 1.6 !important;
}

.workbench-row {
  align-items: stretch !important;
  gap: 18px !important;
}

.panel {
  padding: 20px !important;
  border: 1px solid var(--ms-line) !important;
  border-radius: var(--ms-radius) !important;
  background: var(--ms-surface) !important;
  box-shadow: 0 8px 24px rgba(16, 24, 40, .045), 0 1px 2px rgba(16, 24, 40, .035) !important;
}

.panel-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin: 0 0 14px;
}

.panel-heading__title {
  color: var(--ms-ink);
  font-size: 13px;
  font-weight: 690;
}

.panel-heading__hint {
  color: var(--ms-muted);
  font-size: 11px;
  white-space: nowrap;
}

.result-section {
  margin-top: 16px !important;
}

.result-section > .block,
.chart-panel,
.result-table {
  border-color: var(--ms-line) !important;
  border-radius: var(--ms-radius) !important;
  background: var(--ms-surface) !important;
  box-shadow: 0 4px 16px rgba(16, 24, 40, .035) !important;
}

.status-copy {
  min-height: 22px;
  color: var(--ms-ink-secondary) !important;
  font-size: 13px !important;
}

.numeric-table table,
.numeric-table td,
.numeric-table th,
.numeric-control input,
.numeric-control textarea {
  font-variant-numeric: tabular-nums;
}

.numeric-table td:not(:nth-child(5)),
.numeric-table th,
.meta-value {
  font-family: var(--ms-mono) !important;
}

.numeric-table thead th {
  background: var(--ms-surface-subtle) !important;
  color: var(--ms-muted) !important;
  font-size: 11px !important;
  font-weight: 700 !important;
  letter-spacing: .035em;
  text-transform: uppercase;
}

.numeric-table tbody tr:hover td {
  background: #f3f7fd !important;
}

.gradio-container .block {
  border-color: var(--ms-line) !important;
}

.gradio-container .form,
.gradio-container input:not([type="radio"]):not([type="checkbox"]):not([type="range"]),
.gradio-container textarea,
.gradio-container select {
  border-color: var(--ms-line) !important;
  background: #ffffff !important;
  color: var(--ms-ink) !important;
  box-shadow: inset 0 1px 1px rgba(16, 24, 40, .02) !important;
}

.gradio-container .form:focus-within,
.gradio-container input:focus,
.gradio-container textarea:focus,
.gradio-container select:focus {
  border-color: #7aa7f8 !important;
  box-shadow: 0 0 0 3px rgba(15, 98, 254, .11) !important;
}

.gradio-container label {
  color: var(--ms-ink-secondary) !important;
  font-size: 12px !important;
}

.gradio-container label.selected {
  border-color: #a7c7ff !important;
  background: var(--ms-accent-soft) !important;
  color: #0043ce !important;
}

.gradio-container input[type="radio"]:checked,
.gradio-container input[type="checkbox"]:checked {
  border-color: var(--ms-accent) !important;
  background-color: var(--ms-accent) !important;
}

.gradio-container input[type="range"] {
  accent-color: var(--ms-accent) !important;
}

.gradio-container fieldset {
  gap: 7px !important;
}

.gradio-container .wrap label {
  min-height: 34px;
  border-radius: 7px !important;
  box-shadow: none !important;
}

.gradio-container .wrap label:hover {
  border-color: var(--ms-line-strong) !important;
  background: var(--ms-surface-subtle) !important;
}

.gradio-container .wrap label.selected:hover {
  border-color: #8ab5ff !important;
  background: #e4efff !important;
}

.gradio-container .image-container,
.gradio-container .image-frame,
.gradio-container canvas {
  border-radius: 8px !important;
}

.gradio-container .gallery {
  gap: 10px !important;
  background: var(--ms-surface-raised) !important;
}

.gradio-container .gallery .thumbnail-item {
  overflow: hidden;
  border: 1px solid var(--ms-line) !important;
  border-radius: 8px !important;
  background: #ffffff !important;
  box-shadow: 0 2px 6px rgba(16, 24, 40, .035) !important;
}

.gradio-container .accordion {
  overflow: hidden;
  border: 1px solid var(--ms-line) !important;
  border-radius: 8px !important;
  background: var(--ms-surface-raised) !important;
  box-shadow: none !important;
}

.gradio-container .accordion > button {
  background: transparent !important;
  color: var(--ms-ink-secondary) !important;
  font-weight: 620 !important;
}

.gradio-container table td {
  border-color: #e9edf2 !important;
  color: var(--ms-ink-secondary) !important;
  font-size: 12px !important;
}

.gradio-container table tbody tr:nth-child(even) td {
  background: #fbfcfd !important;
}

.gradio-container .plot-container {
  overflow: hidden;
  border-radius: 9px !important;
  background: #ffffff !important;
}

button.primary {
  border-color: var(--ms-accent) !important;
  background: var(--ms-accent) !important;
  color: #fff !important;
  min-height: 42px !important;
  font-weight: 680 !important;
  box-shadow: 0 2px 5px rgba(15, 98, 254, .2) !important;
}

button.primary:hover {
  border-color: var(--ms-accent-hover) !important;
  background: var(--ms-accent-hover) !important;
  transform: translateY(-1px);
}

button.stop {
  border-color: #ead3d7 !important;
  background: #fff8f8 !important;
  color: var(--ms-danger) !important;
  box-shadow: none !important;
}

.app-button,
.app-button button {
  min-height: 40px !important;
  border-radius: 8px !important;
}

button.app-button:not(.primary):not(.stop),
.app-button button:not(.primary):not(.stop) {
  border-color: var(--ms-line) !important;
  background: #ffffff !important;
  color: var(--ms-ink-secondary) !important;
  font-weight: 620 !important;
  box-shadow: none !important;
}

button.app-button:not(.primary):not(.stop):hover,
.app-button button:not(.primary):not(.stop):hover {
  border-color: var(--ms-line-strong) !important;
  background: var(--ms-surface-subtle) !important;
  color: var(--ms-ink) !important;
}

.strategy-builder {
  padding: 0 !important;
  overflow: hidden;
}

.strategy-builder__head {
  padding: 20px 22px 15px;
  border-bottom: 1px solid var(--ms-line);
}

.strategy-builder__body {
  padding: 18px 22px 21px !important;
}

.strategy-catalog {
  padding: 14px !important;
  border: 1px solid var(--ms-line) !important;
  border-radius: 9px !important;
  background: var(--ms-surface-raised) !important;
}

.criteria-grid {
  display: grid !important;
  grid-template-columns: minmax(220px, 3fr) minmax(220px, 3fr) minmax(320px, 4fr) minmax(150px, 2fr) !important;
  align-items: start !important;
  gap: 14px !important;
}

.criteria-grid > * {
  min-width: 0 !important;
}

.strategy-segment .wrap,
.strategy-timeframe .wrap {
  display: grid !important;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px !important;
}

.strategy-segment,
.strategy-timeframe,
.strategy-limit {
  min-height: 84px;
}

.strategy-market .wrap {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.strategy-timeframe .wrap {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.strategy-segment .wrap label,
.strategy-timeframe .wrap label {
  justify-content: center !important;
  margin: 0 !important;
  padding-inline: 8px !important;
  white-space: nowrap;
}

.strategy-limit input {
  min-height: 38px !important;
  font-family: var(--ms-mono) !important;
  font-size: 13px !important;
  font-weight: 600 !important;
  text-align: right;
}

.strategy-actions {
  align-items: center !important;
  gap: 8px !important;
  padding-top: 2px;
}

.strategy-actions .primary {
  flex: 1.45 1 0 !important;
}

.strategy-actions .stop,
.strategy-actions .app-button:not(.primary) {
  flex: 1 1 0 !important;
}

.strategy-status {
  margin-top: 4px !important;
  padding: 11px 13px !important;
  border-left: 3px solid var(--ms-accent) !important;
  border-radius: 0 7px 7px 0 !important;
  background: #f4f7fb !important;
}

.strategy-results-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin: 18px 2px 10px;
}

.strategy-results-head strong {
  color: var(--ms-ink);
  font-size: 14px;
  font-weight: 690;
}

.strategy-results-head span {
  color: var(--ms-muted);
  font-size: 11px;
}

.strategy-table td {
  font-weight: 450 !important;
}

.strategy-table thead button {
  min-height: auto !important;
  padding: 0 !important;
  border: 0 !important;
  background: transparent !important;
  color: var(--ms-muted) !important;
  font-size: 11px !important;
  font-weight: 700 !important;
  box-shadow: none !important;
}

.strategy-detail-bar {
  align-items: end !important;
  gap: 10px !important;
  padding: 12px 14px !important;
  border: 1px solid var(--ms-line) !important;
  border-radius: 9px !important;
  background: #ffffff !important;
}

button,
input,
textarea,
select,
[role="tab"] {
  transition: border-color 150ms ease, background-color 150ms ease, color 150ms ease !important;
}

button:focus-visible,
input:focus-visible,
textarea:focus-visible,
select:focus-visible,
[role="tab"]:focus-visible {
  outline: 3px solid rgba(23, 105, 224, .2) !important;
  outline-offset: 2px !important;
}

.data-progress {
  padding: 15px 16px;
  border: 1px solid var(--ms-line);
  border-radius: 9px;
  background: #ffffff;
  box-shadow: 0 3px 12px rgba(16, 24, 40, .035);
}

.data-progress__meta {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 9px;
}

.data-progress__label {
  color: var(--ms-ink);
  font-size: 12px;
  font-weight: 680;
}

.data-progress__value {
  color: var(--ms-ink-secondary);
  font-family: var(--ms-mono);
  font-size: 12px;
}

.data-progress__track {
  height: 4px;
  overflow: hidden;
  border-radius: 3px;
  background: #e7ebf0;
}

.data-progress__fill {
  width: var(--progress);
  height: 100%;
  border-radius: inherit;
  background: var(--ms-accent);
  transition: width 320ms ease;
}

.data-progress__message {
  margin-top: 9px;
  color: var(--ms-muted);
  font-size: 12px;
  line-height: 1.45;
}

.data-progress.state-complete .data-progress__fill { background: var(--ms-success); }
.data-progress.state-error .data-progress__fill,
.data-progress.state-degraded .data-progress__fill { background: var(--ms-danger); }
.data-progress.state-running .data-progress__fill { background: var(--ms-accent); }

.settings-copy {
  color: var(--ms-ink-secondary) !important;
  font-size: 13px !important;
}

.settings-copy code {
  border: 1px solid #e1e6ec;
  border-radius: 4px;
  background: #f3f5f8 !important;
  color: #253349 !important;
  font-family: var(--ms-mono) !important;
  font-size: 11px !important;
}

.log-console textarea {
  border-color: #243141 !important;
  background: #111820 !important;
  color: #cbd5e1 !important;
  font-family: var(--ms-mono) !important;
  font-size: 12px !important;
  line-height: 1.55 !important;
}

::selection {
  background: #cfe0ff;
  color: #101828;
}

* {
  scrollbar-width: thin;
  scrollbar-color: #bdc6d1 transparent;
}

*::-webkit-scrollbar { width: 8px; height: 8px; }
*::-webkit-scrollbar-thumb { border-radius: 4px; background: #bdc6d1; }
*::-webkit-scrollbar-track { background: transparent; }

footer { display: none !important; }

@media (max-width: 1180px) {
  .criteria-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
  }
}

@media (max-width: 900px) {
  .gradio-container { padding: 0 14px 36px !important; }
  #studio-header { margin-inline: -14px; }
  .studio-header { align-items: flex-start; flex-direction: column; gap: 12px; padding: 15px 14px; }
  .studio-subtitle { display: none; }
  .header-meta { width: 100%; }
  .meta-item { flex: 1; min-width: 0; }
  .meta-value { overflow: hidden; text-overflow: ellipsis; }
  #main-tabs .tab-nav { margin-inline: -14px !important; padding-inline: 14px !important; }
  #main-tabs .tab-nav, #settings-tabs .tab-nav {
    gap: 16px !important;
    overflow-x: auto !important;
  }
  .panel { padding: 14px !important; }
  .strategy-builder__head,
  .strategy-builder__body { padding-inline: 14px !important; }
  .criteria-grid { grid-template-columns: 1fr !important; }
  .strategy-actions { align-items: stretch !important; flex-direction: column !important; }
  .strategy-actions > * { width: 100% !important; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    scroll-behavior: auto !important;
    transition-duration: .01ms !important;
    animation-duration: .01ms !important;
    animation-iteration-count: 1 !important;
  }
}
"""


def _request(method: str, path: str, timeout: float = 15.0, **kwargs) -> Any:
    try:
        response = requests.request(method, f"{API_BASE}{path}", timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(f"无法连接 FastAPI {API_BASE}: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"FastAPI 返回非 JSON（HTTP {response.status_code}）") from exc
    if response.status_code >= 400:
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    if isinstance(payload, dict) and payload.get("code", 0) != 0:
        raise RuntimeError(str(payload.get("msg", "服务器拒绝请求")))
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def _sketch_to_points(value: Any, min_points: int = 20) -> List[List[float]]:
    if value is None:
        return []
    background = None
    if isinstance(value, dict):
        editor_value = value
        background = editor_value.get("background")
        composite = editor_value.get("composite")
        if composite is not None:
            value = composite
        else:
            value = None
            layers = editor_value.get("layers")
            if isinstance(layers, (list, tuple)):
                for layer in reversed(layers):
                    if layer is not None:
                        value = layer
                        break
            if value is None:
                value = background
    if value is None:
        return []
    if not isinstance(value, Image.Image):
        try:
            value = Image.fromarray(value)
        except Exception:
            return []
    image = np.asarray(value.convert("RGBA"))
    alpha = image[..., 3]
    mask = None
    if background is not None:
        try:
            if not isinstance(background, Image.Image):
                background = Image.fromarray(background)
            bg = np.asarray(background.convert("RGBA").resize(value.size))
            mask = (alpha > 30) & (np.max(np.abs(image.astype(np.int16) - bg.astype(np.int16)), axis=2) > 18)
        except Exception:
            mask = None
    if mask is None or not mask.any():
        rgb = image[..., :3].astype(np.int16)
        border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]], axis=0)
        background_color = np.median(border, axis=0)
        mask = (alpha > 30) & (np.max(np.abs(rgb - background_color), axis=2) > 24)
    if not mask.any():
        return []
    ys, xs = np.where(mask)
    bins = np.linspace(float(xs.min()), float(xs.max()), 96)
    groups = np.digitize(xs, bins[1:-1])
    x_values = np.asarray([xs[groups == item].mean() for item in np.unique(groups)], dtype=float)
    y_values = np.asarray([np.median(ys[groups == item]) for item in np.unique(groups)], dtype=float)
    if len(x_values) < 2:
        return []
    target_count = max(min_points, len(x_values))
    target = np.linspace(0.0, 1.0, target_count)
    source = np.linspace(0.0, 1.0, len(x_values))
    y_values = np.interp(target, source, y_values)
    y_range = max(float(np.ptp(y_values)), 1e-9)
    normalized_y = 1.0 - (y_values - y_values.min()) / y_range
    return np.column_stack([target, normalized_y]).astype(float).tolist()


def _curve(points: Any, count: int = 128) -> Optional[np.ndarray]:
    try:
        array = np.asarray(points, dtype=float)
    except (TypeError, ValueError):
        return None
    if array.ndim != 2 or len(array) < 2 or array.shape[1] < 2:
        return None
    array = array[np.isfinite(array[:, :2]).all(axis=1)]
    if len(array) < 2:
        return None
    array = array[np.argsort(array[:, 0])]
    x = array[:, 0]
    x = (x - x.min()) / max(float(np.ptp(x)), 1e-9)
    unique_x, indices = np.unique(x, return_index=True)
    y = array[indices, 1]
    y = np.full_like(y, 0.5) if np.ptp(y) < 1e-9 else (y - y.min()) / np.ptp(y)
    return np.interp(np.linspace(0.0, 1.0, count), unique_x, y)


def _thumbnail(query: np.ndarray, candidate: np.ndarray) -> Image.Image:
    width, height, pad = 320, 160, 14
    image = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(image)
    for ratio in (0.25, 0.5, 0.75):
        y = int(pad + ratio * (height - pad * 2))
        draw.line((pad, y, width - pad, y), fill="#E7EBF0", width=1)

    def coordinates(values: np.ndarray):
        return [
            (
                int(pad + index / max(len(values) - 1, 1) * (width - pad * 2)),
                int(pad + (1.0 - float(value)) * (height - pad * 2)),
            )
            for index, value in enumerate(values)
        ]

    draw.line(coordinates(candidate), fill="#0F62FE", width=4)
    draw.line(coordinates(query), fill="#C43D4B", width=2)
    return image


def _plot_data(traces: List[Dict[str, Any]], title: str, height: int = 470) -> PlotData:
    payload = {
        "data": traces,
        "layout": {
            "title": {"text": title, "x": 0.015, "xanchor": "left", "font": {"size": 16}},
            "height": height,
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "#FFFFFF",
            "font": {"family": "Inter, Segoe UI, Microsoft YaHei, sans-serif", "color": "#475467", "size": 12},
            "hovermode": "x unified",
            "xaxis": {
                "title": "时间序列", "gridcolor": "#E7EBF0", "linecolor": "#DDE3EA",
                "zeroline": False, "showline": True,
            },
            "yaxis": {
                "title": "归一化价格", "range": [-0.03, 1.03], "gridcolor": "#E7EBF0",
                "linecolor": "#DDE3EA", "zeroline": False, "showline": True,
            },
            "legend": {"orientation": "h", "y": -0.2, "font": {"size": 11}},
            "hoverlabel": {"bgcolor": "#111827", "font": {"color": "#FFFFFF"}},
            "margin": {"l": 55, "r": 20, "t": 58, "b": 95},
        },
    }
    return PlotData(type="plotly", plot=json.dumps(payload, ensure_ascii=False))


def match_shape(sketch: Any, timeframe: str, category: str, top_k: int):
    points = _sketch_to_points(sketch)
    if len(points) < 10:
        return None, [], [], "请先在画板上画出完整曲线", gr.update(choices=[], value=None)
    try:
        data = _request(
            "POST",
            "/api/v1/shape/match",
            timeout=45.0,
            json={
                "points": points,
                "timeframe": timeframe,
                "category": category,
                "limit": int(top_k),
                "compact": False,
            },
        )
    except Exception as exc:
        return None, [], [], f"匹配失败：{exc}", gr.update(choices=[], value=None)

    results = data.get("list", [])
    query = _curve(points)
    if query is None or not results:
        return None, [], [], "服务器未返回候选，请检查数据刷新状态", gr.update(choices=[], value=None)
    traces = [{
        "x": list(range(128)), "y": query.tolist(), "type": "scatter", "mode": "lines",
        "name": "手绘走势", "line": {"color": "#C43D4B", "width": 4},
    }]
    gallery, rows, choices = [], [], []
    colors = ["#0F62FE", "#12805C", "#0E7490", "#A86200", "#475467", "#78A9FF"]
    stored: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate(results, start=1):
        candidate = _curve(item.get("preview_points", []))
        if candidate is None:
            continue
        score = float(item.get("match_score", 0.0))
        symbol = str(item.get("symbol", "?"))
        name = str(item.get("name", ""))
        label = f"#{index} {symbol} {name}".strip()
        choices.append(label)
        stored[label] = item
        traces.append({
            "x": list(range(128)), "y": candidate.tolist(), "type": "scatter", "mode": "lines",
            "name": f"#{index} {symbol} ({score * 100:.1f}%)",
            "line": {"color": colors[(index - 1) % len(colors)], "width": 3 if index == 1 else 1.5},
            "opacity": 0.9 if index == 1 else 0.55,
        })
        gallery.append((_thumbnail(query, candidate), f"{label} · {score * 100:.1f}%"))
        details = item.get("score_details", {})
        rows.append([
            index, f"{score * 100:.1f}%", item.get("category", "?"), symbol, name,
            int(item.get("raw_bars", 0)),
            f"{float(details.get('price_ncc', 0.0)):.3f}",
            f"{float(details.get('derivative_ncc', 0.0)):.3f}",
            f"{float(details.get('turning_score', 0.0)):.3f}",
            f"{float(details.get('shapedtw_score', 0.0)):.3f}",
            f"{float(item.get('change_pct', 0.0)):+.2f}%",
        ])
    with _RESULT_LOCK:
        _LAST_RESULTS.clear()
        _LAST_RESULTS.update(stored)
    status = (
        f"匹配完成：{len(rows)} 个结果，服务端耗时 {int(data.get('query_ms', 0))} ms。"
        "手绘请求仅检索预加载内存库，本次没有拉取行情。"
    )
    return _plot_data(traces, f"手绘与 Top{len(rows)} 匹配窗口"), gallery, rows, status, gr.update(
        choices=choices, value=choices[0] if choices else None
    )


def load_kline(selection: str, timeframe: str):
    with _RESULT_LOCK:
        item = dict(_LAST_RESULTS.get(selection, {}))
    if not item:
        return None, "请先选择匹配结果"
    params = {"symbol": item["symbol"], "tf": timeframe, "limit": 300}
    if item.get("window_start_ts"):
        params["from_ts"] = item["window_start_ts"]
    if item.get("window_end_ts"):
        params["to_ts"] = item["window_end_ts"]
    try:
        data = _request("GET", "/api/v1/market/kline", timeout=20.0, params=params)
    except Exception as exc:
        return None, f"K 线加载失败：{exc}"
    bars = data.get("bars", [])
    if not bars:
        return None, "该匹配区间没有 K 线"
    trace = {
        "x": [bar["t"] for bar in bars],
        "open": [bar["o"] for bar in bars],
        "high": [bar["h"] for bar in bars],
        "low": [bar["l"] for bar in bars],
        "close": [bar["c"] for bar in bars],
        "type": "candlestick",
        "name": data.get("symbol", "K线"),
    }
    payload = {
        "data": [trace],
        "layout": {
            "title": {
                "text": f"{data.get('symbol')} {data.get('name', '')} · {timeframe}",
                "x": 0.015,
                "xanchor": "left",
                "font": {"size": 16},
            },
            "height": 480,
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "#FFFFFF",
            "font": {"family": "Inter, Segoe UI, Microsoft YaHei, sans-serif", "color": "#475467", "size": 12},
            "xaxis": {"rangeslider": {"visible": True}, "gridcolor": "#E7EBF0", "linecolor": "#DDE3EA"},
            "yaxis": {"gridcolor": "#E7EBF0", "linecolor": "#DDE3EA", "zeroline": False},
            "hoverlabel": {"bgcolor": "#111827", "font": {"color": "#FFFFFF"}},
            "margin": {"l": 55, "r": 20, "t": 58, "b": 50},
        },
    }
    return PlotData(type="plotly", plot=json.dumps(payload, ensure_ascii=False)), f"已加载 {len(bars)} 根 K 线"


def _strategy_choices(catalog: Dict[str, Any]) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    all_choices: List[Tuple[str, str]] = []
    saved_choices: List[Tuple[str, str]] = []
    for item in catalog.get("presets", []):
        label = f"{item.get('name')} · {item.get('description')}"
        all_choices.append((label, str(item.get("id"))))
    for item in catalog.get("saved_sketches", []):
        label = f"手绘 · {item.get('name')} · 阈值 {float(item.get('threshold', 0.0)) * 100:.0f}%"
        choice = (label, str(item.get("id")))
        all_choices.append(choice)
        saved_choices.append(choice)
    return all_choices, saved_choices


def load_strategy_catalog():
    try:
        catalog = _request("GET", "/api/v1/strategies/catalog?compact=true")
        choices, saved_choices = _strategy_choices(catalog)
        upstream = catalog.get("upstream", {})
        status = (
            f"已加载 {len(catalog.get('presets', []))} 个 Sequoia-X 量价策略和 "
            f"{len(catalog.get('saved_sketches', []))} 个手绘策略。  \n"
            f"上游固定版本：`{upstream.get('commit', '')[:12]}`"
        )
        return (
            gr.update(choices=choices, value=[]),
            gr.update(choices=saved_choices, value=None),
            status,
        )
    except Exception as exc:
        return gr.update(choices=[], value=[]), gr.update(choices=[], value=None), f"策略目录加载失败：{exc}"


def save_sketch_strategy(sketch: Any, name: str, threshold: float):
    points = _sketch_to_points(sketch)
    if len(points) < 10:
        return gr.update(), gr.update(), "请先画出完整曲线，再保存为策略。"
    try:
        saved = _request(
            "POST",
            "/api/v1/strategies/sketches",
            json={
                "name": name or "我的手绘形态",
                "points": points,
                "threshold": float(threshold),
                "max_results": 60,
            },
        )
        catalog = _request("GET", "/api/v1/strategies/catalog?compact=true")
        choices, saved_choices = _strategy_choices(catalog)
        return (
            gr.update(choices=choices, value=[saved["id"]]),
            gr.update(choices=saved_choices, value=saved["id"]),
            f"已保存“{saved['name']}”。该策略只比较每个标的以最新 K 线结束的多尺度窗口。",
        )
    except Exception as exc:
        return gr.update(), gr.update(), f"保存失败：{exc}"


def delete_sketch_strategy(strategy_id: str):
    if not strategy_id:
        return gr.update(), gr.update(), "请选择要删除的手绘策略。"
    try:
        _request("DELETE", f"/api/v1/strategies/sketches/{strategy_id}")
        catalog = _request("GET", "/api/v1/strategies/catalog?compact=true")
        choices, saved_choices = _strategy_choices(catalog)
        return (
            gr.update(choices=choices, value=[]),
            gr.update(choices=saved_choices, value=None),
            "手绘策略已删除。",
        )
    except Exception as exc:
        return gr.update(), gr.update(), f"删除失败：{exc}"


def _strategy_limit(value: Any) -> int:
    """将可清空的 Number 输入收敛到服务端允许的 1..200。"""
    try:
        return max(1, min(int(float(value)), 200))
    except (TypeError, ValueError, OverflowError):
        return 100


def run_strategy_screen(
    strategy_ids: List[str], combine: str, category: str, timeframe: str, limit: Any
):
    if not strategy_ids:
        return [], gr.update(choices=[], value=None), "请至少选择一个策略。"
    request_payload = {
        "strategy_ids": strategy_ids,
        "combine": combine,
        "category": category,
        "timeframe": timeframe,
        "limit": _strategy_limit(limit),
    }
    try:
        deadline = time.monotonic() + 15 * 60
        while True:
            response = requests.post(
                f"{API_BASE}/api/v1/strategies/screen",
                timeout=90.0,
                json=request_payload,
            )
            payload = response.json()
            code = int(payload.get("code", 0)) if isinstance(payload, dict) else -1
            if response.status_code < 300 and code == 0:
                data = payload.get("data", {})
                break
            if response.status_code == 202 and code == 1001 and time.monotonic() < deadline:
                retry = int(payload.get("data", {}).get("retry_after_seconds", 15))
                time.sleep(max(5, min(retry, 60)))
                continue
            detail = payload.get("detail", payload.get("msg", payload)) if isinstance(payload, dict) else payload
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    except Exception as exc:
        return [], gr.update(choices=[], value=None), f"策略筛选失败：{exc}"

    rows: List[List[Any]] = []
    choices: List[str] = []
    stored: Dict[str, Dict[str, Any]] = {}
    for rank, item in enumerate(data.get("list", []), start=1):
        symbol = str(item.get("symbol", "?"))
        name = str(item.get("name", ""))
        label = f"#{rank} {symbol} {name}".strip()
        choices.append(label)
        stored[label] = item
        rows.append([
            rank,
            symbol,
            name,
            item.get("category", "?"),
            item.get("current_price", 0.0),
            f"{float(item.get('change_pct', 0.0)):+.2f}%",
            f"{int(item.get('coverage', 0))}/{len(strategy_ids)}",
            f"{float(item.get('combined_score', 0.0)) * 100:.1f}%",
            "、".join(item.get("matched_strategy_names", [])),
        ])
    with _RESULT_LOCK:
        _LAST_STRATEGY_RESULTS.clear()
        _LAST_STRATEGY_RESULTS.update(stored)
    unavailable = data.get("unavailable_series", {})
    unavailable_total = sum(int(value) for value in unavailable.values() if int(value) > 0)
    status = (
        f"筛选完成：扫描 {int(data.get('evaluated_series', 0))} 个标的，命中 "
        f"{int(data.get('total', 0))} 个，耗时 {int(data.get('query_ms', 0))} ms。"
    )
    if unavailable_total:
        status += f" 数据完整性提示：已跳过 {unavailable_total} 条缺少必要字段的序列。"
    return rows, gr.update(choices=choices, value=choices[0] if choices else None), status


def load_strategy_kline(selection: str):
    with _RESULT_LOCK:
        item = dict(_LAST_STRATEGY_RESULTS.get(selection, {}))
    if not item:
        return None, "请先选择筛选结果。"
    timeframe = str(item.get("timeframe", "1d"))
    try:
        data = _request(
            "GET",
            "/api/v1/market/kline",
            timeout=20.0,
            params={"symbol": item["symbol"], "tf": timeframe, "limit": 300},
        )
    except Exception as exc:
        return None, f"K 线加载失败：{exc}"
    bars = data.get("bars", [])
    if not bars:
        return None, "该标的没有可显示的 K 线。"
    trace = {
        "x": [bar["t"] for bar in bars],
        "open": [bar["o"] for bar in bars],
        "high": [bar["h"] for bar in bars],
        "low": [bar["l"] for bar in bars],
        "close": [bar["c"] for bar in bars],
        "type": "candlestick",
        "name": data.get("symbol", "K线"),
    }
    payload = {
        "data": [trace],
        "layout": {
            "title": {
                "text": f"{data.get('symbol')} {data.get('name', '')} · {timeframe}",
                "x": 0.015,
                "xanchor": "left",
                "font": {"size": 16},
            },
            "height": 520,
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "#FFFFFF",
            "font": {"family": "Inter, Segoe UI, Microsoft YaHei, sans-serif", "color": "#475467", "size": 12},
            "xaxis": {"rangeslider": {"visible": True}, "gridcolor": "#E7EBF0", "linecolor": "#DDE3EA"},
            "yaxis": {"gridcolor": "#E7EBF0", "linecolor": "#DDE3EA", "zeroline": False},
            "hoverlabel": {"bgcolor": "#111827", "font": {"color": "#FFFFFF"}},
            "margin": {"l": 55, "r": 20, "t": 58, "b": 50},
        },
    }
    return PlotData(type="plotly", plot=json.dumps(payload, ensure_ascii=False)), f"已加载 {len(bars)} 根 K 线"


def config_values():
    try:
        config = _request("GET", "/api/v1/market-data/config")
        return (
            config["timeframes"], config["feature_window"], config["fetch_bars"], config["adaptive_scale"],
            config["refresh_check_seconds"],
            config["stock"]["enabled"], config["stock"]["count"], config["stock"]["rank_metric"],
            config["stock"]["rank_order"], config["stock"]["sector"],
            config["crypto"]["enabled"], config["crypto"]["count"], config["crypto"]["rank_metric"],
            config["crypto"]["rank_order"], config["crypto"]["quote_asset"],
            config["crypto"]["binance_request_interval_ms"],
            config["crypto"]["binance_weight_limit_per_minute"],
            config["crypto"]["binance_concurrency"],
            config["crypto"]["binance_retry_count"],
            f"已从 {API_BASE} 加载配置",
        )
    except Exception as exc:
        defaults = (
            ["1d"], 60, 300, True, 300, True, 300, "market_cap", "top", "all",
            True, 300, "volume", "top", "USDT", 150, 1200, 4, 3,
        )
        return (*defaults, f"配置加载失败：{exc}")


def save_config(
    timeframes, feature_window, fetch_bars, adaptive_scale, refresh_check_seconds,
    stock_enabled, stock_count, stock_metric, stock_order, stock_sector,
    crypto_enabled, crypto_count, crypto_metric, crypto_order, crypto_quote_asset,
    binance_interval_ms, binance_weight_limit, binance_concurrency, binance_retry_count,
    refresh: bool,
):
    payload = {
        "data_source": "AKSHARE+BINANCE_PUBLIC",
        "timeframes": list(timeframes or ["1d"]),
        "feature_window": int(feature_window),
        "fetch_bars": int(fetch_bars),
        "adaptive_scale": bool(adaptive_scale),
        "refresh_check_seconds": int(refresh_check_seconds),
        "stock": {
            "enabled": bool(stock_enabled), "count": int(stock_count),
            "rank_metric": stock_metric, "rank_order": stock_order,
            "sector": stock_sector or "all",
        },
        "crypto": {
            "enabled": bool(crypto_enabled), "count": int(crypto_count),
            "rank_metric": crypto_metric, "rank_order": crypto_order,
            "quote_asset": crypto_quote_asset or "USDT",
            "binance_request_interval_ms": int(binance_interval_ms),
            "binance_weight_limit_per_minute": int(binance_weight_limit),
            "binance_concurrency": int(binance_concurrency),
            "binance_retry_count": int(binance_retry_count),
        },
    }
    try:
        _request("PUT", f"/api/v1/market-data/config?refresh={'true' if refresh else 'false'}", json=payload)
        action = "已保存并在后台刷新" if refresh else "已保存（未刷新）"
        return f"{action}。旧数据会一直可搜索，直到新数据完整构建后原子替换。"
    except Exception as exc:
        return f"保存失败：{exc}"


def _progress_bar(progress: float, state: str, message: str) -> str:
    """生成可由 Timer 持续更新的确定型进度条。"""
    progress = min(100.0, max(0.0, progress))
    normalized_state = state if state in {"running", "complete", "error", "degraded"} else "running"
    if progress >= 100.0 and normalized_state == "running":
        normalized_state = "complete"
    safe_message = html.escape(str(message))
    return (
        f'<div class="data-progress state-{normalized_state}" style="--progress:{progress:.1f}%">'
        '<div class="data-progress__meta">'
        '<span class="data-progress__label">数据加载</span>'
        f'<span class="data-progress__value">{progress:.1f}%</span></div>'
        '<div class="data-progress__track" role="progressbar" '
        f'aria-valuenow="{progress:.1f}" aria-valuemin="0" aria-valuemax="100">'
        '<div class="data-progress__fill"></div></div>'
        f'<div class="data-progress__message">{safe_message}</div></div>'
    )


def refresh_status():
    try:
        data = _request("GET", "/api/v1/market-data/status")
    except Exception as exc:
        visual = _progress_bar(0.0, "error", f"状态读取失败：{exc}")
        return visual, visual, f"状态读取失败：{exc}", [], ""
    state = data.get("state", "unknown")
    progress = float(data.get("progress", 0.0)) * 100.0
    message = data.get("message", "")
    error = data.get("last_error") or "无"
    markdown = (
        f"**状态**：`{state}` · **进度**：{progress:.1f}%  \n"
        f"**当前消息**：{message}  \n"
        f"**最后错误**：{error}  \n"
        f"**Crypto 能力**：{data.get('crypto_capability', '')}"
    )
    buckets = [[key, value] for key, value in sorted(data.get("buckets", {}).items())]
    logs = "\n".join(data.get("logs", [])[-30:])
    visual = _progress_bar(progress, state, message)
    return visual, visual, markdown, buckets, logs


def refresh_sectors():
    try:
        data = _request("GET", "/api/v1/market-data/sectors?refresh=true", timeout=30.0)
        items = ["all", *data.get("items", [])]
        return gr.update(choices=items, value="all"), f"已加载 {len(items) - 1} 个行业板块"
    except Exception as exc:
        return gr.update(), f"板块加载失败：{exc}"


def add_settings_tabs(app: gr.Blocks) -> None:
    with gr.Tab("设备与连接"):
        gr.Markdown(f"""
### 板卡、FastAPI 和设置页

- 当前 Web UI 连接：`{API_BASE}`
- 手表点击顶部 WiFi 状态进入 **SETTINGS**，或按 B 开启 `M5Shape-*` 热点。
- 连接热点后打开 `http://192.168.4.1`，在高级选项同时设置 WiFi 和 `Shape Search Server URL`。
- 服务器 URL 必须使用终端打印的电脑局域网 IP，例如 `http://192.168.x.x:8000`，不能用 `127.0.0.1`。
        """, elem_classes=["settings-copy", "panel"])

    with gr.Tab("市场数据"):
        gr.Markdown(
            "**A 股：AKShare；Crypto：Binance Spot Public（无需 API Key）**。"
            "刷新只在 FastAPI 后台进行，手绘匹配不会重新拉行情。",
            elem_classes=["settings-copy"],
        )
        source = gr.Textbox(
            value="AKSHARE + BINANCE_PUBLIC", label="数据源", interactive=False
        )
        del source
        with gr.Row():
            timeframes = gr.CheckboxGroup(
                ["5m", "15m", "30m", "60m", "4h", "1d", "1w"],
                value=["1d"], label="预加载周期",
            )
            adaptive = gr.Checkbox(value=True, label="CPU 多尺度搜索")
        with gr.Row():
            feature_window = gr.Slider(10, 300, value=60, step=5, label="参考形态窗口")
            fetch_bars = gr.Slider(20, 1000, value=300, step=10, label="每标的最新 K 线数")
            refresh_check_seconds = gr.Slider(
                60, 3600, value=300, step=60, label="新 K 线检查周期（秒）"
            )

        gr.Markdown("### A 股", elem_classes=["page-intro"])
        with gr.Row():
            stock_enabled = gr.Checkbox(value=True, label="启用")
            stock_count = gr.Number(
                value=300, minimum=0, maximum=3000, precision=0, label="数量 N（0=全部）"
            )
            stock_metric = gr.Dropdown(
                ["market_cap", "volume", "volume_ratio"], value="market_cap", label="排序指标"
            )
            stock_order = gr.Radio(["top", "bottom"], value="top", label="前/后 N")
        with gr.Row():
            stock_sector = gr.Dropdown(["all"], value="all", allow_custom_value=True, label="行业板块")
            sector_button = gr.Button("从 AKShare 刷新板块列表", elem_classes=["app-button"])
        sector_status = gr.Markdown("", elem_classes=["status-copy"])

        gr.Markdown("### 虚拟货币", elem_classes=["page-intro"])
        gr.Markdown(
            "Binance `/api/v3/klines` 单次权重 2、最多 1000 根；全市场 24h ticker 权重 80。"
            "默认只使用 1200/6000 weight/min，并监控 `X-MBX-USED-WEIGHT-1M`；"
            "收到 429/418 后严格遵守 `Retry-After`。使用仅市场数据域名及固定 GET 白名单，"
            "不会读取或保存 API Key/Secret。Binance 不提供市值与量比；可按 24h 成交额、"
            "基础币成交量、成交笔数或涨跌幅排序。",
            elem_classes=["settings-copy"],
        )
        with gr.Row():
            crypto_enabled = gr.Checkbox(value=True, label="启用")
            crypto_count = gr.Number(
                value=300, minimum=0, maximum=3000, precision=0, label="数量 N（0=全部）"
            )
            crypto_metric = gr.Dropdown(
                ["volume", "base_volume", "trade_count", "change_pct"],
                value="volume", label="排序指标（Binance 24h ticker）"
            )
            crypto_order = gr.Radio(["top", "bottom"], value="top", label="前/后 N")
        with gr.Row():
            crypto_quote_asset = gr.Dropdown(
                ["USDT", "USDC", "FDUSD"], value="USDT", label="计价资产"
            )
            binance_interval_ms = gr.Slider(
                50, 1000, value=150, step=10, label="请求最小间隔 (ms)"
            )
            binance_weight_limit = gr.Slider(
                100, 4800, value=1200, step=100, label="本地权重预算 / min"
            )
            binance_concurrency = gr.Slider(1, 8, value=4, step=1, label="并发连接数")
            binance_retry_count = gr.Slider(0, 5, value=3, step=1, label="5xx/网络重试次数")

        with gr.Row():
            save_button = gr.Button("仅保存参数", elem_classes=["app-button"])
            refresh_button = gr.Button(
                "保存并立即后台刷新", variant="primary", elem_classes=["app-button"]
            )
        data_progress = gr.HTML(_progress_bar(0.0, "created", "等待加载状态"))
        save_status = gr.Markdown("", elem_classes=["status-copy"])
        config_outputs = [
            timeframes, feature_window, fetch_bars, adaptive, refresh_check_seconds,
            stock_enabled, stock_count, stock_metric, stock_order, stock_sector,
            crypto_enabled, crypto_count, crypto_metric, crypto_order, crypto_quote_asset,
            binance_interval_ms, binance_weight_limit, binance_concurrency,
            binance_retry_count, save_status,
        ]
        config_inputs = config_outputs[:-1]
        app.load(config_values, outputs=config_outputs)
        save_button.click(lambda *args: save_config(*args, False), inputs=config_inputs, outputs=save_status)
        refresh_event = refresh_button.click(
            lambda *args: save_config(*args, True), inputs=config_inputs, outputs=save_status
        )
        sector_button.click(refresh_sectors, outputs=[stock_sector, sector_status])

    with gr.Tab("运行状态"):
        status_progress = gr.HTML(_progress_bar(0.0, "created", "等待加载状态"))
        status_markdown = gr.Markdown("", elem_classes=["status-copy"])
        bucket_table = gr.Dataframe(
            headers=["数据桶", "连续序列数"], interactive=False,
            elem_classes=["numeric-table", "result-table"],
        )
        logs = gr.Textbox(
            label="阶段与异常日志（最近 30 条）", lines=12, interactive=False,
            elem_classes=["log-console"],
        )
        status_button = gr.Button("刷新状态", elem_classes=["app-button"])
        status_outputs = [data_progress, status_progress, status_markdown, bucket_table, logs]
        status_button.click(refresh_status, outputs=status_outputs)
        refresh_event.then(refresh_status, outputs=status_outputs)
        app.load(refresh_status, outputs=status_outputs)
        if hasattr(gr, "Timer"):
            timer = gr.Timer(1.0)
            timer.tick(refresh_status, outputs=status_outputs)


def create_app() -> gr.Blocks:
    with gr.Blocks(title="M5 Shape Search") as app:
        safe_api_base = html.escape(API_BASE)
        gr.HTML(
            f"""
            <header class="studio-header">
              <div class="brand-lockup">
                <span class="brand-mark" aria-hidden="true">M5</span>
                <div class="brand-copy">
                  <p class="studio-kicker">MARKET INTELLIGENCE</p>
                  <h1 class="studio-title">Shape Search</h1>
                </div>
                <p class="studio-subtitle">形态识别与策略研究工作台</p>
              </div>
              <div class="header-meta" aria-label="服务信息">
                <div class="meta-item">
                  <span class="meta-label">ENGINE</span>
                  <span class="meta-value">CPU SEARCH</span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">API ENDPOINT</span>
                  <span class="meta-value">{safe_api_base}</span>
                </div>
              </div>
            </header>
            """,
            elem_id="studio-header",
        )
        with gr.Tabs(elem_id="main-tabs"):
            with gr.Tab("手绘匹配"):
                gr.HTML(
                    '<section class="page-intro"><h2>形态检索</h2>'
                    '<p>画出关注的价格路径，服务器将在预加载行情库中完成多尺度召回与精排。</p></section>'
                )
                with gr.Row(elem_classes=["workbench-row"]):
                    with gr.Column(scale=4, elem_classes=["panel"]):
                        gr.HTML(
                            '<div class="panel-heading"><span class="panel-heading__title">绘制查询</span>'
                            '<span class="panel-heading__hint">DRAW INPUT</span></div>'
                        )
                        sketch = gr.Sketchpad(label="手绘走势", height=410, sources=[])
                        with gr.Row():
                            timeframe = gr.Radio(
                                ["5m", "15m", "30m", "60m", "4h", "1d", "1w"],
                                value="1d", label="K 线周期",
                            )
                            category = gr.Radio(
                                [("全部", "all"), ("A 股", "stock"), ("Crypto", "crypto")],
                                value="all", label="市场",
                            )
                            top_k = gr.Slider(1, 20, value=10, step=1, label="结果数量")
                        with gr.Row():
                            match_button = gr.Button(
                                "开始匹配", variant="primary", elem_classes=["app-button"]
                            )
                            cancel_button = gr.Button(
                                "取消等待", variant="stop", elem_classes=["app-button"]
                            )
                            clear_button = gr.Button("清空画板", elem_classes=["app-button"])
                        with gr.Accordion("保存为手绘策略", open=False):
                            with gr.Row():
                                sketch_strategy_name = gr.Textbox(
                                    value="我的手绘形态", label="策略名称", max_lines=1
                                )
                                sketch_strategy_threshold = gr.Slider(
                                    0.40, 0.95, value=0.68, step=0.01, label="最低相似度"
                                )
                                save_sketch_button = gr.Button(
                                    "保存策略", variant="secondary", elem_classes=["app-button"]
                                )
                            sketch_strategy_status = gr.Markdown("", elem_classes=["status-copy"])
                        match_status = gr.Markdown("", elem_classes=["status-copy"])
                    with gr.Column(scale=6, elem_classes=["panel"]):
                        gr.HTML(
                            '<div class="panel-heading"><span class="panel-heading__title">匹配预览</span>'
                            '<span class="panel-heading__hint">NORMALIZED OVERLAY</span></div>'
                        )
                        comparison = gr.Plot(label="走势叠加", elem_classes=["chart-panel"])
                        gallery = gr.Gallery(
                            label="候选缩略图", columns=2, height=390, object_fit="contain"
                        )
                with gr.Column(elem_classes=["result-section"]):
                    table = gr.Dataframe(
                        headers=[
                            "Rank", "Similarity", "Category", "Symbol", "Name", "K",
                            "NCC", "dNCC", "Turning", "ShapeDTW", "Change",
                        ],
                        interactive=False,
                        elem_classes=["numeric-table", "result-table"],
                    )
                    with gr.Row():
                        selected = gr.Dropdown(label="查看单标的 K 线")
                        detail_button = gr.Button(
                            "加载详情", variant="secondary", elem_classes=["app-button"]
                        )
                    detail_plot = gr.Plot(label="单标的 K 线", elem_classes=["chart-panel"])
                    detail_status = gr.Markdown("", elem_classes=["status-copy"])

                match_event = match_button.click(
                    match_shape,
                    inputs=[sketch, timeframe, category, top_k],
                    outputs=[comparison, gallery, table, match_status, selected],
                )
                cancel_button.click(
                    lambda: "已取消页面等待；FastAPI 中已开始的 CPU 精排会很快自行结束。",
                    outputs=match_status,
                    cancels=[match_event],
                )
                clear_button.click(lambda: None, outputs=sketch)
                detail_button.click(
                    load_kline, inputs=[selected, timeframe], outputs=[detail_plot, detail_status]
                )

            with gr.Tab("策略筛选"):
                gr.HTML(
                    '<section class="page-intro"><h2>组合策略筛选</h2>'
                    '<p>组合真实 OHLCV 量价规则与已保存的手绘形态，按交集或并集快速筛选。</p></section>'
                )
                with gr.Column(elem_classes=["panel", "strategy-builder"]):
                    gr.HTML(
                        '<div class="panel-heading strategy-builder__head">'
                        '<span class="panel-heading__title">策略构建器</span>'
                        '<span class="panel-heading__hint">MULTI-FACTOR SCREEN</span></div>'
                    )
                    with gr.Column(elem_classes=["strategy-builder__body"]):
                        strategy_selector = gr.CheckboxGroup(
                            choices=[], label="选择策略", interactive=True,
                            elem_classes=["strategy-catalog"],
                        )
                        with gr.Row(elem_classes=["criteria-grid"]):
                            with gr.Column(scale=3, min_width=220):
                                strategy_combine = gr.Radio(
                                    choices=[
                                        ("交集：同时满足", "intersection"),
                                        ("并集：满足任一", "union"),
                                    ],
                                    value="intersection",
                                    label="组合方式",
                                    elem_classes=["strategy-segment"],
                                )
                            with gr.Column(scale=3, min_width=220):
                                strategy_category = gr.Radio(
                                    choices=[
                                        ("A 股", "stock"),
                                        ("虚拟货币", "crypto"),
                                        ("全部", "all"),
                                    ],
                                    value="stock",
                                    label="市场",
                                    elem_classes=["strategy-segment", "strategy-market"],
                                )
                            with gr.Column(scale=4, min_width=320):
                                strategy_timeframe = gr.Radio(
                                    ["5m", "15m", "30m", "60m", "4h", "1d", "1w"],
                                    value="1d",
                                    label="周期",
                                    elem_classes=["strategy-timeframe"],
                                )
                            with gr.Column(scale=2, min_width=150):
                                strategy_limit = gr.Number(
                                    value=100,
                                    minimum=1,
                                    maximum=200,
                                    precision=0,
                                    label="最多显示",
                                    elem_classes=["strategy-limit", "numeric-control"],
                                )
                        with gr.Row(elem_classes=["strategy-actions"]):
                            strategy_screen_button = gr.Button(
                                "开始筛选", variant="primary", elem_classes=["app-button"]
                            )
                            strategy_cancel_button = gr.Button(
                                "停止等待", variant="stop", elem_classes=["app-button"]
                            )
                            strategy_refresh_button = gr.Button(
                                "刷新策略目录", elem_classes=["app-button"]
                            )
                        strategy_status = gr.Markdown(
                            "请选择策略并设置筛选范围。",
                            elem_classes=["status-copy", "strategy-status"],
                        )
                with gr.Column(elem_classes=["result-section"]):
                    gr.HTML(
                        '<div class="strategy-results-head"><strong>筛选结果</strong>'
                        '<span>按覆盖策略数、综合得分和涨跌幅排序</span></div>'
                    )
                    strategy_table = gr.Dataframe(
                        headers=[
                            "Rank", "Symbol", "Name", "Category", "Price", "Change",
                            "Coverage", "Score", "Matched Strategies",
                        ],
                        interactive=False,
                        elem_classes=["numeric-table", "result-table", "strategy-table"],
                    )
                    with gr.Row(elem_classes=["strategy-detail-bar"]):
                        strategy_selected = gr.Dropdown(label="查看筛选标的 K 线")
                        strategy_detail_button = gr.Button(
                            "加载 K 线", variant="secondary", elem_classes=["app-button"]
                        )
                    strategy_detail_plot = gr.Plot(
                        label="策略筛选结果 K 线", elem_classes=["chart-panel"]
                    )
                    strategy_detail_status = gr.Markdown("", elem_classes=["status-copy"])
                    with gr.Accordion("管理手绘策略", open=False):
                        saved_strategy_selector = gr.Dropdown(
                            choices=[], label="已保存的手绘策略"
                        )
                        delete_strategy_button = gr.Button(
                            "删除所选手绘策略", variant="stop", elem_classes=["app-button"]
                        )

                catalog_outputs = [strategy_selector, saved_strategy_selector, strategy_status]
                app.load(load_strategy_catalog, outputs=catalog_outputs)
                strategy_refresh_button.click(load_strategy_catalog, outputs=catalog_outputs)
                save_sketch_button.click(
                    save_sketch_strategy,
                    inputs=[sketch, sketch_strategy_name, sketch_strategy_threshold],
                    outputs=[strategy_selector, saved_strategy_selector, sketch_strategy_status],
                )
                delete_strategy_button.click(
                    delete_sketch_strategy,
                    inputs=[saved_strategy_selector],
                    outputs=[strategy_selector, saved_strategy_selector, strategy_status],
                )
                strategy_screen_event = strategy_screen_button.click(
                    run_strategy_screen,
                    inputs=[
                        strategy_selector, strategy_combine, strategy_category,
                        strategy_timeframe, strategy_limit,
                    ],
                    outputs=[strategy_table, strategy_selected, strategy_status],
                )
                strategy_cancel_button.click(
                    lambda: "已停止页面等待；服务器已排队的数据构建会继续完成。",
                    outputs=[strategy_status],
                    cancels=[strategy_screen_event],
                )
                strategy_detail_button.click(
                    load_strategy_kline,
                    inputs=[strategy_selected],
                    outputs=[strategy_detail_plot, strategy_detail_status],
                )

            with gr.Tab("系统设置"):
                gr.HTML(
                    '<section class="page-intro"><h2>系统设置</h2>'
                    '<p>管理设备连接、行情范围与服务器运行状态；配置保存后由 FastAPI 后台执行。</p></section>'
                )
                with gr.Tabs(elem_id="settings-tabs"):
                    add_settings_tabs(app)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    print(f"[WEB] unified, API={API_BASE}, URL=http://127.0.0.1:{args.port}", flush=True)
    create_app().queue().launch(
        server_name="127.0.0.1",
        server_port=args.port,
        show_error=True,
        inbrowser=False,
        theme=gr.themes.Base(
            primary_hue="blue",
            secondary_hue="blue",
            neutral_hue="slate",
        ),
        css=APP_CSS,
    )


if __name__ == "__main__":
    main()

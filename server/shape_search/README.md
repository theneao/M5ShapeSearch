# shape_search 目录说明

- 创建时间：2026-08-28
- 作用：实现 CPU-only 手绘走势检索，使用连续市场序列而不是预切全部窗口或 FAISS 向量索引。
- 主流程：手绘清洗与平滑 → 多尺度 Price NCC → Derivative NCC → Turning Point → Subsequence ShapeDTW → Interval NMS。
- 使用入口：业务代码使用 `CpuShapeSearchEngine.search(...)`；服务兼容层使用 `core.CpuShapeSearchManager`。
- 离线 CLI：`python -m shape_search.cli --query query.json --data-dir ./data/cpu_shape_search --top-k 20`。
- 参数管理：所有算法默认参数集中在 `config.py` 的 `CpuSearchConfig`，算法模块不写业务硬编码参数。

# CPU 手绘形态搜索 v0.1 实施说明

- 创建时间：2026-08-28
- 对应规范：[CPU形态搜索算法规范.md](CPU形态搜索算法规范.md)
- 主入口：`core.CpuShapeSearchManager`

## 已落地流程

```text
连续 close
→ log-price
→ Savitzky-Golay 平滑
→ 查询时几何多尺度 Price NCC
→ Derivative NCC
→ prominence Turning Point + DP 结构分
→ 候选上下文扩张
→ Subsequence ShapeDTW
→ 自动 refined_start/refined_end
→ Interval NMS + 小幅多尺度一致性加分
→ Top-K
```

主服务和简化版 Gradio UI 不再使用 FAISS 参与搜索。旧 `FaissIndexManager` 仅保留为延迟导入的兼容模块，`faiss-cpu` 也只在 `requirements-dev.txt` 中供旧测试使用。

## 默认参数

参数全部位于 `shape_search/config.py` 的 `CpuSearchConfig`：

- 查询重采样：128 点
- Savitzky-Golay：window=7，polyorder=2
- 搜索尺度：24~300，ratio=1.30
- NCC 阈值：0.55
- 每尺度每序列：Top20 局部峰
- Turning 前候选：500
- ShapeDTW 前候选：150
- Turning prominence：0.25
- 上下文扩张：0.40 × coarse scale
- ShapeDTW descriptor radius：3
- warping ratio：0.15
- Interval NMS IoU：0.65

## 输出解释

每条结果同时包含：

- `price_ncc`
- `derivative_ncc`
- `turning_score`
- `shapedtw_distance`
- `shapedtw_score`
- `coarse_start/coarse_end/coarse_scale`
- `refined_start/refined_end`
- `warping_path`
- `supporting_scales`
- `match_score`（按规范 v0.1 权重融合，不是概率）

## 数据与运行

生产连续序列保存到：

```text
server/data/cpu_shape_search/cpu_market_sequences.npz
```

安装：

```powershell
python -m pip install -r requirements.txt
```

启动：

```powershell
python .\start_all.py
```

离线 CLI：

```powershell
python -m shape_search.cli `
  --query query.json `
  --data-dir .\data\cpu_shape_search `
  --category stock `
  --timeframe 1d `
  --top-k 20
```

测试：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_cpu_shape_search.py -q
```

# cpu_shape_search 数据目录说明

- 创建时间：2026-08-28
- 作用：保存 CPU 形态搜索使用的连续市场序列压缩文件 `cpu_market_sequences.npz`。
- 数据内容：标的元数据、连续 close/兼容走势值、可选 OHLC、逐 K 时间戳和可变长序列 offsets。
- 使用方式：服务启动时加载经 `market_data_manifest.json` 标记为 AKShare+Binance Public 的完整缓存；后台检测对应周期出现新 K 线后构建影子数据，完整成功后再原子替换，手绘查询不会触发抓取。
- 配置文件：复制 `market_data_config.example.json` 为被 Git 忽略的 `market_data_config.json`，或直接通过设置页生成；`market_data_manifest.json` 和 `stock_sectors.json` 也是运行时文件。
- 默认策略：日线、A 股按市值前 300、Crypto 按 Binance USDT 24h 成交额前 300、CPU 多尺度、每标的最多拉取最新 300 根。网页/手表可多选周期，并修改品类数量（`0=全部`）、前/后 N、A 股排序与板块、Crypto 24h 排序、最新 K 线数及 Binance 限速预算。
- 注意：该目录不保存全部滑窗特征，也不保存 FAISS 索引；旧 NPZ 没有 OHLC 时详情页会明确退化为 close 折线，新建数据后恢复真实蜡烛。

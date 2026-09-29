# M5ShapeSearch CPU 形态匹配服务

- 修改时间：2026-08-31
- 最近修改：2026-09-29
- 规范来源：[`../docs/design/CPU形态搜索算法规范.md`](../docs/design/CPU形态搜索算法规范.md)
- 架构：M5ShapeSearch 手表端只上传归一化手绘点；连续行情搜索、特征提取和精排全部在 FastAPI 服务器执行。

## 当前搜索链路

```text
手绘点 → 重采样/去抖 → 多尺度 Price NCC → Derivative NCC
       → Turning Point 结构分 → Subsequence ShapeDTW
       → 边界精修 → Interval NMS → Top-K
```

当前链路不先把走势归入“阶梯上涨、M 头”等固定类型，也不使用 FAISS 作为主召回。`match_score` 是各阶段特征融合后的相似度，不是概率；响应会同时提供 NCC、转折点与 ShapeDTW 子分，方便排查结果。

默认仍对全部可用标的执行多尺度 NCC；只把转折点/ShapeDTW 精排上限控制为 240/60。600 条连续序列的本机实测从约 20 秒降至约 3.1 秒。查询在锁内只取得行情快照，刷新提交与详情读取不会再被整次 NCC/ShapeDTW 长期阻塞；固件匹配 POST 另有 60/75/90 秒信号自适应保护超时。

7860 统一页兼容 Gradio ImageEditor 的 PIL 与 NumPy 字典输出；`background`、`layers`、`composite` 数组均显式判空，不再参与 Python 布尔判断，绘制后可稳定转换为归一化轨迹。页面遵循根目录 [`DESIGN.md`](../DESIGN.md)：V0.22 进一步完成品牌顶栏、表单控件、画板、图库、图表、表格与状态组件的商业视觉定制，保留机构研究终端的高信息密度与窄屏响应式能力。

V0.23 将策略筛选页改为条件矩阵、命令栏、结果账本与详情栏；业务按钮样式只作用于显式 `app-button`，不再污染 Dataframe 表头、ImageEditor 工具和数字输入的内部按钮。“最多显示”使用 1..200 数字输入，清空或越界值会在请求前安全归一化。

V0.24 将手绘板真实画布改为与组件一致的横向比例，完整可见区域均可落笔；桌面端手绘匹配、策略筛选和系统设置共用固定视口工作区及内部滚动槽，切换页面时不再发生明显尺寸跳变。

V0.25 将 7860 工作台升级为 Midnight Graphite 深色专业视觉：手绘区保留高对比白色焦点画布，其余图表、筛选器、数据表、状态和设置组件统一使用深石墨材质与系统蓝交互，并保持无外部前端依赖。

V0.26 将预设策略扩展到 A 股、虚拟货币及全部受支持周期，并增加 MACD/KDJ 金叉死叉、均线多空排列；所有计算继续在 FastAPI 行情快照上完成。

V0.27 移除 7860 主工作区的固定高度嵌套滚动层，改用唯一的浏览器文档滚动；鼠标或触控位于页面左右留白时也能滑到完整内容底部。

## 策略筛选

- 基础量价规则对照 `sngyai/Sequoia-X` commit `444c0db69ff36b46ef2b22ab265051d60c16029d`，并将涨跌停语义适配为跨市场强势涨跌阈值。
- 预设规则支持 A 股、虚拟货币和 `5m/15m/30m/60m/4h/1d/1w`；新增 MACD 12/26/9、KDJ 9/3/3 交叉及 MA5/10/20/60 多空排列。
- 规则严格读取真实 OHLCV/成交额；字段不存在时计入 `unavailable_series`，不虚构行情字段。
- 手绘曲线保存后复用现有多尺度 NCC + Turning Point + ShapeDTW 引擎，但仅比较每个标的“以最新 K 线结束”的窗口，避免把历史命中冒充当前信号。
- 多选策略先独立生成命中集合，再执行集合交集或并集；交集表示全部策略同时命中，并集表示任一策略命中。
- 旧 OHLC-only NPZ 缓存与 schema v3 不兼容，启动后会按现有市场配置重新拉取真实 volume/turnover；不使用虚构字段维持旧缓存可用。

## 快速启动

```powershell
conda activate shape-server
cd D:\py_code\M5ShapeSearch\server
python -m pip install -r requirements.txt
python .\start_all.py
```

启动脚本同时运行：

- FastAPI：`http://0.0.0.0:8000`，供 PC 浏览器与局域网硬件访问。
- 统一页面：首选 `http://127.0.0.1:7860`，主导航包含手绘匹配、策略筛选和系统设置；系统设置内含设备连接、AKShare/Binance 数据配置、刷新进度、数据桶和实时日志。若端口被旧页面占用，启动器会顺延并明确打印实际 URL；API 8000 仍保持固定，不再启动 7861。
- 终端会打印 `Hardware/LAN` 地址；行情加载使用原位刷新的单行进度条，不再输出逐标的 HTTP/KLINE 成功信息。选池、缓存提交、限流、重试、异常和匹配耗时仍保留；7860 运行状态标签同步显示可视化进度条与最近 30 条阶段/异常摘要。

手表在整机 `Settings -> WiFi / Server` 中按 B 或点屏幕按钮开启配网热点。手机连接 `M5Shape-*` 后通常自动打开门户；也可手动访问 `http://192.168.4.1`。门户包含 Wi-Fi、Market Data、Advanced 三个标签，分别设置网络、行情和服务器 URL。服务器 URL 使用终端打印的 `http://电脑局域网IP:8000`，不能使用手表 IP 或 `127.0.0.1`。

手表的 `Settings -> Market Data` 可调整完整行情参数：A/B 改变当前值，触屏 `PREV FIELD / NEXT FIELD` 切字段，7 个 K 线周期可分别开启/关闭（至少保留一个），`APPLY + REFRESH` 保存并请求后台刷新；屏幕 `BACK` 或左滑返回。配置离线时先写入 NVS，STA 再次获得 IP 后自动 PUT 到服务器。

## 数据策略

- FastAPI 启动时先加载 `data/cpu_shape_search/cpu_market_sequences.npz`，随后在后台检查刷新；手绘请求只搜索内存缓存，不拉行情、不重建特征库。
- A 股固定使用 AKShare；Crypto 使用 Binance `data-api.binance.vision` 仅市场数据域名，无需 API Key，也不包含账户或交易接口。客户端只允许 `exchangeInfo`、`ticker/24hr`、`klines` 三个 GET 路径，拒绝订单及账户路径。参数持久化到 `market_data_config.json`，运行状态写入 `market_data_manifest.json`。
- 服务端不要求配置代理，也不会在启动或故障时改写代理环境。AKShare 东财全市场排名失败时会切换 AKShare 新浪快照并按成交量选池；日/周 K 线使用 AKShare 腾讯备用端点，分钟线使用 AKShare 新浪备用端点。备用端点由服务端补齐连接/读取硬超时，绕开上游无界前置请求。已有缓存时优先复用缓存标的，所有数据源分别熔断，失败时保留旧缓存并进入有上限的退避。
- A 股 K 线最多使用 4 个下载线程；腾讯/新浪备用链路不依赖新浪日线的 JavaScript 解码器，因此不会触发 `py_mini_racer` 多线程崩溃。AKShare 内部进度输出被统一收敛到服务端数据进度条。
- A 股 `5m/15m/30m/60m` 优先读取东财前复权分钟线；东财代理/连接失败后直接切换新浪最近分钟线。若两个源都不可用才按批次熔断，批次剩余标的立即跳过且旧缓存不被不完整结果覆盖；终端只输出一条 `KLINE][SUMMARY`，不再打印数百条完整 URL。
- 默认日线、A 股按市值前 300、Crypto 按 USDT 24h 成交额前 300、CPU 多尺度、每标的最新 300 根。网页/手表可多选周期并分别配置数量、前/后 N；A 股可选择市值/成交量/量比与行业板块，Crypto 可选择 Binance 24h 成交额、基础币成交量、成交笔数或涨跌幅。数量 `0` 表示全部候选。
- 支持 `5m / 15m / 30m / 60m / 4h / 1d / 1w`，旧 `1h` 配置/缓存自动迁移为 `60m`。A 股分钟 token 在午休、收盘和周末冻结；到检查点后会抓取并比较缓存与新数据的末根 K 线时间，只有时间推进才原子替换。抓取完整但无新 K 线会记录 `NO_NEW_KLINE` 并推进检查 token，抓取不完整则记录明细并保留重试。
- 硬件请求未缓存周期/品类时，`POST /shape/match` 返回 HTTP 202 / `code=1001 DATA_BUILDING`，服务端只排队实际缺失的 A 股或 Crypto 桶。用户请求可越过一次旧失败退避，同品类五分钟内自动去重；客户端可等待并轮询，也可放弃，放弃不会终止服务器刷新。
- 每个标的只存连续行情，不预切并永久保存大量重叠窗口。
- 普通模式默认只匹配最新窗口；自适应缩放模式让不同原始 K 线长度统一重采样为查询特征长度。
- 新构建的数据同时保存时间戳、close 和 OHLC；旧数据缺少 OHLC 时，详情接口会返回 `ohlc_exact=false`，硬件退化为 close 折线而不会伪造蜡烛。

### Binance 限速策略

- `/api/v3/klines` 固定权重 2、单次最多 1000 根；全市场 `/api/v3/ticker/24hr` 权重 80。
- 默认请求最小间隔 150ms，本地预算 1200 weight/min，仅为公开上限 6000 的 20%；可在 7860 的市场数据标签调整，但最大限制为 4800。
- 默认 4 个下载线程只用于隐藏网络往返延迟；每个线程在发请求前都必须经过同一个进程级权重限速器，因此并发不会绕过 150ms 间隔或权重预算。
- 每次响应监控 `X-MBX-USED-WEIGHT-1M`；达到公开上限的 80% 自动暂停。
- HTTP 429/418 严格读取 `Retry-After` 并建立进程级阻断，网络错误及 5xx 使用指数退避。
- A 股与 Crypto 分别保存周期 token；其中一个数据源失败不会造成另一个数据源在每次检查时被重复抓取。
- Binance 不提供市值和量比字段，因此 Crypto 不用代理数值冒充市值；只开放 24h ticker 实际提供的 `quoteVolume`、`volume`、`count` 和 `priceChangePercent` 排序。

### 数据配置接口

```http
GET  /api/v1/market-data/config
PUT  /api/v1/market-data/config?refresh=true
GET  /api/v1/market-data/status              # Web/调试完整状态
GET  /api/v1/market-data/status?compact=true # 硬件看板紧凑状态（无日志/报告/manifest）
POST /api/v1/market-data/refresh
GET  /api/v1/market-data/sectors?refresh=true
```

### 策略接口

```http
GET    /api/v1/strategies/catalog?compact=true
POST   /api/v1/strategies/sketches
DELETE /api/v1/strategies/sketches/{strategy_id}
POST   /api/v1/strategies/screen
```

`compact=true` 会省略保存策略的 128 个归一化点，供 ESP32 读取目录。筛选桶尚未准备好时返回 HTTP 202 / `DATA_BUILDING`，网页和手表可继续等待或取消本地等待；服务器建库任务不随客户端取消而中断。

## 设备接口

### 健康检查

```http
GET /api/v1/health
```

### 手绘匹配

```http
POST /api/v1/shape/match
Content-Type: application/json
```

```json
{
  "points": [[0.0, 0.2], [0.25, 0.7], [0.6, 0.3], [1.0, 0.8]],
  "category": "all",
  "timeframe": "1d",
  "limit": 10,
  "compact": true
}
```

`compact=true` 是 ESP32 模式：保留 Top-K、子分和最多 64 个缩略图点，省略设备不使用的 ShapeDTW 路径与诊断明细。服务端仍执行完整算法。

### K 线详情

```http
GET /api/v1/market/kline?symbol=BINANCE:BTCUSDT&tf=1d&from_ts=1710000000&to_ts=1711800000&limit=200
```

返回紧凑字段 `t/o/h/l/c/v`。硬件点击结果卡片时，会用该命中的 `window_start_ts/window_end_ts` 请求对应时间段，而不是在设备端重新计算或从全量行情中搜索。

## 测试

```powershell
cd server
python -m compileall -q core routers shape_search tests main.py start_all.py
python -m pytest tests -q
```

固件构建：

```powershell
cd ..
idf.py build
```

生成的固件位于 `build/M5ShapeSearch.bin`。

## 主要目录

```text
server/
├── main.py                          FastAPI 启动与 CPU 序列加载
├── start_all.py                     API + 统一 Web UI 及实时日志
├── server_web_ui.py                 7860 手绘、设置与运行看板
├── routers/shape_router.py          匹配、策略、K 线和市场数据配置接口
├── core/cpu_shape_search_manager.py 连续序列持久化和搜索适配
├── core/akshare_data_builder.py     A股/Crypto 选池与连续 K 线构建协调
├── core/binance_public_data.py      Binance 公共行情、权重限速与失败退避
├── core/market_data_service.py      启动预加载、周期刷新和原子替换
├── shape_search/                    NCC、Turning Point、ShapeDTW 引擎
├── strategy/                        Sequoia-X 预设、手绘持久化与集合筛选
├── data/cpu_shape_search/           连续行情 NPZ
└── tests/                           算法与持久化测试
```

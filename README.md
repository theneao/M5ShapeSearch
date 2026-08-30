# M5ShapeSearch

面向 M5Stack StopWatch（ESP32-S3）的手绘技术形态搜索系统。手表负责采集手绘轨迹、展示 Top-K 结果和单标的 K 线详情；FastAPI 服务负责行情缓存、多尺度特征提取与 CPU 精排。

## 当前范围

- 固件仅保留 `Shape Search` 与 `Settings` 两个入口，并针对 466×466 圆形屏幕设计交互。
- 左侧 A 键用于返回或取消，右侧 B 键用于确认或执行；结果页和详情页支持左滑返回。
- 支持 A 股与 Binance 公开行情，周期包括 `5m`、`15m`、`30m`、`1h`、`4h`、`1d`、`1w`。
- 搜索使用多尺度 NCC、导数 NCC、转折点结构与 ShapeDTW，特征和匹配计算集中在服务器完成。
- Wi-Fi、服务器地址、行情池和性能模式均可在手表设置中调整，并通过 NVS 持久化。

## 显示与动画

- LVGL 使用 48 行局部双缓冲，优先放在 ESP32-S3 内部 DMA 内存；最终画面仍由 AMOLED 帧缓冲合并脏区后提交。
- 首页不再使用超宽循环滚动容器，只对单个 200×200 图标做 48px 两段式位移，并在动画期间合并连续输入。
- 设置和 Shape Search 内部页面采用单帧切换，避免 466×466 QSPI 屏幕逐帧传输约 434KB 的全屏动画。
- Top10 与设置菜单使用局部纵向惯性滚动并关闭边缘弹性；K 线详情支持像素级跟手平移、松手吸附和比例缩放。
- 表盘动态错误与 A 股板块均使用英文安全文本；股票中文名称仍保存在服务器，但不会交给缺少中文字形的手表字体显示。
- `Smooth` / `Eco` 保留不同的局部动画节奏；主循环每轮让出 2 ms，避免业务循环持续抢占 LVGL 渲染任务。
- 固件版本由根目录 `CMakeLists.txt` 的 `PROJECT_VER` 统一生成，启动页、About 和串口启动信息保持一致。

## 快速启动

### 服务端

```powershell
conda activate shape-server
cd server
python .\start_all.py
```

默认端口：

- API：`http://127.0.0.1:8000`
- 手绘调试页：`http://127.0.0.1:7860`
- 数据与运行设置：`http://127.0.0.1:7861`

局域网硬件应使用启动终端输出的 `Hardware/LAN` 地址。完整说明见[服务端一键启动](docs/guides/服务端一键启动.md)。

服务部署不依赖代理。AKShare 主行情端点异常时会保留旧缓存并退避；首次没有 A 股缓存时，选池会切换新浪快照、日/周线会切换腾讯历史接口。AKShare 分钟线只有东财数据源，故障时按批次熔断并保留旧缓存，不会用日线伪造分钟线。

### 固件

```powershell
idf.py build
idf.py -p COM5 flash monitor
```

生成的主固件为 `build/M5ShapeSearch.bin`。ESP-IDF 版本和依赖说明见[快速开始](docs/guides/快速开始.md)。

## 私有配置

仓库不会保存 Wi-Fi 密码、API 密钥、行情缓存或测试索引。

首次本地编译前，可复制：

```powershell
Copy-Item main\apps\app_stock_selector\private_config.example.h `
  main\apps\app_stock_selector\private_config.h
```

然后在被 Git 忽略的 `private_config.h` 中填写可选的首次配网默认值。留空时可通过手表自身的 `M5Shape-*` 配置热点设置网络与服务器地址。

## 文档

- [文档总览](docs/README.md)
- [系统实施方案](docs/design/技术形态搜索系统实施方案.md)
- [CPU 形态搜索算法规范](docs/design/CPU形态搜索算法规范.md)
- [项目研发交接](docs/reports/项目研发交接.md)
- [服务端说明](server/README.md)

## 项目结构

```text
M5ShapeSearch/
├── main/                 ESP32-S3 固件与 LVGL 页面
├── server/               FastAPI、行情缓存、搜索引擎与 Web 页面
├── docs/                 设计、使用指南和项目报告
├── CMakeLists.txt
└── README.md
```

## 上游与许可

硬件基础工程源自 M5Stack `M5StopWatch-UserDemo`。本仓库保留原项目许可文件，并将上游远端保存为 `upstream`，便于后续同步。

# M5 Shape Search — Interface Design System

> 创建时间：2026-09-23；最近修改：2026-09-23
> 作用：定义服务器分析工作台的视觉、交互和内容规范，作为后续页面扩展的唯一设计基线。  
> 使用方式：新增或修改 Web 页面前先对照本文件；若引入新颜色、间距、圆角或交互模式，应先更新本文件。

## 1. Design direction

产品气质为 **Institutional Research / Quiet Precision**：像一套可信赖的机构级研究终端，而不是营销页或通用后台模板。

- 核心感受：克制、精确、安静、可信。
- 信息密度：中高；优先快速比较和决策，不牺牲扫描效率。
- 差异度：6/10；保留专业工具熟悉感，通过品牌顶栏、组件皮肤和数据表达建立辨识度。
- 动效强度：3/10；仅用于状态反馈、页签切换和进度变化。
- 信息密度：7/10；桌面优先，首屏同时容纳查询、图形和关键操作。
- 参考方法：采用 `awesome-design-md` 中 Linear 的精确层级、Coinbase 的金融可信感，并根据本项目的数据研究场景重新设计；不复制其品牌资产。

## 2. Visual principles

1. **一个主强调色**：交互统一使用研究蓝，不用紫色渐变或多彩按钮。
2. **层级来自排版与细线**：不依赖厚重阴影，不把每一块内容都做成悬浮卡片。
3. **结果优先**：走势图、候选结果和核心操作占据首屏；说明文字主动退后。
4. **数值可比较**：代码、周期、相似度和技术指标优先使用等宽数字。
5. **状态明确**：加载、成功、降级、错误使用文字与语义色共同表达，不能只靠颜色。

## 3. Tokens

### Color

| Token | Value | Use |
|---|---:|---|
| `canvas` | `#EDF0F4` | 浏览器外层背景 |
| `surface` | `#FFFFFF` | 主工作区 |
| `surface-subtle` | `#F6F8FA` | 次级区域、表头 |
| `ink` | `#101828` | 主文字 |
| `ink-secondary` | `#3D4A5C` | 辅助文字 |
| `ink-muted` | `#6B7789` | 标签、说明 |
| `hairline` | `#DFE4EA` | 边框与分隔线 |
| `hairline-strong` | `#C6CED8` | hover/active 边框 |
| `accent` | `#0F62FE` | 主操作、选中状态 |
| `accent-hover` | `#0043CE` | 主操作 hover |
| `accent-soft` | `#EDF4FF` | 选中背景 |
| `success` | `#12805C` | 成功/就绪 |
| `warning` | `#A86200` | 等待/降级 |
| `danger` | `#C43D4B` | 错误/破坏操作 |

### Typography

- 界面：`"Segoe UI Variable", "Segoe UI", "PingFang SC", "Microsoft YaHei", ui-sans-serif, sans-serif`
- 数值：`"IBM Plex Mono", "SFMono-Regular", Consolas, "Liberation Mono", monospace`
- 页面标题：28px / 700；区块标题：16px / 650；正文：14px / 400；标签：12px / 600。
- 禁止超大 Hero 标题、全大写长段文字和低对比度正文。

### Geometry

- 间距基线：4px；常用间距为 8 / 12 / 16 / 24 / 32。
- 输入与按钮：8px 圆角；主容器：12px 圆角；标签状态：6px 圆角。
- 阴影仅用于顶层工作面板：`0 1px 2px rgba(16, 24, 40, .04)`。
- 内容最大宽度：1540px；桌面双栏约 42% / 58%，窄屏自动单栏。

## 4. Components

- **Header**：36px 深色品牌标识、产品名、引擎与连接端点；保持 76px 商用应用顶栏高度。
- **Primary navigation**：水平页签，选中项使用蓝色下边线与深色文字。
- **Workbench**：输入与参数为左栏，图表与候选为右栏，表格单独占满宽度。
- **Button**：每组最多一个主按钮；取消和删除为语义危险色，普通刷新使用中性按钮。
- **Chart**：透明画布、浅灰网格、左对齐标题；手绘曲线用红色，候选主曲线用蓝色。
- **Table**：弱表头、细行分隔、数字等宽，hover 只做轻微底色变化。
- **Progress**：同时显示阶段、百分比、状态文案；成功、降级和错误分别使用语义色。
- **Empty/Error**：给出原因与下一步，不只显示 `Error` 或空白区域。
- **Strategy workbench**：策略目录独占一行；组合方式、市场、周期和数量使用 12 列响应式条件矩阵；主操作、取消和刷新组成独立命令栏；结果表与详情操作分区展示。

### Commercial finish

- Gradio 原生控件必须统一覆盖输入框、Radio、Checkbox、Slider、Accordion、Gallery 和 Dataframe 状态，不允许出现默认橙色或默认大圆角。
- 业务按钮只能通过 `app-button` 显式选择器定制，禁止使用 `button:not(...)` 等全局规则，以免污染 Dataframe、ImageEditor 和 Number 的内部控制按钮。
- 主面板使用白色实体表面、10px 圆角、细边框和两层低透明阴影；内部信息依赖间距分组，不继续嵌套卡片。
- 顶栏承担品牌识别，页面正文不再重复大尺寸产品标题。
- 表格、端点、百分比和技术指标使用等宽数字；交互标签使用系统无衬线字体。

## 5. Interaction and accessibility

- 所有可点击控件必须有清晰的 hover、active 与 `focus-visible` 状态。
- 默认过渡 140–180ms；进度宽度 320ms；禁止循环装饰动画。
- 遵守 `prefers-reduced-motion`，关闭非必要过渡。
- 正文和背景满足 WCAG AA 对比度；不能只靠红绿区分状态。
- 触控窄屏下操作目标不小于 40px，核心操作不因换行而隐藏。

## 6. Do not

- 不使用渐变、玻璃拟态、霓虹发光、夸张阴影或 AI 紫色。
- 不在按钮中使用 emoji；图标只在语义明确且风格统一时使用。
- 不为每段文字建立独立圆角卡片。
- 不把说明信息置于结果之前，不让装饰元素与数据争夺注意力。
- 不为“高级感”牺牲加载状态、错误原因、键盘焦点或移动端可用性。

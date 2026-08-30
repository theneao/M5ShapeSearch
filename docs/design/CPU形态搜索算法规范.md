# 股票手绘形态搜索：CPU-only 特征计算与检索实现规格

> 目标：用户手绘一段走势后，在股票历史 K 线中检索视觉/几何形态最相似的局部子序列。  
> 约束：仅使用 CPU 云服务器；允许查询耗时较高；不依赖 GPU；不为每一种窗口长度离线重复计算整套特征。  
> 推荐主方案：**Multi-scale NCC + Derivative NCC + Turning-Point Structure + Subsequence ShapeDTW Rerank**。

---

# 1. 系统目标

给定用户手绘查询：

\[
Q = (q_1,q_2,\ldots,q_m)
\]

以及某只股票的长时间序列：

\[
T=(t_1,t_2,\ldots,t_N)
\]

搜索目标不是要求一个固定长度窗口整体与 `Q` 相似，而是寻找：

\[
(s^*,e^*)=
\arg\max_{s,e}
S(Q,T_{s:e})
\]

其中：

\[
e-s+1
\]

允许与查询长度 `m` 不同。

系统必须支持：

- 不同绝对价格；
- 不同涨跌幅幅度；
- 不同形态持续时间；
- 局部时间伸缩；
- 用户手绘采样点与 K 线采样密度不同；
- K 线噪声明显高于用户手绘轨迹；
- 目标形态只占一个大窗口的一部分；
- 一个长序列中寻找任意局部匹配段。

---

# 2. 推荐总体架构

```text
用户手绘轨迹
      ↓
轨迹清洗 / 重采样 / 平滑
      ↓
Query Feature Builder
      ↓
Multi-scale NCC
      ↓
价格形状候选 Top-N
      ↓
Derivative NCC
      ↓
方向 / 斜率结构过滤
      ↓
Turning Point Structure Score
      ↓
保留 Top-M
      ↓
候选窗口向左右扩张
      ↓
Subsequence ShapeDTW
      ↓
自动寻找真实 start / end
      ↓
综合评分
      ↓
Interval NMS 去重
      ↓
Top-K
```

推荐 MVP 阶段不要加入：

- 神经网络；
- FAISS；
- GPU；
- 大规模 learned embedding；
- 大量技术指标。

第一阶段的核心任务是先把“**人眼觉得像**”的距离函数做好。

---

# 3. 数据输入定义

## 3.1 股票数据

最少字段：

```text
timestamp
open
high
low
close
volume
```

主形态搜索通道只使用：

```text
close
```

或者：

```text
typical_price = (high + low + close) / 3
```

推荐第一版：

```python
price = np.log(close)
```

使用 log-price 的原因：

\[
\log(P_t)-\log(P_{t-1})
\]

近似百分比收益率，因此不同价格水平的股票更加可比。

---

# 4. 离线数据预处理

## 4.1 Log Price

定义：

\[
p_t=\log(C_t)
\]

代码：

```python
log_price = np.log(close.astype(np.float64))
```

---

## 4.2 平滑序列

用户手绘轨迹天然比真实 K 线平滑，因此不能直接用原始 close 作为唯一比较对象。

推荐使用 Savitzky-Golay：

```python
from scipy.signal import savgol_filter

smooth_price = savgol_filter(
    log_price,
    window_length=7,
    polyorder=2
)
```

默认：

```yaml
smooth:
  method: savgol
  window_length: 7
  polyorder: 2
```

如果数据周期很短、噪声更大，可以测试：

```text
window = 5
window = 7
window = 9
window = 11
```

必须保证 window 为奇数。

---

# 5. Query 手绘轨迹预处理

用户输入一般是：

```text
[(x1,y1), (x2,y2), ...]
```

不能直接按鼠标事件点比较。

---

## 5.1 x 轴单调化

首先删除：

- 重复 x；
- 向后绘制造成的乱序点；
- 极近的重复点。

然后按 x 排序。

```python
points = sorted(points, key=lambda p: p[0])
```

---

## 5.2 按 x 轴均匀重采样

假设原始用户输入：

\[
(x_i,y_i)
\]

构造均匀：

\[
x'_j
=
x_{min}
+
j
\frac{x_{max}-x_{min}}{M-1}
\]

其中默认：

```yaml
query_resample_points: 128
```

使用线性插值：

```python
x_new = np.linspace(x.min(), x.max(), 128)
y_new = np.interp(x_new, x, y)
```

得到：

```text
Q_raw[128]
```

---

## 5.3 屏幕坐标翻转

Canvas 通常 y 越大位置越低，因此必须：

```python
q = -y_new
```

否则上涨走势会被理解为下跌。

---

## 5.4 Query 平滑

推荐：

```python
q_smooth = savgol_filter(
    q,
    window_length=7,
    polyorder=2
)
```

查询和数据库应该使用**近似一致的平滑逻辑**。

---

# 6. Z-Normalization

对于任意序列：

\[
X=(x_1,\ldots,x_n)
\]

均值：

\[
\mu_X
=
\frac1n\sum_{i=1}^{n}x_i
\]

标准差：

\[
\sigma_X
=
\sqrt{
\frac1n
\sum_{i=1}^{n}
(x_i-\mu_X)^2
}
\]

Z-normalization：

\[
Z(X)_i
=
\frac{x_i-\mu_X}
{\sigma_X+\epsilon}
\]

推荐：

```python
EPS = 1e-8
z = (x - x.mean()) / (x.std() + EPS)
```

这会消除：

- 绝对价格；
- y 轴平移；
- 大部分幅度差异。

例如：

```text
10 → 12 → 10

和

100 → 120 → 100
```

经过 normalization 后形态基本一致。

---

# 7. 特征 1：Price NCC

这是第一层最重要的 recall 特征。

给定 query：

\[
Q=(q_1,\ldots,q_m)
\]

候选窗口：

\[
X=(x_1,\ldots,x_m)
\]

Normalized Cross Correlation：

\[
NCC(Q,X)
=
\frac{
\sum_{i=1}^{m}
(q_i-\mu_Q)(x_i-\mu_X)
}{
m\sigma_Q\sigma_X
}
\]

取值：

\[
NCC\in[-1,1]
\]

解释：

```text
1.0     几乎完全相同
0.8     高度相似
0.5     中等相关
0       无明显相关
-1      上下翻转
```

注意：

**不要使用 abs(NCC)**。

因为：

```text
W 底
```

和：

```text
M 顶
```

不应该被视为相同形态。

---

# 8. NCC 与归一化欧氏距离关系

对长度为 `m` 的 z-normalized 序列：

\[
D^2
=
2m(1-\rho)
\]

其中：

\[
\rho=NCC
\]

因此：

```text
最大化 NCC
```

与：

```text
最小化 z-normalized Euclidean Distance
```

本质等价。

这也是 MASS / Matrix Profile 类方法可以高效进行 subsequence search 的原因之一。

---

# 9. Multi-scale NCC

这是解决“形态长度未知”的第一层方案。

不要离线存储：

```text
32 bar feature
33 bar feature
34 bar feature
...
300 bar feature
```

而是在 query 时使用少量尺度。

推荐默认：

```python
SEARCH_LENGTHS = [
    24,
    32,
    42,
    55,
    72,
    95,
    125,
    165,
    218,
    288,
]
```

也可以根据业务定义：

```yaml
length:
  min: 20
  max: 300
  scale_ratio: 1.30
```

生成：

\[
L_{k+1}
=
round(rL_k)
\]

其中：

\[
r\approx1.25\sim1.35
\]

---

# 10. Query 尺度转换

Query 首先标准化为 128 点。

对于搜索长度 `L`：

```python
q_L = scipy.signal.resample(q_128, L)
```

推荐优先使用：

```python
np.interp
```

避免 FFT resample 在边界产生振铃。

示例：

```python
def resample_1d(x, n):
    old_pos = np.linspace(0, 1, len(x))
    new_pos = np.linspace(0, 1, n)
    return np.interp(new_pos, old_pos, x)
```

---

# 11. 快速滑窗 NCC

不要 Python for-loop 每一个窗口。

目标是计算：

\[
NCC(Q,T_{i:i+m})
\]

对于：

\[
i=0,\ldots,N-m
\]

推荐使用 FFT convolution / MASS 思路。

核心：

\[
QT_i
=
\sum_{j=0}^{m-1}
q_jt_{i+j}
\]

可以通过卷积计算：

\[
QT
=
T * reverse(Q)
\]

FFT 复杂度约：

\[
O(N\log N)
\]

---

# 12. Rolling Mean / Std

对于窗口长度 `m`：

\[
S_i
=
\sum_{j=i}^{i+m-1}t_j
\]

\[
SS_i
=
\sum_{j=i}^{i+m-1}t_j^2
\]

利用 cumulative sum：

```python
cs = np.concatenate([[0], np.cumsum(T)])
cs2 = np.concatenate([[0], np.cumsum(T*T)])

sum_x = cs[m:] - cs[:-m]
sum_x2 = cs2[m:] - cs2[:-m]
```

均值：

\[
\mu_i=\frac{S_i}{m}
\]

方差：

\[
\sigma_i^2
=
\frac{SS_i}{m}
-\mu_i^2
\]

---

# 13. NCC 完整公式

假设 query 已经 zero mean：

\[
\sum q_i = 0
\]

且：

\[
\sigma_q
\]

已知，则：

\[
NCC_i
=
\frac{
QT_i
}{
m\sigma_q\sigma_i
}
\]

更加通用：

\[
NCC_i=
\frac{
QT_i-m\mu_q\mu_i
}{
m\sigma_q\sigma_i
}
\]

---

# 14. NCC 候选峰值提取

不要直接保留所有：

```text
NCC > threshold
```

因为同一个形态会产生大量相邻重叠窗口。

例如：

```text
start = 100
start = 101
start = 102
...
```

应该寻找局部 maxima。

推荐：

```python
from scipy.signal import find_peaks

peaks, props = find_peaks(
    ncc,
    distance=max(3, int(L * 0.25)),
    height=ncc_threshold
)
```

初始阈值：

```yaml
ncc:
  min_score: 0.55
```

每个 scale：

```yaml
top_per_scale_per_symbol: 20
```

不要在第一版使用非常严格的 `0.8`，否则 recall 会下降。

---

# 15. 特征 2：Derivative

纯价格 NCC 有时会把：

```text
轮廓类似
```

但：

```text
局部涨跌节奏不同
```

的序列排在前面。

因此加入一阶导数。

---

# 16. 一阶导数公式

最简单：

\[
d_t=x_t-x_{t-1}
\]

但推荐中央差分：

\[
d_t
=
\frac{x_{t+1}-x_{t-1}}{2}
\]

边界：

\[
d_0=x_1-x_0
\]

\[
d_{n-1}=x_{n-1}-x_{n-2}
\]

代码：

```python
d = np.gradient(x)
```

注意：

必须对**平滑后的序列**求 derivative：

```python
smooth
→
gradient
```

而不是：

```python
raw close
→
gradient
```

---

# 17. Derivative normalization

Derivative 也必须单独 normalization：

\[
d_z
=
\frac{
d-\mu_d
}{
\sigma_d+\epsilon
}
\]

不能直接沿用 price 的 mean/std。

---

# 18. Derivative NCC

计算：

\[
S_d
=
NCC(
D(Q),
D(X)
)
\]

其中：

\[
D()
\]

是一阶导数。

推荐第一阶段评分：

\[
S_{coarse}
=
0.65S_p
+
0.35S_d
\]

其中：

```text
S_p = price NCC
S_d = derivative NCC
```

默认：

```yaml
coarse_score:
  price_ncc_weight: 0.65
  derivative_ncc_weight: 0.35
```

后续通过人工数据调参。

---

# 19. 为什么暂时不推荐 Second Derivative NCC

二阶导：

\[
a_t
=
d_t-d_{t-1}
\]

或者：

\[
a_t=
x_{t+1}
-2x_t
+x_{t-1}
\]

它可以描述：

```text
凹 / 凸
峰 / 谷
曲率变化
```

但金融数据噪声会被差分放大。

因此 MVP：

```text
Price
+
Derivative
```

先做。

二阶导只作为后续实验。

---

# 20. 特征 3：Turning Points

这是推荐增加的轻量结构特征。

目标是让算法感知：

```text
峰 → 谷 → 峰
```

这种人眼非常重视的结构。

---

# 21. Turning Point 检测

对 smoothed normalized series：

```python
from scipy.signal import find_peaks
```

Peak：

```python
peaks, _ = find_peaks(
    x,
    prominence=prominence
)
```

Valley：

```python
valleys, _ = find_peaks(
    -x,
    prominence=prominence
)
```

---

# 22. Prominence

推荐相对 normalization 后振幅设置：

```yaml
turning_point:
  prominence: 0.25
```

因为 z-normalized 序列标准差约 1。

可以测试：

```text
0.15
0.20
0.25
0.35
```

---

# 23. Turning Point 表示

每个显著 turning point：

\[
P_k
=
(
\tau_k,
h_k,
type_k,
prom_k
)
\]

其中：

### 时间位置

\[
\tau_k
=
\frac{i_k}{n-1}
\]

范围：

\[
[0,1]
\]

### 高度

\[
h_k=x_{i_k}
\]

### 类型

```text
peak = +1
valley = -1
```

### prominence

\[
prom_k
\]

---

# 24. Turning Point Sequence

例如 W：

```text
peak
↓
valley
↓
peak
↓
valley
↓
peak
```

可以编码：

```python
types = [+1,-1,+1,-1,+1]
```

结构类型顺序非常重要。

---

# 25. Turning Point 匹配

推荐第一版使用 DP / DTW 匹配 turning-point sequence。

单点 cost：

\[
C(i,j)
=
w_t|\tau_i-\tau_j|
+
w_h|h_i-h_j|
+
w_p|prom_i-prom_j|
+
P_{type}
\]

其中：

\[
P_{type}
=
\begin{cases}
0,&type_i=type_j\\
\lambda,&type_i\ne type_j
\end{cases}
\]

初始：

```yaml
turning_point:
  time_weight: 0.35
  height_weight: 0.35
  prominence_weight: 0.15
  type_mismatch_penalty: 1.0
```

---

# 26. 简化版本 Turning Point Score

如果暂时不想写 DP，可以先实现：

### 数量差

\[
D_n
=
\frac{
|K_q-K_x|
}{
\max(K_q,K_x,1)
}
\]

### 类型序列相似度

可用 edit distance。

### 时间位置误差

对匹配点：

\[
D_t
=
\frac1K
\sum|\tau_i^q-\tau_i^x|
\]

### 高度误差

\[
D_h
=
\frac1K
\sum|h_i^q-h_i^x|
\]

最终：

\[
D_{turn}
=
0.2D_n
+
0.4D_t
+
0.4D_h
\]

转换成 similarity：

\[
S_{turn}
=
e^{-\alpha D_{turn}}
\]

推荐：

\[
\alpha=2
\]

---

# 27. 第一阶段候选综合评分

推荐：

\[
S_{stage1}
=
0.55S_p
+
0.30S_d
+
0.15S_{turn}
\]

默认：

```yaml
stage1:
  price: 0.55
  derivative: 0.30
  turning_point: 0.15
```

注意：

Turning Point 不一定需要对所有 NCC 窗口计算。

推荐：

```text
NCC Top 2000
↓
Derivative NCC
↓
Top 500
↓
Turning Point
↓
Top 100~200
```

---

# 28. 候选边界扩张

NCC 的窗口只是 anchor，不是最终结果。

假设：

```text
NCC candidate:
[start, start+L)
```

向左右扩展：

\[
pad
=
round(\beta L)
\]

推荐：

```yaml
boundary:
  context_ratio: 0.40
```

即：

```python
left = start - int(0.4 * L)
right = start + L + int(0.4 * L)
```

裁剪到股票序列合法范围。

示例：

```text
NCC 命中：

[1000,1080]

扩展：

[968,1112]
```

然后在：

```text
[968,1112]
```

中做 Subsequence ShapeDTW。

---

# 29. 为什么必须 Subsequence DTW

普通 DTW 比较的是：

\[
Q
\leftrightarrow
X_{完整窗口}
\]

而你的需求是：

\[
Q
\leftrightarrow
X_{某个局部子序列}
\]

所以应该：

\[
(s^*,e^*)
=
\arg\min_{s,e}
DTW(Q,X_{s:e})
\]

这样真实匹配：

```text
候选大窗口：
xxxxxxxx /\/\____/\/ xxxxxxxxx

真正结果：
        [ /\/\____/\/ ]
```

可以自动被找出来。

---

# 30. ShapeDTW 的核心思想

普通 DTW 单点比较：

\[
c(i,j)
=
|q_i-x_j|
\]

ShapeDTW 不只比较一个点，而是比较点附近的小窗口：

\[
s_i=
[
q_{i-r},
\ldots,
q_i,
\ldots,
q_{i+r}
]
\]

候选：

\[
u_j=
[
x_{j-r},
\ldots,
x_j,
\ldots,
x_{j+r}
]
\]

局部描述符距离：

\[
c(i,j)
=
\|f(s_i)-f(u_j)\|_2^2
\]

这样比较的是：

```text
这个点附近的走势结构
```

而不是：

```text
这个点的单独高度
```

---

# 31. 推荐 Shape Descriptor

为了 CPU 简单、可解释，推荐：

\[
F_i
=
[
y_i,
d_i,
\bar y_i^{(r)}
]
\]

其中：

```text
y_i        normalized level
d_i        derivative
mean/local smooth descriptor
```

也可直接使用长度为：

```yaml
shape_descriptor_radius: 3
```

的局部 patch：

\[
[
y_{i-3},
\ldots,
y_{i+3}
]
\]

并附加 derivative patch。

最终：

\[
F_i=
[
y_{i-r:i+r},
\lambda_d d_{i-r:i+r}
]
\]

推荐：

```yaml
shapedtw:
  descriptor_radius: 3
  derivative_weight: 0.50
```

---

# 32. Shape Descriptor normalization

每个局部 patch 不建议单独完全 z-normalize，否则可能破坏：

```text
峰比肩高
```

这样的全局关系。

推荐：

- 整个 sequence 先 normalize；
- local patch 不再独立做 z-score。

---

# 33. ShapeDTW Local Cost

定义：

\[
c(i,j)
=
w_y
\|Y_i-Y_j\|_2^2
+
w_d
\|D_i-D_j\|_2^2
\]

推荐：

\[
w_y=0.65
\]

\[
w_d=0.35
\]

或者：

```yaml
shapedtw:
  level_weight: 0.65
  derivative_weight: 0.35
```

---

# 34. DTW 动态规划

标准递推：

\[
DP(i,j)
=
c(i,j)
+
\min
\begin{cases}
DP(i-1,j)\\
DP(i,j-1)\\
DP(i-1,j-1)
\end{cases}
\]

---

# 35. Subsequence DTW 初始化

普通 DTW：

```text
DP[0,0] = 0
其他边界 = inf
```

Subsequence DTW 为允许 query 在 reference 任意位置开始：

\[
DP(0,j)=0
\]

即：

```python
dp[0, :] = 0
```

Query 必须完整匹配，因此：

```python
dp[:,0] = inf
dp[0,0] = 0
```

最终：

\[
j^*
=
\arg\min_j DP(m,j)
\]

得到最佳结束位置。

然后 backtracking 找：

\[
start
\]

---

# 36. DTW 距离归一化

不要直接使用：

```text
raw accumulated cost
```

因为路径越长 cost 越大。

推荐：

\[
D_{dtw}
=
\frac{
DTWCost
}{
PathLength
}
\]

然后变 similarity：

\[
S_{dtw}
=
e^{-D_{dtw}/\tau}
\]

推荐：

```yaml
dtw_similarity_temperature: 1.0
```

后续根据分布校准。

---

# 37. Warping Constraint

ShapeDTW 不应允许无限扭曲。

否则：

```text
简单上涨
```

也可能被过度拉伸匹配：

```text
复杂震荡上涨
```

推荐 Sakoe-Chiba band：

\[
|i/m-j/n|
<
r
\]

推荐：

```yaml
dtw:
  warping_ratio: 0.15
```

即允许约：

```text
±15%
```

局部时间偏移。

测试：

```text
0.10
0.15
0.20
0.25
```

---

# 38. 最终评分

ShapeDTW 完成后得到：

```text
S_price
S_derivative
S_turn
S_dtw
```

推荐最终：

\[
S_{final}
=
0.30S_p
+
0.20S_d
+
0.15S_{turn}
+
0.35S_{dtw}
\]

默认：

```yaml
final_score:
  price_ncc: 0.30
  derivative_ncc: 0.20
  turning_point: 0.15
  shapedtw: 0.35
```

如果后续发现 ShapeDTW 更接近人工判断，可增大：

```text
0.40 ~ 0.50
```

---

# 39. 更推荐的 Score Calibration

不同 score 不一定同分布。

例如：

```text
NCC:
0.5 ~ 0.95

DTW score:
0.2 ~ 0.8
```

不要长期直接线性混合原始值。

MVP 可以先使用线性组合。

正式版本建议对每个 score 做：

```text
Percentile calibration
```

或：

```text
Logistic calibration
```

再融合。

---

# 40. 多尺度候选去重

同一个形态会被多个尺度发现。

例如：

```text
64 bars  → [100,163]
80 bars  → [95,174]
100 bars → [88,187]
```

ShapeDTW refinement 后：

```text
[103,157]
[102,158]
[104,157]
```

应合并。

---

# 41. Interval IoU

定义：

\[
IoU(A,B)
=
\frac{
|A\cap B|
}{
|A\cup B|
}
\]

若：

\[
IoU>0.65
\]

认为属于同一结果。

推荐：

```yaml
nms:
  iou_threshold: 0.65
```

保留：

```text
score 最大
```

的结果。

---

# 42. Scale Consistency Bonus

如果一个形态在多个尺度均独立命中，可以增加置信度。

定义：

\[
S'
=
S
+
\eta
\log(1+n_{scale})
\]

推荐：

```yaml
scale_bonus_eta: 0.02
```

这个权重必须非常小，避免覆盖真实距离。

---

# 43. 推荐完整检索参数

```yaml
query:
  resample_points: 128
  smooth_window: 7
  smooth_polyorder: 2

search:
  min_length: 24
  max_length: 300
  scale_ratio: 1.30

ncc:
  min_score: 0.55
  top_per_scale_per_symbol: 20

derivative:
  enabled: true

stage1:
  price_weight: 0.55
  derivative_weight: 0.30
  turning_point_weight: 0.15

turning_point:
  prominence: 0.25
  time_weight: 0.35
  height_weight: 0.35
  prominence_weight: 0.15
  type_mismatch_penalty: 1.0

candidate:
  global_top_before_turning_point: 500
  global_top_before_dtw: 150

boundary:
  context_ratio: 0.40

shapedtw:
  descriptor_radius: 3
  level_weight: 0.65
  derivative_weight: 0.35
  warping_ratio: 0.15

final_score:
  price_ncc: 0.30
  derivative_ncc: 0.20
  turning_point: 0.15
  shapedtw: 0.35

nms:
  iou_threshold: 0.65

output:
  top_k: 20
```

---

# 44. 离线需要预计算什么

重点：

**不要预计算所有窗口特征。**

只需要保存连续序列。

推荐：

```text
data/
  daily/
    000001.SZ.npz
    000002.SZ.npz
    ...
```

每个文件：

```python
{
    "timestamp": timestamps,
    "log_price": log_price,
    "smooth_price": smooth_price,
    "derivative": derivative,
    "volume": volume
}
```

甚至 derivative 可以运行时计算。

---

# 45. 建议额外缓存

为了 CPU 搜索快，可以缓存：

```text
cumsum(price)
cumsum(price²)
FFT(price)
```

但不是必须。

基础版本：

```text
price
smooth_price
derivative
```

即可。

---

# 46. 推荐 Python 模块结构

```text
shape_search/
│
├── config.py
│
├── data/
│   ├── loader.py
│   ├── preprocess_market.py
│   └── storage.py
│
├── query/
│   ├── preprocess_sketch.py
│   └── resample.py
│
├── features/
│   ├── normalization.py
│   ├── derivative.py
│   ├── turning_points.py
│   └── descriptors.py
│
├── search/
│   ├── ncc.py
│   ├── multiscale.py
│   ├── candidate.py
│   └── nms.py
│
├── rerank/
│   ├── dtw.py
│   ├── subsequence_dtw.py
│   ├── shapedtw.py
│   └── scoring.py
│
├── benchmark/
│   ├── synthetic.py
│   ├── metrics.py
│   └── visualize.py
│
└── main.py
```

---

# 47. 核心数据结构

```python
from dataclasses import dataclass

@dataclass
class Candidate:
    symbol: str
    scale: int
    coarse_start: int
    coarse_end: int

    price_ncc: float
    derivative_ncc: float
    turning_score: float | None = None

    refined_start: int | None = None
    refined_end: int | None = None

    shapedtw_distance: float | None = None
    shapedtw_score: float | None = None

    final_score: float | None = None
```

---

# 48. QueryFeature

```python
@dataclass
class QueryFeatures:
    raw: np.ndarray
    smooth: np.ndarray
    normalized: np.ndarray
    derivative: np.ndarray
    turning_points: list
```

---

# 49. NCC 接口

```python
def sliding_ncc(
    query: np.ndarray,
    series: np.ndarray
) -> np.ndarray:
    """
    返回每个合法起点的 NCC。

    output shape:
        len(series) - len(query) + 1
    """
```

必须保证：

```text
query length <= series length
```

---

# 50. Multi-scale 接口

```python
def multiscale_ncc_search(
    query: np.ndarray,
    series: np.ndarray,
    lengths: list[int],
    top_per_scale: int
) -> list[Candidate]:
    ...
```

---

# 51. Turning Point 接口

```python
@dataclass
class TurningPoint:
    index: int
    time_norm: float
    height: float
    kind: int
    prominence: float
```

其中：

```text
kind = +1 peak
kind = -1 valley
```

---

# 52. ShapeDTW 接口

```python
def subsequence_shapedtw(
    query: np.ndarray,
    reference: np.ndarray,
    descriptor_radius: int = 3,
    warping_ratio: float = 0.15
) -> tuple[
    float,  # distance
    int,    # start
    int,    # end
    list[tuple[int,int]]  # warping path
]:
    ...
```

---

# 53. 整体搜索伪代码

```python
def search_shape(sketch, market_db, config):

    # --------------------------------
    # 1 Query preprocessing
    # --------------------------------

    q = preprocess_sketch(
        sketch,
        resample_points=128
    )

    q_smooth = smooth(q)

    q_norm = z_normalize(q_smooth)

    q_deriv = z_normalize(
        gradient(q_smooth)
    )

    q_turns = detect_turning_points(q_norm)

    # --------------------------------
    # 2 Multi-scale NCC recall
    # --------------------------------

    all_candidates = []

    lengths = geometric_lengths(
        min_length=24,
        max_length=300,
        ratio=1.30
    )

    for symbol in market_db.symbols:

        series = market_db[symbol].smooth_price

        for L in lengths:

            qL = resample(q_norm, L)

            ncc_price = sliding_ncc(
                qL,
                series
            )

            peaks = select_local_ncc_peaks(
                ncc_price,
                min_score=0.55,
                min_distance=int(L*0.25),
                top_k=20
            )

            for start in peaks:

                end = start + L

                all_candidates.append(
                    Candidate(
                        symbol=symbol,
                        scale=L,
                        coarse_start=start,
                        coarse_end=end,
                        price_ncc=ncc_price[start],
                        derivative_ncc=0.0
                    )
                )

    # --------------------------------
    # 3 Derivative NCC
    # --------------------------------

    for c in all_candidates:

        x = load_segment(
            c.symbol,
            c.coarse_start,
            c.coarse_end
        )

        qL = resample(
            q_deriv,
            len(x)
        )

        xd = z_normalize(
            gradient(x)
        )

        c.derivative_ncc = pearson(
            qL,
            xd
        )

    # preliminary rank

    for c in all_candidates:

        c.pre_score = (
            0.65 * c.price_ncc
            +
            0.35 * c.derivative_ncc
        )

    all_candidates.sort(
        key=lambda x: x.pre_score,
        reverse=True
    )

    all_candidates = all_candidates[:500]

    # --------------------------------
    # 4 Turning point rerank
    # --------------------------------

    for c in all_candidates:

        x = load_segment(
            c.symbol,
            c.coarse_start,
            c.coarse_end
        )

        x = z_normalize(x)

        x_turns = detect_turning_points(x)

        c.turning_score = turning_similarity(
            q_turns,
            x_turns
        )

        c.stage1_score = (
            0.55 * c.price_ncc
            +
            0.30 * c.derivative_ncc
            +
            0.15 * c.turning_score
        )

    all_candidates.sort(
        key=lambda x: x.stage1_score,
        reverse=True
    )

    dtw_candidates = all_candidates[:150]

    # --------------------------------
    # 5 Boundary expansion + ShapeDTW
    # --------------------------------

    results = []

    for c in dtw_candidates:

        L = c.scale

        pad = int(
            config.boundary.context_ratio
            * L
        )

        left = max(
            0,
            c.coarse_start - pad
        )

        right = min(
            market_db.length(c.symbol),
            c.coarse_end + pad
        )

        context = market_db[
            c.symbol
        ].smooth_price[left:right]

        context = z_normalize(context)

        distance, s, e, path = subsequence_shapedtw(
            query=q_norm,
            reference=context,
            descriptor_radius=3,
            warping_ratio=0.15
        )

        c.refined_start = left + s
        c.refined_end = left + e

        c.shapedtw_distance = distance

        c.shapedtw_score = np.exp(
            -distance
        )

        c.final_score = (
            0.30 * c.price_ncc
            +
            0.20 * c.derivative_ncc
            +
            0.15 * c.turning_score
            +
            0.35 * c.shapedtw_score
        )

        results.append(c)

    # --------------------------------
    # 6 interval dedup
    # --------------------------------

    results.sort(
        key=lambda x: x.final_score,
        reverse=True
    )

    results = interval_nms(
        results,
        iou_threshold=0.65
    )

    return results[:20]
```

---

# 54. 推荐优化：先粗尺度、后细尺度

如果全市场速度不够，不要立刻增加索引复杂度。

可以先：

```text
32
64
128
256
```

粗搜索。

如果某只股票：

```text
64  score = 0.85
128 score = 0.92
256 score = 0.60
```

只进一步搜索：

```text
80
96
112
144
160
```

这叫：

```text
coarse-to-fine scale search
```

可以显著减少 NCC 次数。

---

# 55. CPU 并行

最适合的并行粒度：

```text
symbol
```

每个 worker 负责一批股票。

推荐：

```python
ProcessPoolExecutor
```

或者：

```python
joblib.Parallel
```

避免大量细粒度任务。

---

# 56. 建议进程数

```python
workers = max(
    1,
    os.cpu_count() - 1
)
```

如果 NumPy / MKL 自己多线程，需要避免：

```text
进程数 × BLAS线程
```

导致线程爆炸。

部署时设置：

```bash
OMP_NUM_THREADS=1
MKL_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
```

然后使用多进程按股票并行。

---

# 57. 数据存储建议

CPU-only 单机 MVP 推荐：

```text
NumPy mmap
```

或：

```text
NPZ
```

如果股票数量大：

```text
Parquet + NumPy arrays
```

搜索核心不要频繁 DataFrame 操作。

计算阶段尽量使用：

```python
np.ndarray
```

---

# 58. 不推荐数据库层实时滑窗

不要：

```text
SQL SELECT
→
每一个窗口
→
Python
```

正确方式：

```text
数据库只负责 metadata
+
历史时间序列读取
+
NumPy 搜索
```

---

# 59. 成交量如何使用

第一版不要把 volume 加入主 shape distance。

否则：

```text
图形很像
但成交量不同
```

会被错误拉低。

推荐：

```text
price shape = primary
volume = optional rerank
```

例如：

\[
S
=
0.9S_{shape}
+
0.1S_{volume}
\]

并且只在用户开启：

```text
“同时匹配成交量”
```

时使用。

---

# 60. Volatility 辅助特征

可以计算：

\[
r_t
=
\log
\frac{C_t}{C_{t-1}}
\]

窗口 volatility：

\[
\sigma_r
=
std(r)
\]

但不要在 z-normalized shape 主分数里硬加入。

用途：

```text
filter / tag / secondary rerank
```

例如：

```text
形态相似
+
波动率环境相似
```

---

# 61. 可能出现的错误匹配

## 61.1 过度 Z-normalization

下面：

```text
3% 振幅 W
```

与：

```text
80% 振幅 W
```

可能获得高 shape score。

几何上这是合理的。

但金融语义不同。

解决：

单独保存：

```text
amplitude_ratio
volatility
ATR ratio
```

作为 metadata。

---

## 61.2 W 和复杂震荡

NCC 可能认为：

```text
W
```

和：

```text
很多小波动构成的大 W
```

很像。

Turning Point prominence 可以减少这个问题。

---

## 61.3 趋势漂移

如果某候选：

```text
W + 强上升趋势
```

而 query 是：

```text
水平 W
```

纯 z-normalization 后可能仍较像。

Derivative NCC 可以显著帮助。

---

## 61.4 DTW 过度扭曲

若 warping 不限制：

```text
非常宽左肩
+
非常窄右肩
```

也可能被 DTW 强行匹配。

因此必须有：

```yaml
warping_ratio: 0.10 ~ 0.20
```

---

# 62. 测试集必须包含哪些扰动

对同一个基准 pattern 生成：

## 时间尺度

```text
0.5x
0.75x
1x
1.5x
2x
3x
```

## 幅度

```text
0.5x
1x
2x
4x
```

## Noise

```text
0%
2%
5%
10%
```

## Context

目标只占：

```text
25%
50%
75%
100%
```

## 局部时间 warp

```text
5%
10%
20%
30%
```

---

# 63. 最重要的评估指标

不要只看：

```text
accuracy
```

这是 retrieval。

必须至少看：

\[
Recall@K
\]

\[
Precision@K
\]

\[
MRR
\]

以及边界：

\[
BoundaryIoU
\]

---

# 64. BoundaryIoU

真实：

```text
[100,150]
```

预测：

```text
[95,155]
```

IoU：

\[
IoU
=
\frac{
|[100,150]|
}{
|[95,155]|
}
\]

用于衡量：

```text
算法是否真的找到了正确局部范围
```

这是验证 Subsequence ShapeDTW 是否有效的重要指标。

---

# 65. 推荐开发阶段

## Phase 1

实现：

```text
Sketch preprocess
+
Fixed length NCC
```

验证基本形态。

---

## Phase 2

加入：

```text
Multi-scale NCC
```

验证不同长度。

---

## Phase 3

加入：

```text
Derivative NCC
```

观察误匹配下降。

---

## Phase 4

加入：

```text
Turning Point Score
```

提升视觉结构一致性。

---

## Phase 5

加入：

```text
Subsequence DTW
```

验证真实 start / end。

---

## Phase 6

升级：

```text
ShapeDTW
```

替代普通 point-wise DTW。

---

## Phase 7

建立 benchmark 和自动调权。

---

# 66. 必须做的消融实验

依次比较：

```text
A:
Price NCC

B:
Price NCC
+
Derivative NCC

C:
Price NCC
+
Derivative NCC
+
Turning Point

D:
Price NCC
+
Derivative NCC
+
Subsequence DTW

E:
Price NCC
+
Derivative NCC
+
Turning Point
+
Subsequence ShapeDTW
```

观察：

```text
Recall@20
Precision@20
MRR
BoundaryIoU
CPU Time
```

不要凭感觉判断哪个特征有效。

---

# 67. 推荐第一版最终方案

最终 MVP 固定：

\[
\boxed{
\text{Multi-scale Price NCC}
}
\]

↓

\[
\boxed{
\text{Derivative NCC}
}
\]

↓

\[
\boxed{
\text{Turning-Point Structure}
}
\]

↓

\[
\boxed{
\text{Subsequence ShapeDTW}
}
\]

↓

\[
\boxed{
\text{Interval NMS}
}
\]

这套结构具有：

```text
无需 GPU
无需训练
无需提前计算所有窗口
支持不同形态长度
支持局部匹配
支持自动边界定位
容易解释
容易调参
适合先做 MVP
```

---

# 68. 给 Vibecoding 工具的实现原则

在让 Codex / Claude Code 编写程序时，明确要求：

1. **每个算法模块必须独立实现和单元测试。**
2. 不允许一开始做复杂 Web UI。
3. 先完成纯 Python benchmark CLI。
4. 所有距离函数必须可视化。
5. 每次搜索必须输出每个子分数。
6. 不允许只输出一个“similarity”黑盒分数。
7. 必须保存 ShapeDTW warping path。
8. 必须输出 coarse candidate 和 refined candidate。
9. 所有参数放 YAML / dataclass，不允许硬编码。
10. 所有结果都可以离线复现实验。

---

# 69. 推荐 CLI

```bash
python -m shape_search.search \
    --query examples/query.json \
    --market daily \
    --min-length 24 \
    --max-length 300 \
    --top-k 20
```

输出：

```json
[
  {
    "symbol": "000001.SZ",
    "start": "2023-03-20",
    "end": "2023-06-09",
    "price_ncc": 0.91,
    "derivative_ncc": 0.83,
    "turning_score": 0.88,
    "shapedtw_score": 0.90,
    "final_score": 0.886
  }
]
```

---

# 70. 推荐 Debug 图

每个结果生成：

```text
debug/
  result_001.png
```

图片包括：

```text
左：
用户 sketch

右：
真实 K 线匹配子段

下面：
Z-normalized overlay

再下面：
Derivative overlay

再下面：
DTW warping path
```

这是调算法最重要的工具之一。

---

# 71. 最后一个设计原则

不要试图让第一层就完美。

正确职责分离：

```text
NCC：
“这里附近可能像”

Derivative：
“涨跌方向结构是不是一致”

Turning Point：
“主要峰谷结构是不是一致”

ShapeDTW：
“真正对应的是哪一段，并且局部节奏是否相似”
```

也就是说：

\[
\boxed{
Recall
\rightarrow
Structure Filter
\rightarrow
Boundary Refinement
\rightarrow
Precise Ranking
}
\]

而不是让一个距离函数同时负责所有事情。

---

# 72. 推荐默认实现版本 v0.1

```yaml
version: 0.1

algorithm:
  recall:
    name: multiscale_ncc

  rerank_1:
    name: derivative_ncc

  rerank_2:
    name: turning_point

  final:
    name: subsequence_shapedtw

query:
  resample_points: 128
  smooth_window: 7
  smooth_polyorder: 2

scale:
  min: 24
  max: 300
  ratio: 1.30

candidate:
  ncc_threshold: 0.55
  top_per_scale_per_symbol: 20
  before_turning_point: 500
  before_dtw: 150

turning_point:
  prominence: 0.25

dtw:
  descriptor_radius: 3
  warping_ratio: 0.15
  context_ratio: 0.40

weights:
  stage1:
    price_ncc: 0.55
    derivative_ncc: 0.30
    turning_point: 0.15

  final:
    price_ncc: 0.30
    derivative_ncc: 0.20
    turning_point: 0.15
    shapedtw: 0.35

dedup:
  interval_iou: 0.65

output:
  top_k: 20
```

---

# 73. 建议 Vibecoding 第一条任务 Prompt

可以直接把下面内容交给代码工具：

```text
请根据本项目 specification.md 实现一个 CPU-only 的股票手绘形态搜索 prototype。

第一阶段只实现：

1. sketch JSON 输入；
2. sketch uniform resample；
3. Savitzky-Golay smoothing；
4. Z-normalization；
5. 股票 close -> log-price；
6. market smooth series；
7. sliding normalized cross correlation；
8. multi-scale query search；
9. local peak candidate extraction；
10. 输出每个 candidate 的 symbol/start/end/scale/NCC；
11. matplotlib debug overlay。

暂时不要实现：
- Web UI
- Database
- DTW
- ShapeDTW
- Volume
- GPU
- Neural Network

要求：
- NumPy / SciPy；
- pytest 单元测试；
- 所有参数 Config dataclass；
- CLI 可运行；
- 提供 synthetic W/M/V pattern 测试数据；
- 输出 Top-20；
- 每一步保存可选 debug 数据；
- 对 sliding NCC 写 correctness test，与 brute-force Pearson 结果比较，误差 < 1e-6。
```

完成之后再给工具第二条任务：

```text
在现有项目上增加 Derivative NCC + Turning Point，
不要重写已有 NCC 模块。
```

最后再增加：

```text
Subsequence DTW
→
ShapeDTW
```

这样比一次让 vibecoding 工具实现整个系统可靠得多。

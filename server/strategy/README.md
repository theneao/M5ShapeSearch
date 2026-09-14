# strategy

创建时间：2026-09-15。

该目录负责服务器端策略目录、Sequoia-X 行情规则适配、手绘策略持久化，以及多策略交集/并集运算。行情读取统一来自 `CpuShapeSearchManager` 的已提交内存快照，不建立第二套行情数据库。

内置六个行情策略的判定条件源自 [sngyai/Sequoia-X](https://github.com/sngyai/Sequoia-X) 提交 `444c0db69ff36b46ef2b22ab265051d60c16029d`；上游 README 声明 MIT。这里只适配其确定性公式，不引入 baostock、SQLite 或飞书推送链路。

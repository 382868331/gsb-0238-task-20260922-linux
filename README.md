# paychan —— 有向支付通道的多路径可行分拆

离线、纯 Python 标准库的可复用算法库：在一个有向支付通道网络中，把一笔整数
收款需求 **D** 拆到至多 **3 条简单路径**上，在满足每条有向边余额的前提下，
求**源总支付最低**的分拆方案，并输出逐跳账单与不可行瓶颈定位。

## 运行环境

- Python **3.14.7**（仅使用标准库；3.10+ 的语法即可运行，但以 3.14.7 为准）。
- **无需安装任何第三方依赖**，不需要网络、账号、密钥、数据库、中间件、云 API
  或 Docker；核心算法、测试、演示全部离线、确定性。

## 准确命令

```bash
# 必要测试（43 个用例）
python -m unittest discover -s tests -v

# 固定输入演示（真实计算，正常结果 + 不可行瓶颈 + 一个实际触发的拒绝边界）
python demo.py
```

两者均可在仓库根目录直接执行；`demo.py` 实测耗时约 0.04 秒（远低于 8 秒）。

## 问题契约与费用口径

- 网络至多 **8 个节点、16 条有向边**；不支持平行边（同一对 `(src,dst)`
  两条边报 `DuplicateEdgeError`），允许同一对节点之间方向相反的两条边。
- 每条边有整数余额 `balance`、固定费 `base_fee = b ≥ 0`、比例费
  `ppm = r ≥ 0`（整数，百万分比）。
- 一条边向下游**转发 x 单位**时收取费用
  `fee(x) = b + ceil(x·r / 1_000_000)`（全程整数精确计算，ceil 用
  `(x·r + 999_999) // 1_000_000`）。
- 沿路径 `e₁,…,eₖ` 交付 `a`：从目的端反向递推，边 `eᵢ` 的**实际发送额**
  （转发额）为 `xᵢ`，`xₖ = a`，`xᵢ = xᵢ₊₁ + bᵢ + ceil(xᵢ₊₁·rᵢ/10⁶)`；
  源总支付为 `x₁ + b₁ + ceil(x₁·r₁/10⁶)`。
- **余额约束的是每条边的实际发送额（转发额）`xᵢ ≤ balance`**：费在该跳
  内部结算（上游多付、该跳自留），不额外占用通道余额。多条路径共用一条边时，
  各路径在该边上的转发额**累计**不得超过余额。
- 交付额为非负整数，`0 ≤ D ≤ 100`；至多使用 3 条节点不重复的简单路径；
  交付 0 的路径不计费、不产生固定费，输出中省略。

## 优化目标（完全的、可复现的平局裁决）

在全部可行分拆中按以下顺序取最小：

1. **源总支付** `Σ source_paid` 最低；
2. **使用路径数**最少（仅计交付额 > 0 的路径）；
3. 活跃路径按节点标签序列字典序排序后，整条 **(路径, 交付额) 序列**字典序最小
   ——同一路径集合、不同金额且总成本相同的平局也由此唯一裁决。

节点标签支持 `int` / `str` 等可哈希键（拒绝 `bool` / `float` 标签）；
跨类型标签按类型分组后再按值比较（int 与 str 不做隐式转换）。

## 接口

```python
from paychan import Edge, Network, split_payment

net = Network([
    Edge("S", "A", balance=60, base_fee=1, ppm=2000),
    Edge("S", "B", balance=60, base_fee=0, ppm=5000),
    Edge("A", "R", balance=28, base_fee=1, ppm=0),
    Edge("B", "R", balance=30, base_fee=0, ppm=1000),
    Edge("R", "T", balance=100, base_fee=1, ppm=3000),
])
result = split_payment(net, "S", "T", 50)
```

`Edge` / `Network` 也接受 dict（`{"src":..., "dst":..., "balance":..., ...}`）
与 tuple（`(src, dst, balance[, base_fee[, ppm]])`）形式。

返回值（二者之一）：

- `SplitResult`：`plans: tuple[PathPlan, ...]`（按路径字典序排列）、
  `source_total_paid`、`amount_delivered`、`total_fee`、`paths_used`、
  `edge_usage()`（`{edge_index: 累计转发额}`）。
  每个 `PathPlan` 含 `nodes`、`edge_ids`、`deliver`、`source_paid` 与
  `hops: tuple[HopBill, ...]`；`HopBill` 字段为
  `edge_index, src, dst, amount（转发额）, fee, inflow（=amount+fee）,
  balance_used（=amount）`，逐跳首尾相接。
- `InfeasibleResult`：`reason` 为 `"no_path"`（源在有向图上不可达目的）或
  `"insufficient_capacity"`（有路但余额在 ≤3 路径内无法承载 D）；
  `bottlenecks` 给出以边余额为容量松弛求出的 s-t 最小割上的瓶颈边
  （含本边余额、割总容量、需求、缺口）。松弛无法见证缺口时（如纯粹受
  3 路径上限限制）瓶颈列表为空——不可行结论本身来自主搜索的完整穷举。

异常层级：`PaymentSplitError` 为公共基类。

- `InvalidInputError(locator, message)`：非法输入，`locator` 是可定位字段
  （如 `"amount"`、`"edges[2].ppm"`、`"destination"`）。子类：
  `NetworkLimitError`（超过 8 节点 / 16 边 / D>100）、
  `UnknownNodeError`、`DuplicateEdgeError`。
- `SearchBudgetExhausted`：**搜索预算耗尽**（默认 5,000,000 次
  DFS 步/组合/叶子评估，可用 `budget=` 调整）。此时既未证明最优、
  也未证明无解，调大预算可重试——与返回 `InfeasibleResult`
  （已完整穷举、**证明无解**）严格区分，二者绝不混淆。

输入校验语义：数值输入拒绝 `NaN` / `±Infinity`；所有整数参数拒绝 `bool`
（`True/False` 不静默当 1/0）；非整数 `float` 拒绝（不做静默舍入），
恰为整数的 `float`（如 `3.0`）接受。非法输入一律报错，不静默纠正。

辅助公开接口：`simple_paths(network, source, destination, *, budget=...)`
返回按字典序排列的全部简单路径 `[(nodes, edge_ids), ...]`。

## 算法与设计取舍

1. DFS 枚举全部简单 s-t 路径（节点不重复）；对每条路径在 `[0, D]` 上二分
   求单边可行最大交付额（可行性对金额单调），预计算 `cost[a]` 与每跳转发额表。
2. 枚举 1..3 条路径的组合与和为 D 的**正整数**分配（更少路径由更小的
   组合规模覆盖），逐跳累计共享边转发额并校验余额。
3. 剪枝：组合容量和不足；后续路径容量上界不足；`(费用下界, 路径数)`
   元组支配剪枝（费用下界 = 已付 + 剩余交付单位 + 剩余路径固定费，ppm 部分 ≥ 0）；
   一旦得到费用下界 D 且仅 1 路径的方案即提前终止。
4. 不可行诊断用带残量反向边的 Edmonds–Karp 求余额松弛 s-t 最小割。
   该松弛是必要条件（忽略逐跳费叠加、ceil 取整与 3 路径上限），仅用于
   **定位**，不作为不可行证明。

## 误差口径

**库内不存在浮点金额或浮点费率**：费用、递推、余额、源支付全部是 Python
任意精度整数的精确运算，不存在舍入误差或容差。唯一与 `float` 的接触点是
输入边界：拒绝 `NaN/±Infinity`、拒绝非整数 `float`；接受整数型 `float`
时先取精确整数值。因此 README 无需声明任何数值容差——不存在需要容差掩盖
的边界。

## 测试与独立参考

- `tests/test_validation.py`：NaN/Infinity、bool、非整数 float、负数、
  越界（8/16/100/3）、重复边、自环、未知节点、预算非法，以及预算耗尽与
  确定结论可重试的区分。
- `tests/test_scenarios.py`：共享瓶颈累计、固定费抑制拆分、容量强制拆分、
  ceil 取整各边界点、无路（含方向相反）、逐跳账单链式恒等、余额按转发额
  而非含费 inflow 校验、3 路径上限、字典序与最少路径平局。
- `tests/test_api.py`：三种边构造形式（Edge/dict/tuple）、只读视图、
  公开 `simple_paths` 工具、`feasible` 标志与异常层级。
- `tests/test_reference.py` / `tests/test_invariants.py`：对 10 个固定网络
  在小 D（0..20）与 D∈{25,50,75,100} 上逐值核对，并用固定种子随机网络
  （200 个入库用例）与**独立参考实现** `tests/reference.py` 交叉核对：
  可行性/原因、最优源支付、路径与金额分配、每边占用；同时核对逐跳账单
  链式恒等等结构不变量。独立参考只使用标准库、**不导入 `paychan`**，
  以另一套直白递归穷举独立给出期望值；往返一致性仅作补充，不作为期望值来源。

## 已知限制

- 只实现本题契约的数学子集：不模拟支付通道的时序状态、HTLC 失败/重试、
  流动性双向转换、时间锁或加密细节；不宣称兼容任何行业协议（如 BOLT）。
- 不支持平行边；不做网络连接，不持久化任何状态；`split_payment` 是
  纯函数式计算。
- 穷举搜索的规模上限按契约固定（8 节点 / 16 边 / D≤100 / 3 路径）；
  超限直接拒绝。上限内最坏结构实测亚秒级；预算是硬性安全阀，耗尽报
  `SearchBudgetExhausted`（结论未知），不会把未证明的结果当作答案返回。

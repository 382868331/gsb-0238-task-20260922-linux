# mp_split — 有向支付通道的多路径可行分拆

离线、确定性、纯整数的支付通道分拆求解库：给定有向通道网络（每边有
余额、基础费 `b`、整数 ppm 比例 `r`、每边锁定增量 `lock_delta`）与
收款金额 `D`，在至多 3 条简单路径上求非负整数分拆，使**源总支付最小**，
并返回逐跳账单（含逐跳到期）、每条边的累计占用与每条路径的总锁定期；
可按**付款人最大期限**过滤锁定期过长的路径；无解时返回可定位的瓶颈
诊断。不连接任何网络。

- **Python 版本**：Python 3.14.7（仅用标准库；3.10+ 应也可运行，未逐一验证）
- **依赖**：无第三方依赖，无需安装任何包
- **网络/密钥/数据库/中间件/Docker**：均不需要

## 费用模型（本题契约的数学子集）

在一条边向下游转发实际金额 `x` 时：

```
fee(x) = b + ceil(x * r / 1_000_000)
下游收到 = x - fee(x)
```

- 边的**余额按实际发送额 `x` 占用**（不是按交付额）。
- 路径金额由收款端向源端反向递推。给定本边下游要收到 `d`，最小
  实际发送额有闭式解（`0 <= r < 1_000_000`）：

  ```
  x* = max(b + d, ceil((b + d) * 1_000_000 / (1_000_000 - r)))
  ```

  推导见 `mp_split/fees.py`；当 `r >= 1_000_000` 时任何正金额都
  无法通过该边交付（比例费至少吞掉全部增量），该路径不参与求解。
- `x = 0`（边未被使用）时费用为 0。

## 锁定增量与到期模型

每条边除余额/费用外还可带非负整数 **`lock_delta`**（默认 `0`）。对
路径上的边 `e_0`（源端首跳）… `e_{m-1}`（收款端末跳），第 `j` 跳的
付款人**到期**为该跳（含）到末跳各边增量之和，即与金额同一方向
**沿路径反向累计**：

```
expiry_j   = lock_delta(e_j) + lock_delta(e_{j+1}) + ... + lock_delta(e_{m-1})
expiry_{m-1} = lock_delta(e_{m-1})          # 末跳到期 = 末边自己的增量
lock_total = expiry_0                       # 源端（付款人）总锁定期
```

- 到期只依赖路径拓扑与边属性，**与分拆金额无关**，故不进费用优化，
  只做路径资格过滤。
- `solve(..., max_lock=L)` 时，仅 `lock_total <= L` 的路径参与组合
  搜索。期限是**逐路径**约束：多路径分拆中每条路径单独满足，**不跨
  路径相加**（与余额按实际发送额跨路径累计不同）。等号允许
  （`lock_total == L` 合法）。
- `max_lock=None`（默认）或省略时不过滤，旧调用行为完全不变。
- 所有余额可行路径都被期限过滤时，返回
  `bottleneck.kind == "lock_limit_exceeded"`，`detail` 给出可达的
  最小总锁定期；这与余额不足（`insufficient_balance`）、无路
  （`unreachable`）严格区分。

## 输入上限（接口边界）

| 项目 | 上限 |
|---|---|
| 节点数 | 8 |
| 有向边数 | 16 |
| 收款金额 D | `0 <= D <= 100` 整数 |
| 简单路径数 | 至多 3 条 |
| 余额 / b / r / lock_delta | 非负整数 |

这是本题的有限协议/数学子集，**不宣称**与任何真实支付网络行业标准
完整兼容（不实现 HTLC 超时、流动性探测、并发更新、信道状态机等）。

## 接口

```python
from mp_split import Edge, Network, solve

net = Network(
    [
        Edge(src=0, dst=1, balance=40, base_fee=1, ppm=0, lock_delta=2),
        Edge(src=1, dst=2, balance=40, base_fee=2, ppm=5_000, lock_delta=3),
        # ...
    ],
    node_count=None,          # 默认按边引用的节点推导；可显式给 1..8
)

result = solve(net, source=0, destination=2, delivery=60,
               max_paths=3, budget=20_000_000, max_lock=10)
```

`solve` 参数：

- `network: Network`，`source/destination`：节点原始编号（必须不同）；
- `delivery: int`，`0 <= D <= 100`；
- `max_paths`：`1..3`，默认 3；
- `budget`：搜索评估上限（正整数），默认 20,000,000；
- `max_lock`：付款人最大期限，非负整数或 `None`（默认，不限制）；
  只允许 `lock_total <= max_lock` 的路径参与分拆，逐路径过滤、等号
  允许。

### 返回值 `SplitResult`（不可变值对象）

| 字段 | 含义 |
|---|---|
| `feasible` | 是否存在可行分拆 |
| `source_total` | 最小源总支付（不可行为 `None`） |
| `used_paths` | 使用的路径数 |
| `paths` | `PathBill` 元组：`path`（原始节点编号）、`delivered`、`source_paid`、`hops`、`lock_total` |
| `paths[i].lock_total` | 该路径源端总锁定期（各边 `lock_delta` 之和） |
| `paths[i].hops` | `HopBill` 元组，按转发顺序：`edge / delivered / fee / sent / expiry`，满足 `sent - fee == delivered`；首跳 `sent == source_paid`、`expiry == lock_total`，末跳 `delivered` 为该路径交付额、`expiry` 为末边增量 |
| `edge_usages` | `EdgeUsage`：每条被占用边跨路径累计的 `delivered_total / fee_total / sent_total` 与涉及路径下标 |
| `bottleneck` | 不可行时的 `Bottleneck(kind, detail)` |
| `evaluations` | 搜索评估计数（可观测性，不是优化目标） |
| `max_lock` | 本次求解生效的付款人最大期限回显（`None` 为不限） |

### 优化优先级（字典序目标）

1. 源总支付最小；
2. 在此前提下使用路径数最少（更便宜的 3 路径会击败更贵的 1 路径）；
3. 再按路径节点序列的字典序（简单路径按节点元组排序，组合按索引
   升序枚举，等值保留最先出现者）。

### 错误语义

- 所有本库异常继承 `mp_split.MPSplitError`。
- `NetworkValidationError`：输入不满足契约。信息包含可定位字段，如
  `edges[2].balance`、`edge (0->1).ppm`、`source`。规则：
  - 整数参数拒绝 `bool`（`True/False` 虽是 `int` 子类也拒绝）；
  - 拒绝 `NaN / +Inf / -Inf`；`float` 一律拒绝（不静默取整，必须显式传 `int`）；
  - 拒绝负数余额/费用/锁定增量、自环、重复有向边、越界节点引用、超规模输入；
  - `solve` 拒绝 `D<0 / D>100`、`max_paths` 越界、`budget<=0`、
    `max_lock<0`（非 `None` 时必须为非负整数）、端点不存在或相同。
  - 求解是无状态的纯函数，不存在“部分变更”；非法输入在任何计算前抛出。
- `SearchBudgetExhausted`：搜索评估数超过 `budget`。**这是“算不动”，
  不是“无解”**——与 `feasible=False` 的“已完整穷举证明无解”严格分开。
  在题面上限与默认预算下不会发生（实测最坏对抗网络约 18.3 万次评估）；
  该参数主要用于隔离测试与防御。

### 无解诊断

- `bottleneck.kind == "unreachable"`：源到收款端无任何简单路径。
- `bottleneck.kind == "insufficient_balance"`：存在满足期限的路径但
  余额（按实际发送额计）不足；`detail` 给出“至多可交付 X of D”的
  精确上界，例如 `deliverable at most 74 of 100 with up to 3 simple
  paths; ...`（设了 `max_lock` 时附加上限说明）。
- `bottleneck.kind == "lock_limit_exceeded"`：存在余额可行的路径，
  但每条的源端总锁定期都超过 `max_lock`；`detail` 给出可达的最小
  总锁定期，例如 `... minimum achievable lock total is 6 (lock deltas
  accumulate backwards along the path; limit is per-path and equality
  is allowed)`。期限不过滤时不产生该诊断。

诊断优先级：`unreachable`（拓扑无路）→ `lock_limit_exceeded`
（余额可行路径全部超期）→ `insufficient_balance`（期限内的路径余额
不足）。

## 设计取舍

1. **精确整数，不用浮点。** 费用递推、取整、余额比较全部用 Python
   `int`；比例费闭式解由不等式 `x - b - ceil(xr/M) >= d` 精确化为
   `x*(M-r) >= (b+d)*M` 导出。
2. **先枚举简单路径，再搜分拆。** 8 节点 16 边的 s→t 简单路径数实测
   上界约 45（对全部 16 边子集爬山搜索得到），穷举路径组合可行：
   路径组合数至多 `C(45,1)+C(45,2)+C(45,3) ≈ 1.5 万`。
3. **固定路径组合上做整数分拆搜索**，配两条可证安全的剪枝：
   - 后缀最小费用下界（路径对表惰性计算并在组合间缓存）；
   - 后缀每条共享边的最小累计实发额（逐边独立标量 DP，可加，故
     不会高估资源需求而错误剪枝）。
   全局当前最优费用作为严格 cutoff 跨路径数层传递。
4. **共享边按各路径实际发送额累计**校验；不经过某紧边的路径不受该
   边约束（诊断递归对此显式处理）。
5. **同费用同分拆的确定性 tie-break**：固定组合内，费用相同时取
   分拆向量字典序最大者（搜索按分量降序枚举，首个最优即定）。这是
   题面未完全规定处的确定性补充，不影响三级公开优先级。
6. **无解与预算分离**：只有搜索树被完整剪枝/遍历后才返回
   `feasible=False`；预算触发抛异常。
7. **期限是逐路径硬过滤，不是资源约束。**到期与分拆金额无关，因此
   不进入费用搜索与余额累计：在路径枚举后直接按 `lock_total <=
   max_lock` 过滤，余额搜索逻辑完全不变。锁定增量沿路径反向累计
   （与金额反向递推同方向），逐跳到期随账单一次算出；所有可行路径
   都被过滤光时给独立的 `lock_limit_exceeded` 诊断及可达最小总期限。

## 已知限制

- 仅实现本题契约：并行单边、同一有序节点对多边、时序/手续费动态
  变化、失败回滚、概率流动性等均不支持。
- 锁定增量只建模为“沿路径反向累计的逐跳到期 + 付款人总期限过滤”：
  逐路径判定、不跨路径相加、与金额无关；不实现 CLTV 余量差、逐跳
  最小增量、超时状态机或失败场景的备用分拆（本轮迭代明确不交付）。
  `lock_delta` 只要求非负整数，不设数值上限。
- 路径上限 3、D 上限 100、8 节点 / 16 边是硬边界，超限直接报错。
- 最坏对抗构造（高密度路径图且多紧边、恰不可行）在默认预算下实测
  约 2.8 秒、约 18 万次评估；常规网络为毫秒级。默认预算 2 千万次
  评估在题面上限内不会被打穿；若被打穿会抛 `SearchBudgetExhausted`
  而非给出错误的无解结论。
- 节点编号允许不连续，内部压缩为连续编号；公开账单始终使用原始编号。

## 误差口径

**库内不存在浮点计算，因此没有浮点误差/容差**：所有金额、费用、
取整、余额比较都是精确整数等式/不等式。测试中的边界断言使用精确
相等（例如 `x-1` 恰好不够、整除时不多收 1），不用宽容差掩盖边界。

## 运行

执行测试（必要测试，含与独立参考实现的穷举交叉验证）：

```bash
python -m unittest discover -s tests -v
```

执行演示（真实计算；约 0.04 秒，远低于 90 秒；不使用 sleep 或预录
结果）：

```bash
python demo.py
```

演示打印：一个 D=60 的成功分拆（逐跳账单 + 每边余额核对）、一个
D=100 的已证明无解瓶颈、一个 NaN 余额被拒绝的异常边界，以及锁定
增量三例：`max_lock` 把免费长锁路径过滤成收费短锁路径（含逐跳到期
手算核对）、期限等号放行与再小 1 被拒（`lock_limit_exceeded`）、
`max_lock=-1` 的参数拒绝。

## 目录

```
mp_split/
  __init__.py     公开接口导出
  errors.py       MPSplitError / NetworkValidationError / SearchBudgetExhausted
  model.py        Edge(含 lock_delta) / Network / HopBill(含 expiry) /
                  PathBill(含 lock_total) / EdgeUsage / SplitResult(含 max_lock)
  fees.py         费用公式与最小实发额闭式解（纯整数）
  solver.py       简单路径枚举、期限过滤、分拆搜索、瓶颈诊断
tests/
  test_fees.py            费用数学与取整边界
  test_validation.py      输入校验（bool / NaN / 规模 / 引用）
  test_solver.py          共享瓶颈、固定费抑制拆分、无路、预算等场景
  test_locks.py           锁定增量/反向累计到期/期限过滤（手算 + 穷举对拍）
  test_cross_validate.py  对独立参考的穷举交叉验证
  reference.py            独立参考实现（不导入被测核心，支持 lock_delta）
demo.py                   固定输入演示
```

## 验证手段说明

`tests/reference.py` 是**独立**参考：自带路径 DFS、自带倍增+二分的
费用反推（形态不同于被测闭式解）、穷举 1..3 路径的全部正整数
分拆，并独立计算逐跳账单、最大可交付额、路径总锁定期与逐跳到期
（后缀和直算）。它**不导入 `mp_split` 的求解代码**。交叉测试逐字段
比对费用、路径序列、分拆向量、逐跳账单、不可行上界与期限诊断；
`test_locks.py` 对 220 个确定性随机网络 × 7 种 `max_lock` 取值做
穷举对拍；账单的往返自洽（`sent - fee == delivered`、首跳到期
`== lock_total` 等）只作补充断言。此外另有小 D 的密集图穷举对拍与
8 节点 16 边 D≤100 的上限规模自洽 fuzz（开发期运行，非交付测试的
必需部分）。

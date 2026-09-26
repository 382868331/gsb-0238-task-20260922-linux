# mp_split — 有向支付通道的多路径可行分拆

离线、确定性、纯整数的支付通道分拆求解库：给定有向通道网络（每边有
余额、基础费 `b`、整数 ppm 比例 `r`）与收款金额 `D`，在至多 3 条
简单路径上求非负整数分拆，使**源总支付最小**，并返回逐跳账单与每条
边的累计占用；无解时返回可定位的瓶颈诊断。不连接任何网络。

增量能力：**循环付款规划**（`solve_cycle`）——从指定节点出发，经过
至少两条边回到自身（首边减余额、末边增余额的通道再平衡），给定最终
收回金额，求**源端实际支出最小**的简单环，逐跳返回实发/转发/费用；
规划是只读的，不修改网络余额。

- **Python 版本**：Python 3.14.7（仅用标准库；3.10+ 应也可运行，未逐一验证）
- **依赖**：无第三方依赖，无需安装任何包
- **网络/密钥/数据库/中间件/Docker**：均不需要

## 环境准备（隔离 venv）

不需要任何第三方包，隔离虚拟环境只是为了与系统 Python 隔离：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m unittest discover -s tests -v   # 跑全部测试
python demo_iteration.py                  # 循环付款演示（< 90 秒，实测 < 0.1 秒）
python demo.py                            # 多路径分拆演示
```

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

## 输入上限（接口边界）

| 项目 | 上限 |
|---|---|
| 节点数 | 8 |
| 有向边数 | 16 |
| 收款金额 D | `0 <= D <= 100` 整数 |
| 简单路径数 | 至多 3 条 |
| 余额 / b / r | 非负整数 |

这是本题的有限协议/数学子集，**不宣称**与任何真实支付网络行业标准
完整兼容（不实现 HTLC 超时、流动性探测、并发更新、信道状态机等）。

## 接口

```python
from mp_split import Edge, Network, solve

net = Network(
    [
        Edge(src=0, dst=1, balance=40, base_fee=1, ppm=0),
        Edge(src=1, dst=2, balance=40, base_fee=2, ppm=5_000),
        # ...
    ],
    node_count=None,          # 默认按边引用的节点推导；可显式给 1..8
)

result = solve(net, source=0, destination=2, delivery=60,
               max_paths=3, budget=20_000_000)
```

`solve` 参数：

- `network: Network`，`source/destination`：节点原始编号（必须不同）；
- `delivery: int`，`0 <= D <= 100`；
- `max_paths`：`1..3`，默认 3；
- `budget`：搜索评估上限（正整数），默认 20,000,000。

### 返回值 `SplitResult`（不可变值对象）

| 字段 | 含义 |
|---|---|
| `feasible` | 是否存在可行分拆 |
| `source_total` | 最小源总支付（不可行为 `None`） |
| `used_paths` | 使用的路径数 |
| `paths` | `PathBill` 元组：`path`（原始节点编号）、`delivered`、`source_paid`、`hops` |
| `paths[i].hops` | `HopBill` 元组，按转发顺序：`edge / delivered / fee / sent`，满足 `sent - fee == delivered`；首跳 `sent == source_paid`，末跳 `delivered` 为该路径交付额 |
| `edge_usages` | `EdgeUsage`：每条被占用边跨路径累计的 `delivered_total / fee_total / sent_total` 与涉及路径下标 |
| `bottleneck` | 不可行时的 `Bottleneck(kind, detail)` |
| `evaluations` | 搜索评估计数（可观测性，不是优化目标） |

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
  - 拒绝负数余额/费用、自环、重复有向边、越界节点引用、超规模输入；
  - `solve` 拒绝 `D<0 / D>100`、`max_paths` 越界、`budget<=0`、端点不存在或相同。
  - 求解是无状态的纯函数，不存在“部分变更”；非法输入在任何计算前抛出。
- `SearchBudgetExhausted`：搜索评估数超过 `budget`。**这是“算不动”，
  不是“无解”**——与 `feasible=False` 的“已完整穷举证明无解”严格分开。
  在题面上限与默认预算下不会发生（实测最坏对抗网络约 18.3 万次评估）；
  该参数主要用于隔离测试与防御。

### 无解诊断

- `bottleneck.kind == "unreachable"`：源到收款端无任何简单路径。
- `bottleneck.kind == "insufficient_balance"`：存在路径但余额（按实际
  发送额计）不足；`detail` 给出“至多可交付 X of D”的精确上界，例如
  `deliverable at most 74 of 100 with up to 3 simple paths; ...`。

## 循环付款规划（solve_cycle）

从指定节点出发规划一笔回到自己的循环付款（通道再平衡）：路径是经过
**至少两条边**的简单环，第一条边是要**减余额**的通道（源端实发），
最后一条边是要**增余额**的通道（最终收回）。中间节点不重复；首尾同
属一个节点不算零长度路径（必须真走至少两条有向边）。费用仍按原整数
向上取整规则（`fee = b + ceil(x*r/1e6)`）从收款端反向递推；每条有向
边的余额按该边上的实际发送额**单独**检查；规划不修改网络余额。

```python
from mp_split import solve_cycle

result = solve_cycle(net, node=0, amount=60, budget=20_000_000)
```

- `node`：出发/回收节点的原始编号；`amount`：最终收回金额
  `0 <= D <= 100`；`budget` 语义与 `solve` 相同。
- 优化优先级（字典序）：**源端实际支出（首跳实发额）最小** →
  **跳数最少** → **边编号（输入顺序）元组字典序最先**。

返回值 `CycleResult`（不可变值对象）：

| 字段 | 含义 |
|---|---|
| `feasible` / `bottleneck` | 是否可行 / 不可行原因 |
| `source_total` | 最小源端实际支出（首跳实发额） |
| `amount` | 最终收回金额（末跳交付额） |
| `cycle` | 环节点序列（原始编号，首尾相同） |
| `hops` | `HopBill` 元组，按转发顺序：`edge / delivered / fee / sent`，满足 `sent - fee == delivered` |
| `evaluations` | 搜索评估计数（可观测性） |

循环付款的无解诊断：

- `bottleneck.kind == "no_cycle"`：出发节点不存在任何至少两条边的
  简单环。
- `bottleneck.kind == "insufficient_balance"`：存在环但余额不足；
  `detail` 给出“至多可收回 X of D”的精确上界，例如
  `returnable at most 77 of 78 around any simple cycle from 0; ...`。

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

## 已知限制

- 仅实现本题契约：并行单边、同一有序节点对多边、时序/手续费动态
  变化、失败回滚、概率流动性等均不支持。
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

执行演示（真实计算；约 0.04 秒，远低于 8 秒；不使用 sleep 或预录
结果）：

```bash
python demo.py
```

演示打印：一个 D=60 的成功分拆（逐跳账单 + 每边余额核对）、一个
D=100 的已证明无解瓶颈，以及一个 NaN 余额被拒绝的异常边界。

循环付款演示（真实离线计算；实测 < 0.1 秒，远低于 90 秒；不依赖
账号、密钥、sleep 或预录结果）：

```bash
python demo_iteration.py
```

演示打印：一个收回 60 的成功循环付款（逐跳实发/转发/费用 + 每边
余额核对 + 余额未被修改核对）、一个首边手续费导致余额不足的已证明
无解瓶颈，以及一个收回金额超限被拒绝的异常边界。

## 目录

```
mp_split/
  __init__.py     公开接口导出
  errors.py       MPSplitError / NetworkValidationError / SearchBudgetExhausted
  model.py        Edge / Network / HopBill / PathBill / EdgeUsage / SplitResult / CycleResult
  fees.py         费用公式与最小实发额闭式解（纯整数）
  solver.py       简单路径/简单环枚举、分拆搜索、循环付款规划、瓶颈诊断
tests/
  test_fees.py            费用数学与取整边界
  test_validation.py      输入校验（bool / NaN / 规模 / 引用）
  test_solver.py          共享瓶颈、固定费抑制拆分、无路、预算等场景
  test_cycle.py           循环付款：无环、首边费用超额、并列环取舍、交叉核对
  test_cross_validate.py  对独立参考的穷举交叉验证
  reference.py            独立参考实现（不导入被测核心；含简单环枚举参考）
demo.py                   固定输入演示
demo_iteration.py         循环付款固定输入演示
```

## 验证手段说明

`tests/reference.py` 是**独立**参考：自带路径 DFS、自带倍增+二分的
费用反推（形态不同于被测闭式解）、穷举 1..3 路径的全部正整数
分拆，并独立计算逐跳账单与最大可交付额。它**不导入 `mp_split` 的
求解代码**。交叉测试逐字段比对费用、路径序列、分拆向量、逐跳账单
与不可行上界；账单的往返自洽（`sent - fee == delivered` 等）只作
补充断言。此外另有小 D 的密集图穷举对拍与 8 节点 16 边 D≤100 的
上限规模自洽 fuzz（开发期运行，非交付测试的必需部分）。

循环付款同样配有独立参考 `reference_cycle`（自带简单环 DFS 与
倍增+二分费用反推，不导入被测核心）；`tests/test_cycle.py` 在
400 个确定性随机小图上逐字段比对可行性、源端支出、环序列与逐跳
账单，并另测不可行时“至多可收回 X”上界与参考一致。

# mp_split — 有向支付通道的多路径可行分拆

离线、确定性、纯整数的支付通道分拆求解库：给定有向通道网络（每边有
余额、基础费 `b`、整数 ppm 比例 `r`、**锁定增量 `lock_delta`**）与
收款金额 `D`，在至多 3 条简单路径上求非负整数分拆，使**源总支付最小**，
并返回逐跳账单（金额、费用与**到期**）与每条边的累计占用；每条边的
锁定增量沿路径从收款端向源端反向累计为到期，超过付款人最大期限
`max_lock` 的路径被过滤；无解时返回可定位的瓶颈诊断。不连接任何网络。

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

## 期限模型（每边锁定增量）

每条边另有非负整数 **锁定增量** `lock_delta`（类 CLTV 时间锁增量，
与转发金额无关）：

- 以**收款端到期 0 为基准**，上游在每一跳承担的到期 = 下游到期 +
  该边增量，即到期沿路径**从收款端向源端反向累计**；
- 路径的源端到期 `source_lock` = 路径各边 `lock_delta` 之和；
  逐跳的绝对到期在 `HopBill.lock` 返回，相邻跳满足
  `hops[j].lock - hops[j+1].lock == hops[j].edge.lock_delta`；
- `solve(..., max_lock=L)` 只接受 `source_lock <= L` 的路径。
  **等号允许**（恰好等于不超限），严格超过才过滤；期限是路径级常量，
  过滤在分拆搜索之前完成，不影响金额优化；
- 所有可交付路径都超限时返回
  `bottleneck.kind == "deadline_exceeded"`，`detail` 给出最小可达
  源端到期。`max_lock=None`（默认）表示不限制，旧调用行为不变。

## 输入上限（接口边界）

| 项目 | 上限 |
|---|---|
| 节点数 | 8 |
| 有向边数 | 16 |
| 收款金额 D | `0 <= D <= 100` 整数 |
| 简单路径数 | 至多 3 条 |
| 余额 / b / r / lock_delta | 非负整数 |
| `max_lock` | 非负整数，或 `None`（不限制） |

这是本题的有限协议/数学子集，**不宣称**与任何真实支付网络行业标准
完整兼容（不实现 HTLC 超时、流动性探测、并发更新、信道状态机等）。

## 接口

```python
from mp_split import Edge, Network, solve

net = Network(
    [
        Edge(src=0, dst=1, balance=40, base_fee=1, ppm=0, lock_delta=4),
        Edge(src=1, dst=2, balance=40, base_fee=2, ppm=5_000, lock_delta=6),
        # ...
    ],
    node_count=None,          # 默认按边引用的节点推导；可显式给 1..8
)

result = solve(net, source=0, destination=2, delivery=60,
               max_paths=3, budget=20_000_000, max_lock=40)
```

`solve` 参数：

- `network: Network`，`source/destination`：节点原始编号（必须不同）；
- `delivery: int`，`0 <= D <= 100`；
- `max_paths`：`1..3`，默认 3；
- `budget`：搜索评估上限（正整数），默认 20,000,000；
- `max_lock`：付款人最大期限，非负整数或 `None`（默认不限制）。

`Edge` 的 `lock_delta` 为可选关键字参数，缺省 `0`；旧的 5 参数
`Edge(src, dst, balance, base_fee, ppm)` 构造与不带 `max_lock` 的
`solve` 调用继续可用，行为不变（此时所有到期均为 0）。

### 返回值 `SplitResult`（不可变值对象）

| 字段 | 含义 |
|---|---|
| `feasible` | 是否存在可行分拆 |
| `source_total` | 最小源总支付（不可行为 `None`） |
| `used_paths` | 使用的路径数 |
| `paths` | `PathBill` 元组：`path`（原始节点编号）、`delivered`、`source_paid`、`hops`、`source_lock` |
| `paths[i].source_lock` | 该路径源端累计到期（各边 `lock_delta` 之和） |
| `paths[i].hops` | `HopBill` 元组，按转发顺序：`edge / delivered / fee / sent / lock`，满足 `sent - fee == delivered`；首跳 `sent == source_paid`、`lock == source_lock`，末跳 `delivered` 为该路径交付额 |
| `edge_usages` | `EdgeUsage`：每条被占用边跨路径累计的 `delivered_total / fee_total / sent_total` 与涉及路径下标 |
| `bottleneck` | 不可行时的 `Bottleneck(kind, detail)` |
| `evaluations` | 搜索评估计数（可观测性，不是优化目标） |
| `max_lock` | 回显本次求解的期限上限（`None` 表示不限制） |

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
    `max_lock<0`（`bool`/`float` 同样拒绝）、端点不存在或相同。
  - 求解是无状态的纯函数，不存在“部分变更”；非法输入在任何计算前抛出。
- `SearchBudgetExhausted`：搜索评估数超过 `budget`。**这是“算不动”，
  不是“无解”**——与 `feasible=False` 的“已完整穷举证明无解”严格分开。
  在题面上限与默认预算下不会发生（实测最坏对抗网络约 18.3 万次评估）；
  该参数主要用于隔离测试与防御。

### 无解诊断

- `bottleneck.kind == "unreachable"`：源到收款端无任何简单路径。
- `bottleneck.kind == "deadline_exceeded"`：存在路径但**所有可交付
  路径**的源端累计到期都 `> max_lock`；`detail` 给出最小可达到期与
  对应最早路径，例如 `minimum achievable source lock is 20 ...`。
- `bottleneck.kind == "insufficient_balance"`：存在满足期限的路径但
  余额（按实际发送额计）不足；`detail` 给出“至多可交付 X of D”的
  精确上界，例如 `deliverable at most 74 of 100 with up to 3 simple
  paths; ...`。诊断优先级：无路 → 期限全超 → 余额不足。

## 设计取舍

1. **精确整数，不用浮点。** 费用递推、取整、余额比较、到期累计全部
   用 Python `int`；比例费闭式解由不等式 `x - b - ceil(xr/M) >= d`
   精确化为 `x*(M-r) >= (b+d)*M` 导出。
2. **先枚举简单路径，再搜分拆。** 8 节点 16 边的 s→t 简单路径数实测
   上界约 45（对全部 16 边子集爬山搜索得到），穷举路径组合可行：
   路径组合数至多 `C(45,1)+C(45,2)+C(45,3) ≈ 1.5 万`。
3. **期限是路径级常量，在分拆搜索前过滤。** `source_lock` 只取决于
   路径各边的 `lock_delta`，与金额无关；因此先剔除 `source_lock >
   max_lock` 的路径，再在剩余路径上跑与期限无关的金额搜索，既不
   增加搜索状态，也保证过滤完备。
4. **固定路径组合上做整数分拆搜索**，配两条可证安全的剪枝：
   - 后缀最小费用下界（路径对表惰性计算并在组合间缓存）；
   - 后缀每条共享边的最小累计实发额（逐边独立标量 DP，可加，故
     不会高估资源需求而错误剪枝）。
   全局当前最优费用作为严格 cutoff 跨路径数层传递。
5. **共享边按各路径实际发送额累计**校验；不经过某紧边的路径不受该
   边约束（诊断递归对此显式处理）。
6. **同费用同分拆的确定性 tie-break**：固定组合内，费用相同时取
   分拆向量字典序最大者（搜索按分量降序枚举，首个最优即定）。这是
   题面未完全规定处的确定性补充，不影响三级公开优先级。
7. **无解与预算分离**：只有搜索树被完整剪枝/遍历后才返回
   `feasible=False`；预算触发抛异常。

## 已知限制

- 仅实现本题契约：并行单边、同一有序节点对多边、时序/手续费动态
  变化、失败回滚、概率流动性、HTLC 超时状态机等均不支持；锁定增量
  是纯整数的期限累计，不模拟链上时间推进或过期结算。
- 路径上限 3、D 上限 100、8 节点 / 16 边是硬边界，超限直接报错。
- 到期以**收款端 0**为基准的相对高度；不接受绝对的收款端到期时刻
  参数（若需要绝对时刻，调用方可在结果上整体平移）。
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

演示打印：一个带锁定增量的 D=60 成功分拆（逐跳金额账单、逐跳到期
与每边余额核对）、`max_lock` 的等号/严格超过/全部超限三种期限
边界、一个 D=100 的已证明无解瓶颈，以及 NaN 余额与非法 `max_lock`
被拒绝的异常边界。

## 目录

```
mp_split/
  __init__.py     公开接口导出
  errors.py       MPSplitError / NetworkValidationError / SearchBudgetExhausted
  model.py        Edge(含 lock_delta) / Network / HopBill(含 lock) /
                  PathBill(含 source_lock) / EdgeUsage / SplitResult(含 max_lock)
  fees.py         费用公式与最小实发额闭式解（纯整数）
  solver.py       简单路径枚举、到期反向累计、期限过滤、分拆搜索、瓶颈诊断
tests/
  test_fees.py            费用数学与取整边界
  test_validation.py      输入校验（bool / NaN / 规模 / 引用）
  test_solver.py          共享瓶颈、固定费抑制拆分、无路、预算等场景
  test_locks.py           锁定增量反向累计、期限等号/过滤/失败、非法输入
  test_cross_validate.py  对独立参考的穷举交叉验证（含 lock_delta/max_lock）
  reference.py            独立参考实现（不导入被测核心）
demo.py                   固定输入演示
```

## 验证手段说明

`tests/reference.py` 是**独立**参考：自带路径 DFS、自带倍增+二分的
费用反推（形态不同于被测闭式解）、**独立地反向累计每跳到期**、
穷举 1..3 路径的全部正整数分拆，并独立计算逐跳账单、最大可交付额
与最小可达到期。它**不导入 `mp_split` 的求解代码**。交叉测试逐
字段比对费用、路径序列、分拆向量、逐跳账单（含 `lock`）、不可行
上界与期限瓶颈；账单的往返自洽（`sent - fee == delivered`、相邻跳
到期差等于边增量等）只作补充断言。此外另有用小 D 的密集图穷举
对拍（默认 400 个旧费用用例 + 300 个带锁定增量/`max_lock` 的
确定性随机用例）。

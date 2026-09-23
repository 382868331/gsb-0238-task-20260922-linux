"""核心算法：有向支付通道的多路径可行分拆。

费用与递推（严格整数，无浮点）
------------------------------
一条边转发 ``x`` 单位收取 ``fee(x) = base_fee + ceil(x*ppm/1_000_000)``。
沿路径 ``e1, e2, ..., ek`` 交付 ``a`` 到目的地时，从最后一跳向前递推：

    x_k = a
    x_i = x_{i+1} + fee_{e_i}(x_{i+1})   # 边 e_i 实际向下游转发 x_i

边 e_i 的**实际发送额**（HTLC 口径的通道占用）为 ``x_i``：第 i 跳节点收到
``x_i + fee_{e_i}(x_i)``、自留 ``fee_{e_i}``、向下游转发 ``x_i``。因此余额
约束的是转发额本身：``x_i <= balance(e_i)``；多条路径共用一条边时，各路径在
该边上的转发额累计不得超过余额。源总支付为递推到第一跳之前的
``x_1 + fee_{e_1}(x_1)``。

搜索（穷举，精确最优）
----------------------
1. DFS 枚举 source→destination 的全部**简单路径**（节点不重复）；
2. 对每条路径二分求其单边可行的最大交付额 ``c(P)``（0..D），丢弃 c=0 的死路；
3. 枚举 1..3 条路径的组合（容量和 < D 直接剪枝）；
4. 对每个组合递归枚举和为 D 的正整数分配（被使用路径均 >0；更少路径的
   方案由更小的组合规模覆盖），逐跳检查共享边累计余额；
5. 按 (源总支付, 使用路径数, 排序后 (路径,金额) 序列字典序) 取全局最小。

预算：DFS 步数、组合数、叶子分配评估都计入 ``budget``。耗尽时抛
:class:`SearchBudgetExhausted`——此时既未证明最优也未证明无解，与穷举完毕后
返回的 :class:`InfeasibleResult`（已证明无解）严格区分。
"""

from __future__ import annotations

from collections import deque
from itertools import combinations
from typing import Hashable, Sequence

from .errors import InvalidInputError, NetworkLimitError, SearchBudgetExhausted
from .model import (
    MAX_AMOUNT,
    MAX_PATHS,
    PPM_DENOMINATOR,
    Bottleneck,
    Edge,
    HopBill,
    InfeasibleResult,
    Network,
    PathPlan,
    SplitResult,
)

DEFAULT_BUDGET = 5_000_000
"""默认搜索预算（DFS 步数 + 组合数 + 叶子分配评估的合计上限）。"""


# ---- 标签字典序：混合类型标签也可稳定排序 ----------------------------------
def _label_key(value: Hashable) -> tuple[str, object]:
    if isinstance(value, bool):  # 构造时已拒绝，防御性处理
        raise InvalidInputError("node", "bool labels are not allowed")
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, str):
        return ("str", value)
    return ("other", repr(value))


def _path_key(nodes: Sequence[Hashable]) -> tuple[tuple[str, object], ...]:
    return tuple(_label_key(n) for n in nodes)


# ---- 费用与路径递推（精确整数） --------------------------------------------
def fee_for(edge: Edge, amount: int) -> int:
    """``base_fee + ceil(amount*ppm/1e6)``，精确整数。"""
    return edge.base_fee + (
        amount * edge.ppm + PPM_DENOMINATOR - 1
    ) // PPM_DENOMINATOR


def _forward(path: tuple[int, ...], deliver: int, edges: Sequence[Edge]):
    """给定路径与交付额，从目的端反向递推。

    :returns: ``(source_paid, amounts, fees)``；``amounts[i]`` 为第 i 跳转发额，
              ``fees[i]`` 为第 i 跳费用。
    """
    amounts = [0] * len(path)
    fees = [0] * len(path)
    carry = deliver
    for i in range(len(path) - 1, -1, -1):
        amounts[i] = carry
        f = fee_for(edges[path[i]], carry)
        fees[i] = f
        carry += f
    return carry, amounts, fees


def _path_feasible(path: tuple[int, ...], deliver: int, edges: Sequence[Edge]) -> bool:
    """单路径交付 ``deliver`` 是否满足各边余额。

    边 path[i] 的实际发送额是其**转发额**（进入该跳时的 carry，加费之前）；
    费在跳内结算，不占余额。
    """
    carry = deliver
    for i in range(len(path) - 1, -1, -1):
        if carry > edges[path[i]].balance:
            return False
        carry += fee_for(edges[path[i]], carry)
    return True


def _max_capacity(path: tuple[int, ...], edges: Sequence[Edge], ceiling: int) -> int:
    """路径单边可行的最大交付额（可行性对金额单调，在 [0, ceiling] 上二分）。"""
    if _path_feasible(path, ceiling, edges):
        return ceiling
    lo, hi = 0, ceiling
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _path_feasible(path, mid, edges):
            lo = mid
        else:
            hi = mid - 1
    return lo


# ---- 预算计数 --------------------------------------------------------------
class _Budget:
    __slots__ = ("spent", "limit")

    def __init__(self, limit: int) -> None:
        self.spent = 0
        self.limit = limit

    def tick(self, n: int = 1) -> None:
        self.spent += n
        if self.spent > self.limit:
            raise SearchBudgetExhausted(self.spent, self.limit)


# ---- 简单路径枚举 ----------------------------------------------------------
def _enumerate_simple_paths(
    network: Network,
    source: Hashable,
    destination: Hashable,
    budget: _Budget,
) -> list[tuple[tuple[Hashable, ...], tuple[int, ...]]]:
    """枚举全部 source→destination 简单路径。

    :returns: 路径列表，每条路径为 ``(nodes, edge_ids)`` 二元组，
              按节点序列字典序排列（结果稳定、可复现）。
    """
    edges = network.edges
    found: set[tuple[int, ...]] = set()

    # 手写迭代 DFS：栈帧 = (当前节点, 下一条待考察出边的位置)。
    node = source
    pos = 0
    stack: list[tuple[Hashable, int]] = []
    visited: set[Hashable] = {source}
    path_ids: list[int] = []

    while True:
        budget.tick()
        outs = network.out_ids(node)
        advanced = False
        while pos < len(outs):
            eid = outs[pos]
            pos += 1
            nxt = edges[eid].dst
            if nxt == destination:
                found.add(tuple(path_ids + [eid]))
                continue
            if nxt not in visited:
                visited.add(nxt)
                path_ids.append(eid)
                stack.append((node, pos))
                node, pos = nxt, 0
                advanced = True
                break
        if advanced:
            continue
        # 回退。
        if not stack:
            break
        visited.remove(node)
        path_ids.pop()
        node, pos = stack.pop()

    result: list[tuple[tuple[Hashable, ...], tuple[int, ...]]] = []
    for eids in found:
        nodes: list[Hashable] = [source]
        for eid in eids:
            nodes.append(edges[eid].dst)
        result.append((tuple(nodes), eids))
    result.sort(key=lambda item: _path_key(item[0]))
    return result


def simple_paths(
    network: Network,
    source: Hashable,
    destination: Hashable,
    *,
    budget: object = DEFAULT_BUDGET,
) -> list[tuple[tuple[Hashable, ...], tuple[int, ...]]]:
    """枚举 source→destination 的全部简单路径（公开只读工具）。

    :returns: ``[(nodes, edge_ids), ...]``，按节点序列字典序排列；
              ``nodes`` 为节点标签元组，``edge_ids`` 为 :class:`Network` 边索引元组。
    :raises InvalidInputError: 网络类型错误、节点不存在、源目相同、预算非法。
    :raises SearchBudgetExhausted: 预算耗尽（结论不完整）。
    """
    if not isinstance(network, Network):
        raise InvalidInputError(
            "network", f"must be Network, got {type(network).__name__}"
        )
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise InvalidInputError("budget", f"must be a positive int, got {budget!r}")
    src = network.require_node(source, "source")
    dst = network.require_node(destination, "destination")
    if src == dst:
        raise InvalidInputError("destination", "source and destination must differ")
    return _enumerate_simple_paths(network, src, dst, _Budget(budget))


# ---- 路径金额表 ------------------------------------------------------------
class _PathTable:
    __slots__ = ("ids", "nodes", "cap", "cost", "edge_amount")

    def __init__(
        self,
        ids: tuple[int, ...],
        nodes: tuple[Hashable, ...],
        cap: int,
        cost: list[int],
        edge_amount: dict[int, list[int]],
    ) -> None:
        self.ids = ids
        self.nodes = nodes
        self.cap = cap
        self.cost = cost  # cost[a] = 交付 a 时源支付（a=0 时为 0，不产生固定费）
        self.edge_amount = edge_amount  # eid -> amounts[a]（该跳转发额）


def _build_table(
    network: Network, nodes: tuple[Hashable, ...], path: tuple[int, ...], cap: int
) -> _PathTable:
    edges = network.edges
    cost: list[int] = [0]
    edge_amount: dict[int, list[int]] = {eid: [0] for eid in path}
    for a in range(1, cap + 1):
        paid, amounts, _fees = _forward(path, a, edges)
        cost.append(paid)
        for i, eid in enumerate(path):
            edge_amount[eid].append(amounts[i])
    return _PathTable(path, nodes, cap, cost, edge_amount)


# ---- 主入口 ----------------------------------------------------------------
def split_payment(
    network: Network,
    source: Hashable,
    destination: Hashable,
    amount: object,
    *,
    budget: object = DEFAULT_BUDGET,
):
    """计算最优多路径可行分拆。

    :param network: :class:`Network`。
    :param source: 源节点标签。
    :param destination: 目的节点标签（必须与源不同）。
    :param amount: 接收量 D，非负整数且 ``D <= 100``。
    :param budget: 搜索预算（正整数；DFS 步数 + 组合数 + 叶子评估的合计）。
    :returns: 可行时返回 :class:`SplitResult`；穷举证明无解时返回
              :class:`InfeasibleResult`；预算耗尽抛
              :class:`SearchBudgetExhausted`。
    :raises InvalidInputError: 输入非法（错误消息带可定位字段名）。
    """
    if not isinstance(network, Network):
        raise InvalidInputError(
            "network", f"must be Network, got {type(network).__name__}"
        )
    if isinstance(budget, bool) or not isinstance(budget, int):
        raise InvalidInputError("budget", f"must be int, got {type(budget).__name__}")
    if budget <= 0:
        raise InvalidInputError("budget", f"must be > 0, got {budget}")

    src = network.require_node(source, "source")
    dst = network.require_node(destination, "destination")
    if src == dst:
        raise InvalidInputError("destination", "source and destination must differ")
    d = _require_whole(amount, "amount")
    if d < 0:
        raise InvalidInputError("amount", f"must be >= 0, got {d}")
    if d > MAX_AMOUNT:
        raise NetworkLimitError("amount", f"too large: {d} > {MAX_AMOUNT}")

    if d == 0:
        return SplitResult(plans=(), source=src, destination=dst, amount=0)

    meter = _Budget(budget)
    paths = _enumerate_simple_paths(network, src, dst, meter)
    if not paths:
        return InfeasibleResult(source=src, destination=dst, amount=d, reason="no_path")

    edges = network.edges
    tables: list[_PathTable] = []
    for nodes, eids in paths:
        cap = _max_capacity(eids, edges, d)
        if cap == 0:
            continue  # 连 1 个单位都送不到（费用吃光余额），任何分配都用不上。
        tables.append(_build_table(network, nodes, eids, cap))

    balances = [e.balance for e in edges]
    best_key: tuple | None = None
    best_combo: list[_PathTable] | None = None
    best_alloc: tuple[int, ...] | None = None

    def evaluate(
        combo: Sequence[_PathTable], alloc: tuple[int, ...], total: int
    ) -> None:
        nonlocal best_key, best_combo, best_alloc
        # 第三准则：活跃路径按节点序列字典序排列后，整条 (路径, 交付额) 序列
        # 的字典序最小（路径集合相同但金额不同的平局也由此完全裁决）。
        active = sorted(
            (_path_key(tbl.nodes), a)
            for tbl, a in zip(combo, alloc)
            if a > 0
        )
        key = (total, len(active), tuple(active))
        if best_key is None or key < best_key:
            best_key = key
            best_combo = list(combo)
            best_alloc = alloc

    # 容量降序只是为了让容量剪枝更早生效；最优性来自全局 key 比较，与枚举顺序无关。
    order = sorted(
        range(len(tables)),
        key=lambda i: (-tables[i].cap, _path_key(tables[i].nodes)),
    )
    # 每条被使用路径至少承担各边固定费之和，用作费用下界剪枝。
    base_sums = [sum(edges[eid].base_fee for eid in t.ids) for t in tables]

    def dominated(lb_cost: int, n_paths: int) -> bool:
        """费用下界为 lb_cost、必用 n_paths 条路径的方案是否已不可能优于当前最优。

        严格大于 (cost, nused) 时可安全剪枝；相等时第三准则（路径/金额字典序）
        仍可能更优，故不剪（交给枚举处理，量级已很小）。
        """
        if best_key is None:
            return False
        return (lb_cost, n_paths) > (best_key[0], best_key[1])

    for k in range(1, MAX_PATHS + 1):
        if best_key is not None and (best_key[0], best_key[1]) == (d, 1):
            break  # 费用下界 D、最少 1 路径同时达到，字典序已取最小，全局最优。
        for idxs in combinations(order, k):
            meter.tick()
            combo = [tables[i] for i in idxs]
            if sum(t.cap for t in combo) < d:
                continue
            combo_bases = [base_sums[i] for i in idxs]
            # 组合层费用下界：总交付 D（每单位至少付 1）+ k 条活跃路径各自的固定费。
            if dominated(d + sum(combo_bases), k):
                continue

            used = [0] * len(edges)
            alloc_so_far: list[int] = []

            def rec(level: int, delivered: int, cost_so_far: int) -> None:
                tbl = combo[level]
                remaining_levels = k - level - 1
                if level == k - 1:
                    meter.tick()
                    a = d - delivered  # 最后一条路径补齐余额，组合内每条路径均须正额。
                    if a < 1 or a > tbl.cap:
                        return
                    for eid in tbl.ids:
                        if used[eid] + tbl.edge_amount[eid][a] > balances[eid]:
                            return
                    evaluate(combo, tuple(alloc_so_far + [a]), cost_so_far + tbl.cost[a])
                    return

                later_base = sum(combo_bases[j] for j in range(level + 1, k))
                later_cap = sum(combo[j].cap for j in range(level + 1, k))
                upper = min(tbl.cap, d - delivered - remaining_levels)
                for a in range(1, upper + 1):
                    rem = d - delivered - a
                    if rem < remaining_levels or rem > later_cap:
                        continue  # 剩余路径每条至少 1，且总容量要够。
                    new_cost = cost_so_far + tbl.cost[a]
                    # 本组合最终恰用 k 条路径；费用下界 = 已付 + 剩余 rem 单位
                    # （每单位至少 1）+ 剩余路径固定费。
                    if dominated(new_cost + rem + later_base, k):
                        continue
                    added: list[int] = []
                    ok = True
                    for eid in tbl.ids:
                        inc = tbl.edge_amount[eid][a]
                        if used[eid] + inc > balances[eid]:
                            ok = False
                            break
                        if inc > 0:
                            added.append(eid)
                            used[eid] += inc
                    if ok:
                        alloc_so_far.append(a)
                        rec(level + 1, delivered + a, new_cost)
                        alloc_so_far.pop()
                    for eid in added:
                        used[eid] -= tbl.edge_amount[eid][a]

            rec(0, 0, 0)

    if best_combo is None:
        return _infeasible(network, src, dst, d)

    plans: list[PathPlan] = []
    for tbl, a in zip(best_combo, best_alloc):
        if a == 0:
            continue  # 零额路径不产生固定费，输出中直接省略。
        paid, amounts, fees = _forward(tbl.ids, a, edges)
        hops = tuple(
            HopBill(
                edge_index=eid,
                src=edges[eid].src,
                dst=edges[eid].dst,
                amount=amounts[i],
                fee=fees[i],
                inflow=amounts[i] + fees[i],
                balance_used=amounts[i],
            )
            for i, eid in enumerate(tbl.ids)
        )
        plans.append(
            PathPlan(
                nodes=tbl.nodes,
                edge_ids=tbl.ids,
                deliver=a,
                source_paid=paid,
                hops=hops,
            )
        )
    plans.sort(key=lambda p: _path_key(p.nodes))

    result = SplitResult(plans=tuple(plans), source=src, destination=dst, amount=d)
    _assert_balances(result, network)
    return result


def _require_whole(value: object, locator: str) -> int:
    """整数参数：拒绝 bool；拒绝 NaN/Infinity；非整数 float 不静默舍入。"""
    import math

    if isinstance(value, bool):
        raise InvalidInputError(locator, f"must be int, got bool ({value!r})")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidInputError(
                locator, f"must be finite, got non-finite float ({value!r})"
            )
        if not value.is_integer():
            raise InvalidInputError(
                locator, f"must be a whole number, got fractional float ({value!r})"
            )
        return int(value)
    raise InvalidInputError(
        locator, f"must be int, got {type(value).__name__} ({value!r})"
    )


def _assert_balances(result: SplitResult, network: Network) -> None:
    """构造结果后复核：每条共享边累计转发额 ≤ 余额（防御性自检）。"""
    for eid, amount in result.edge_usage().items():
        if amount > network.edges[eid].balance:
            raise AssertionError(
                f"internal error: edge {eid} usage {amount} > balance "
                f"{network.edges[eid].balance}"
            )


# ---- 不可行诊断：有效单流容量松弛上的 s-t 最小割 ---------------------------
def _residual_cut(network: Network, src: Hashable, dst: Hashable):
    """以每条边余额为容量，做带残量反向边的 Edmonds–Karp 最大流，
    返回 (残量可达标签集, 最小割正向割边索引列表)。

    任何可行支付在每条 s-t 割上至少要净交付 D，故割余额和 < D 是不可行的
    必要证据；该松弛忽略逐跳费叠加、ceil 取整与 3 路径上限，仅作定位诊断，
    不是完整的不可行性证明（证明来自主搜索的完整穷举）。
    """
    labels = sorted(network.nodes, key=_label_key)
    index = {node: i for i, node in enumerate(labels)}
    n = len(labels)
    s, t = index[src], index[dst]

    # 残量弧：[对端节点, 反向弧在对端邻接表中的位置, 残量容量]
    graph: list[list[list]] = [[] for _ in range(n)]

    def add_arc(u: int, v: int, capacity: int) -> None:
        graph[u].append([v, len(graph[v]), capacity])
        graph[v].append([u, len(graph[u]) - 1, 0])

    orig: list[tuple[int, int, int]] = []
    for eid, edge in enumerate(network.edges):
        u, v = index[edge.src], index[edge.dst]
        add_arc(u, v, edge.balance)
        orig.append((u, v, eid))

    while True:
        prev_arc: list[list | None] = [None] * n
        prev_node: list[int] = [-1] * n
        prev_arc[s] = []  # 占位标记源点已访问
        queue = deque([s])
        while queue and prev_arc[t] is None:
            u = queue.popleft()
            for arc in graph[u]:
                w = arc[0]
                if prev_arc[w] is None and arc[2] > 0:
                    prev_arc[w] = arc
                    prev_node[w] = u
                    queue.append(w)
        if prev_arc[t] is None:
            break
        add = 10**18
        w = t
        while w != s:
            add = min(add, prev_arc[w][2])  # type: ignore[index]
            w = prev_node[w]
        w = t
        while w != s:
            arc = prev_arc[w]
            arc[2] -= add  # type: ignore[index]
            rev = graph[arc[0]][arc[1]]  # type: ignore[index]
            rev[2] += add
            w = prev_node[w]

    reachable_idx = {i for i, arc in enumerate(prev_arc) if arc is not None}
    cut = [eid for u, v, eid in orig if u in reachable_idx and v not in reachable_idx]
    return {labels[i] for i in reachable_idx}, cut


def _infeasible(network: Network, src: Hashable, dst: Hashable, d: int) -> InfeasibleResult:
    # 能走到这里说明拓扑上存在简单路径（否则上层已返回 no_path），但余额无法
    # 在 3 条路径内容纳 D。用余额松弛下的 s-t 最小割给出可定位的瓶颈边。
    _reachable, cut = _residual_cut(network, src, dst)
    cut_capacity = sum(network.edges[eid].balance for eid in cut)
    margin = d - cut_capacity
    if margin <= 0:
        # 松弛割无法见证缺口：不可行来自逐跳费叠加、ceil 取整或最多 3 路径
        # 上限（结论已由主搜索穷举证明），此处不给可能误导的瓶颈边。
        return InfeasibleResult(
            source=src,
            destination=dst,
            amount=d,
            reason="insufficient_capacity",
            bottlenecks=(),
        )
    bottlenecks = tuple(
        Bottleneck(
            edge_index=eid,
            src=network.edges[eid].src,
            dst=network.edges[eid].dst,
            balance=network.edges[eid].balance,
            cut_capacity=cut_capacity,
            required=d,
            margin=margin,
        )
        for eid in cut
    )
    return InfeasibleResult(
        source=src,
        destination=dst,
        amount=d,
        reason="insufficient_capacity",
        bottlenecks=bottlenecks,
    )

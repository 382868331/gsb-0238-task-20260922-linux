"""循环付款（rebalance 环）规划：从指定节点出发回到自身的简单环。

用途：通道余额不均时，节点沿一个环把一笔钱付给自己——第一条边是
**要减余额**的通道（本节点作为源端实付），最后一条边是**要增加余额**
的通道（本节点作为收款端收回）。规划只算账，**不修改网络余额**。

契约：

* 环至少两条有向边；第一条边必须是 `decrease_edge`（源端为
  `node`），最后一条边必须是 `increase_edge`（收款端为
  `node`）。即使两条边端点相同（2 边环）也按两条独立有向边各自
  检查容量，绝不把首尾同属一个节点当成零长度路径。
* 中间节点（除 `node` 外）不得重复，且 `node` 不出现在中间——
  即简单环。
* 费用沿用题面整数向上取整规则，由收款端向源端反向递推
  （`mp_split.fees.min_sent_for_delivery`），每条有向边的余额
  按该边上的实际发送额单独检查。
* 目标：给定最终收回金额 `receive`，先最少化源端实际支出
  （第一跳的 `sent`），再最少跳数，再按边在 `network.edges` 中的
  编号序列字典序最先。
"""

from __future__ import annotations

from .errors import NetworkValidationError, SearchBudgetExhausted
from .fees import min_sent_for_delivery
from .model import (
    MAX_DELIVERY,
    Bottleneck,
    CycleResult,
    Edge,
    HopBill,
    Network,
    _is_real_int,
)

DEFAULT_BUDGET = 20_000_000


def _find_edge(net: Network, src: int, dst: int, where: str) -> int:
    """按原始编号定位有向边，返回其在 `net.edges` 中的下标。"""
    s = net.to_internal(src, f"{where}.src")
    d = net.to_internal(dst, f"{where}.dst")
    for i, e in enumerate(net.edges):
        if e.src == s and e.dst == d:
            return i
    raise NetworkValidationError(
        f"{where}: no directed edge {src}->{dst} in network"
    )


def _enumerate_middle_paths(
    adj: list[list[tuple[int, int]]],
    start: int,
    goal: int,
    forbidden: int,
) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """枚举 start->goal 且不经过 forbidden 的全部简单路径（含空路径）。

    返回 (节点元组, 边下标元组)，按节点元组字典序排序。start == goal
    时给出唯一的空路径（不经过任何中间边）。
    """
    if start == goal:
        return [((start,), ())]
    found: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    node_stack = [start]
    edge_stack: list[int] = []
    seen = (1 << start) | (1 << forbidden)

    def dfs(u: int) -> None:
        nonlocal seen
        if u == goal:
            found.append((tuple(node_stack), tuple(edge_stack)))
            return
        for v, ei in adj[u]:
            bit = 1 << v
            if not seen & bit:
                seen |= bit
                node_stack.append(v)
                edge_stack.append(ei)
                dfs(v)
                edge_stack.pop()
                node_stack.pop()
                seen ^= bit

    dfs(start)
    found.sort(key=lambda p: p[0])
    return found


def plan_cycle(
    network: Network,
    node: int,
    decrease_edge: tuple[int, int],
    increase_edge: tuple[int, int],
    receive: int,
    *,
    budget: int = DEFAULT_BUDGET,
) -> CycleResult:
    """规划一笔回到自己的循环付款。

    参数:
        network: 校验后的 :class:`Network`。
        node: 出发并返回的节点（原始编号）。
        decrease_edge: `(src, dst)` 原始编号对，要减余额的通道；
            必须满足 `src == node`，作为环的第一条边。
        increase_edge: `(src, dst)` 原始编号对，要增加余额的通道；
            必须满足 `dst == node`，作为环的最后一条边。
        receive: 最终在 `increase_edge` 上收回的整数金额，
            `0 <= receive <= 100`。
        budget: 搜索评估上限；耗尽抛 :class:`SearchBudgetExhausted`。

    返回:
        :class:`CycleResult`。可行时含逐跳账单 `hops`（转发顺序，
        每跳 `sent - fee == delivered`）与源端实际支出
        `source_paid`；不可行时 `feasible=False` 并带
        `bottleneck`（`"no_cycle"` 或 `"insufficient_balance"`）。
        网络余额不被修改。
    """
    if not isinstance(network, Network):
        raise NetworkValidationError(
            f"network: expected Network, got {type(network).__name__}"
        )
    home = network.to_internal(node, "node")
    for name, pair in (("decrease_edge", decrease_edge), ("increase_edge", increase_edge)):
        if not (isinstance(pair, tuple) and len(pair) == 2):
            raise NetworkValidationError(
                f"{name}: expected a (src, dst) pair, got {pair!r}"
            )
    dec_idx = _find_edge(network, decrease_edge[0], decrease_edge[1], "decrease_edge")
    inc_idx = _find_edge(network, increase_edge[0], increase_edge[1], "increase_edge")
    dec = network.edges[dec_idx]
    inc = network.edges[inc_idx]
    if dec.src != home:
        raise NetworkValidationError(
            f"decrease_edge: must start at node {node}, got {decrease_edge[0]}->{decrease_edge[1]}"
        )
    if inc.dst != home:
        raise NetworkValidationError(
            f"increase_edge: must end at node {node}, got {increase_edge[0]}->{increase_edge[1]}"
        )
    if dec_idx == inc_idx:
        # 不可能发生（无自环边时 src==node 且 dst==node 的边不存在），防御
        raise NetworkValidationError(
            "decrease_edge and increase_edge must be different edges"
        )
    amount = _is_real_int(receive, "receive")
    if amount < 0:
        raise NetworkValidationError(f"receive: must be >= 0, got {amount}")
    if amount > MAX_DELIVERY:
        raise NetworkValidationError(
            f"receive: at most {MAX_DELIVERY}, got {amount}"
        )
    bg = _is_real_int(budget, "budget")
    if bg <= 0:
        raise NetworkValidationError(f"budget: must be > 0, got {budget}")

    counter = {"n": 0}

    def tick() -> None:
        counter["n"] += 1
        if counter["n"] > bg:
            raise SearchBudgetExhausted(counter["n"], bg)

    adj = network.adjacency()
    middle_paths = _enumerate_middle_paths(adj, dec.dst, inc.src, home)
    # 完整环的边序列：首边 + 中间路径 + 末边，至少两条边
    candidates: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    for nodes, mids in middle_paths:
        full_edges = (dec_idx,) + mids + (inc_idx,)
        full_nodes = (home,) + nodes + (home,)
        candidates.append((full_nodes, full_edges))

    if not candidates:
        return CycleResult(
            False,
            None,
            amount,
            (),
            (),
            Bottleneck(
                "no_cycle",
                f"no simple cycle from {node} back to {node} starting with "
                f"edge {decrease_edge[0]}->{decrease_edge[1]} and ending with "
                f"edge {increase_edge[0]}->{increase_edge[1]}",
            ),
            0,
        )

    best_key: tuple[int, int, tuple[int, ...]] | None = None
    best_hops: list[tuple[int, int, int]] | None = None  # (edge_idx, delivered, sent)
    best_nodes: tuple[int, ...] | None = None
    saw_capacity_failure = False

    for nodes, edge_seq in candidates:
        tick()
        # 由收款端向源端反向递推；每条有向边容量单独检查
        hops: list[tuple[int, int, int]] = [None] * len(edge_seq)  # type: ignore[list-item]
        cur = amount
        ok = True
        for j in range(len(edge_seq) - 1, -1, -1):
            e = network.edges[edge_seq[j]]
            sent = min_sent_for_delivery(cur, e.base_fee, e.ppm)
            if sent is None or sent > e.balance:
                ok = False
                saw_capacity_failure = True
                break
            hops[j] = (edge_seq[j], cur, sent)
            cur = sent
        if not ok:
            continue
        key = (cur, len(edge_seq), edge_seq)
        if best_key is None or key < best_key:
            best_key = key
            best_hops = hops
            best_nodes = nodes

    if best_key is None or best_hops is None or best_nodes is None:
        if saw_capacity_failure:
            kind = "insufficient_balance"
            detail = (
                f"every simple cycle from {node} via {decrease_edge[0]}->"
                f"{decrease_edge[1]} ... {increase_edge[0]}->{increase_edge[1]} "
                f"lacks capacity to return {amount} "
                "(balances are charged on the actual sent amount "
                "delivered + fee b + ceil(x*r/1e6))"
            )
        else:
            kind = "no_cycle"
            detail = (
                f"no simple cycle from {node} back to {node} starting with "
                f"edge {decrease_edge[0]}->{decrease_edge[1]} and ending with "
                f"edge {increase_edge[0]}->{increase_edge[1]}"
            )
        return CycleResult(False, None, amount, (), (), Bottleneck(kind, detail), counter["n"])

    hop_bills: list[HopBill] = []
    for ei, delivered, sent in best_hops:
        ie = network.edges[ei]
        out_edge = Edge(
            network.to_label(ie.src),
            network.to_label(ie.dst),
            ie.balance,
            ie.base_fee,
            ie.ppm,
        )
        hop_bills.append(HopBill(out_edge, delivered, sent - delivered, sent))

    return CycleResult(
        True,
        best_key[0],
        amount,
        tuple(network.to_label(v) for v in best_nodes),
        tuple(hop_bills),
        None,
        counter["n"],
    )


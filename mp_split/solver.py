"""多路径可行分拆求解器（精确整数搜索）。

搜索层次（题面规定的优先级）：

1. 源总支付 ``source_total`` 最小；
2. 使用路径数最少（故先穷尽 k=1，再 k=2，再 k=3）；
3. 路径列表的节点序列字典序最先（简单路径按节点元组排序，
   组合按索引升序枚举，等价值保留先出现者）。

同一组合、同一源支付的不同分拆取分拆向量 (d0,...,d{k-1}) 的
字典序最大者（确定性取舍，见 README“设计取舍”）。

实现要点（k <= 3）：

* 每条路径的费用/账单表对 d=0..cap 预计算；
* 长度 1 的后缀下界是标量直取，长度 2 的后缀（路径对）费用/资源
  下界只建一次表并在所有三元组合间缓存共享；
* 资源下界对每条共享边独立求最小累计实发额，逐边可加，因此
  余额剪枝可靠（不会高估资源需求）；
* “证明无解”来自完整搜索；叶子评估计数超过 ``budget`` 才抛
  :class:`SearchBudgetExhausted`，两者严格区分。
"""

from __future__ import annotations

from itertools import combinations

from .errors import NetworkValidationError, SearchBudgetExhausted
from .fees import min_sent_for_delivery
from .model import (
    MAX_DELIVERY,
    MAX_PATHS,
    Bottleneck,
    CycleResult,
    Edge,
    EdgeUsage,
    HopBill,
    Network,
    PathBill,
    SplitResult,
    _is_real_int,
)

DEFAULT_BUDGET = 20_000_000
INF = 10**30


class _PathInfo:
    """一条简单路径的预计算信息。

    ``table[d] = (source_paid, hops)``，hops 为
    ``[(edge_index, delivered, sent), ...]``，按转发顺序排列。
    """

    __slots__ = ("nodes", "edge_idxs", "cap", "table", "sent_map")

    def __init__(self, nodes, edge_idxs, cap, table) -> None:
        self.nodes = nodes
        self.edge_idxs = edge_idxs
        self.cap = cap
        self.table = table
        # edge -> {d: 该路径交付 d 时在该边上的实发额}
        self.sent_map: dict[int, dict[int, int]] = {}
        for d, (_, hops) in enumerate(table):
            for ei, _, sent in hops:
                self.sent_map.setdefault(ei, {})[d] = sent

    def sent_on(self, ei: int, d: int) -> int:
        m = self.sent_map.get(ei)
        return 0 if m is None else m.get(d, 0)


class _PairTables:
    """路径对 (p_i, p_j) 的后缀下界表，按 (R) 惰性计算并记忆。

    ``min_cost(R, positive)``：两路径各分 >=1（positive=True）或允许
    0（False）、合计交付恰为 R 时的最小源支付。
    ``min_use(R, ei, positive)``：同样条件下该对在边 ei 上的最小累计
    实发额（逐边独立最小，仅对两路径共享边有意义）。
    """

    __slots__ = ("pi", "pj", "D", "shared", "_cost", "_use")

    def __init__(self, pi: _PathInfo, pj: _PathInfo, D: int) -> None:
        self.pi = pi
        self.pj = pj
        self.D = D
        self.shared = set(pi.edge_idxs) & set(pj.edge_idxs)
        self._cost: dict[tuple[int, bool], int] = {}
        self._use: dict[tuple[int, bool, int], int] = {}

    def min_cost(self, R: int, positive: bool) -> int:
        key = (R, positive)
        v = self._cost.get(key)
        if v is not None:
            return v
        pi, pj = self.pi, self.pj
        start = 1 if positive else 0
        if R < 2 * start or R > pi.cap + pj.cap:
            self._cost[key] = INF
            return INF
        best_c = INF
        lo = max(start, R - pj.cap)
        hi = min(pi.cap, R - start)
        ti, tj = pi.table, pj.table
        for di in range(lo, hi + 1):
            c = ti[di][0] + tj[R - di][0]
            if c < best_c:
                best_c = c
        self._cost[key] = best_c
        return best_c

    def min_use(self, R: int, ei: int, positive: bool) -> int:
        key = (R, positive, ei)
        v = self._use.get(key)
        if v is not None:
            return v
        pi, pj = self.pi, self.pj
        start = 1 if positive else 0
        if R < 2 * start or R > pi.cap + pj.cap:
            self._use[key] = INF
            return INF
        best_v = INF
        lo = max(start, R - pj.cap)
        hi = min(pi.cap, R - start)
        for di in range(lo, hi + 1):
            val = pi.sent_on(ei, di) + pj.sent_on(ei, R - di)
            if val < best_v:
                best_v = val
        self._use[key] = best_v
        return best_v


class _ComboSearch:
    """固定路径组合上的整数分拆搜索（后缀下界来自共享路径对表）。"""

    def __init__(
        self,
        net: Network,
        combo: tuple[_PathInfo, ...],
        pair_tables: dict[tuple[int, int], _PairTables],
        diag_cache: dict[tuple[int, ...], int],
        budget_tick,
        d_max: int,
    ) -> None:
        self.net = net
        self.combo = combo
        self.k = len(combo)
        self.caps = [p.cap for p in combo]
        self.d_max = d_max
        self.tick = budget_tick
        self.pair_tables = pair_tables
        self.pair_diag_cache = diag_cache

        count: dict[int, int] = {}
        for p in combo:
            for ei in p.edge_idxs:
                count[ei] = count.get(ei, 0) + 1
        shared_all = [ei for ei, c in count.items() if c >= 2]
        # “紧边”过滤：余额 >= 该组合在该边上的理论最大占用时，该边
        # 不可能违约，不进入逐路径状态（否则其用量无谓撑爆诊断状态）。
        max_sent = max(p.table[p.cap][0] for p in combo)
        self.shared = [
            ei for ei in shared_all if net.edges[ei].balance < self.k * max_sent
        ]
        self.bal = [net.edges[ei].balance for ei in self.shared]
        # 单紧边诊断的路径对（B,S）查询缓存
        self.pair_diag_cache: dict[tuple[int, ...], int] = {}

    # ---- 后缀下界 ----
    def _pair(self, i: int, j: int) -> _PairTables:
        # 路径对表跨组合共享：以路径对象身份为键（组合内位置不可用作键）
        pi, pj = self.combo[i], self.combo[j]
        key = (id(pi), id(pj))
        pt = self.pair_tables.get(key)
        if pt is None:
            pt = _PairTables(pi, pj, self.d_max)
            self.pair_tables[key] = pt
        return pt

    def suf_cost(self, pos: int, R: int, positive: bool) -> int:
        """路径 pos.. 合计交付 R 的最小费用（忽略余额），无穷大不可行。"""
        k = self.k
        if R < 0:
            return INF
        if pos == k:
            return 0 if R == 0 else INF
        p = self.combo[pos]
        if pos == k - 1:
            if R > p.cap or (positive and R < 1):
                return INF
            if not positive and R == 0:
                return 0
            return p.table[R][0]
        # 后缀恰为路径对（k <= 3，pos 为 k-2）
        tbl = self._pair(pos, pos + 1)
        return tbl.min_cost(R, positive)

    def suf_use(self, pos: int, R: int, ei: int, positive: bool) -> int:
        """后缀路径在边 ei 上的最小累计实发额。"""
        k = self.k
        if R < 0:
            return INF
        if pos == k:
            return 0 if R == 0 else INF
        p = self.combo[pos]
        if pos == k - 1:
            if R > p.cap or (positive and R < 1):
                return INF
            return p.sent_on(ei, R)
        tbl = self._pair(pos, pos + 1)
        if R > self.d_max:
            return INF
        return tbl.min_use(R, ei, positive)

    # ---- 主搜索 ----
    def run(self, target: int, cutoff: int | None = None) -> tuple[int, list[int]] | None:
        """交付恰为 target（每条选中路径 >=1）的最小费用分拆。

        cutoff 给定时（全局当前最优），只接受严格小于 cutoff 的费用；
        费用等于 cutoff 的分支整枝——因为路径数优先级只在源支付相同
        时起作用，而本层路径数更多，等值分支不可能成为最终答案。
        """
        k = self.k
        if target < k or target > sum(self.caps):
            return None
        best_cost: int | None = None
        best_alloc: list[int] | None = None
        alloc = [0] * k
        prefix_use = [0] * len(self.shared)
        combo = self.combo

        def rec(pos: int, remain: int, cost_so_far: int) -> None:
            nonlocal best_cost, best_alloc
            if pos == k - 1:
                d = remain
                if not 1 <= d <= self.caps[pos]:
                    return
                self.tick()
                total = cost_so_far + combo[pos].table[d][0]
                if cutoff is not None and total >= cutoff:
                    return
                for si, ei in enumerate(self.shared):
                    if prefix_use[si] + combo[pos].sent_on(ei, d) > self.bal[si]:
                        return
                if best_cost is not None and total > best_cost:
                    return
                alloc[pos] = d
                # d 降序枚举：同费用下首个即分拆向量字典序最大者
                if best_cost is None or total < best_cost:
                    best_cost, best_alloc = total, alloc[:]
                return

            rest_paths = k - 1 - pos
            lo = max(1, remain - sum(self.caps[pos + 1 :]))
            hi = min(self.caps[pos], remain - rest_paths)
            for d in range(hi, lo - 1, -1):
                rest = remain - d
                c = combo[pos].table[d][0]
                # 免费弱下界：源支付恒 >= 交付额。能整枝就不构建路径对表。
                cheap_lb = cost_so_far + c + rest
                if cutoff is not None and cheap_lb >= cutoff:
                    continue
                if best_cost is not None and cheap_lb > best_cost:
                    continue
                lb = cost_so_far + c + self.suf_cost(pos + 1, rest, True)
                if lb >= INF:
                    continue
                if best_cost is not None and lb > best_cost:
                    continue
                if cutoff is not None and lb >= cutoff:
                    continue
                # 资源下界同理：先用本路径用量 + 后续至少交付 rest 做弱检查
                ok = True
                for si, ei in enumerate(self.shared):
                    if (
                        prefix_use[si]
                        + combo[pos].sent_on(ei, d)
                        + self.suf_use(pos + 1, rest, ei, True)
                        > self.bal[si]
                    ):
                        ok = False
                        break
                if not ok:
                    continue
                alloc[pos] = d
                for si, ei in enumerate(self.shared):
                    prefix_use[si] += combo[pos].sent_on(ei, d)
                rec(pos + 1, rest, cost_so_far + c)
                for si, ei in enumerate(self.shared):
                    prefix_use[si] -= combo[pos].sent_on(ei, d)

        rec(0, target, 0)
        if best_cost is None:
            return None
        return best_cost, best_alloc  # type: ignore[return-value]

    def max_deliverable(self, hard_cap: int, floor: int) -> int:
        """允许路径取 0 时，<= hard_cap 内可交付的最大额；<= floor 提前返回。

        紧边（可能违约的共享边）数：

        * 0：交付额只受各路径容量约束，直接给和；
        * 1：逐路径枚举 + 后缀路径对的单约束反演（结果跨组合缓存）；
        * >=2：状态按余额钳位的 memoized 递归（小规模，完备精确）。
        """
        upper = min(hard_cap, sum(self.caps))
        if upper <= floor:
            return floor
        k = self.k
        combo = self.combo
        bal = self.bal
        tight = self.shared

        if not tight:
            return upper

        if len(tight) == 1:
            ei = tight[0]
            B = bal[0]

            def suffix_max(pos: int, budget_R: int, sent_left: int) -> int:
                """后缀路径 pos.. 在交付 <= budget_R、边 ei 用量 <= sent_left
                下的最大交付（各路径允许取 0）。覆盖任意长度后缀。
                注意紧边不一定被每条后缀路径经过（不经过者用量恒 0），
                因此不能因 sent_left==0 而整体返回 0。"""
                if pos == k or budget_R <= 0:
                    return 0
                # 跨组合缓存：以后缀路径身份序列为键
                skey = tuple(id(q) for q in combo[pos:])
                key = (skey, ei, budget_R, sent_left)
                cached = self.pair_diag_cache.get(key)
                if cached is not None:
                    return cached
                self.tick()
                p = combo[pos]
                best = 0
                for d in range(min(p.cap, budget_R), -1, -1):
                    si = p.sent_on(ei, d)
                    if si > sent_left:
                        continue
                    val = d + suffix_max(pos + 1, budget_R - d, sent_left - si)
                    if val > best:
                        best = val
                        if val >= budget_R:
                            break
                self.pair_diag_cache[key] = best
                return best

            val = suffix_max(0, upper, B)
            return max(floor, val)

        # 多紧边：状态按余额钳位的 memoized 递归
        caps = self.caps
        memo: dict[tuple[int, int, tuple[int, ...]], int] = {}

        def clamp(v: int, b: int) -> int:
            return v if v <= b else b + 1

        def best(pos: int, capR: int, use: tuple[int, ...]) -> int:
            if pos == k:
                return 0
            key = (pos, capR, use)
            cached = memo.get(key)
            if cached is not None:
                return cached
            self.tick()
            local_best = -1
            hi = min(caps[pos], capR)
            for d in range(hi, -1, -1):
                new_use = list(use)
                ok = True
                for si, ei in enumerate(tight):
                    v = use[si] + combo[pos].sent_on(ei, d)
                    if v > bal[si]:
                        ok = False
                        break
                    new_use[si] = clamp(v, bal[si])
                if not ok:
                    continue
                vrest = best(pos + 1, capR - d, tuple(new_use))
                if vrest < 0:
                    continue
                val = d + vrest
                if val > local_best:
                    local_best = val
                    if val >= capR:
                        break
            memo[key] = local_best
            return local_best

        return max(floor, best(0, upper, tuple([0] * len(tight))))


def _enumerate_simple_paths(
    adj: list[list[tuple[int, int]]], s: int, t: int
) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """枚举 s->t 全部简单路径，按节点元组字典序排序。"""
    found: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    node_stack = [s]
    edge_stack: list[int] = []
    seen = 1 << s

    def dfs(u: int) -> None:
        nonlocal seen
        if u == t:
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

    dfs(s)
    found.sort(key=lambda p: p[0])
    return found


def _build_path_info(
    net: Network, nodes: tuple[int, ...], edge_idxs: tuple[int, ...], d_max: int
) -> _PathInfo | None:
    """计算路径在 d=0..d_max 下的反向递推账单与可用容量。

    若路径上存在 ``ppm >= 1e6`` 的边，任何正金额都不可交付（cap=0，
    该路径被丢弃）。
    """
    table: list[tuple[int, list[tuple[int, int, int]]]] = [(0, [])]
    cap = 0
    for d in range(1, d_max + 1):
        hops: list[tuple[int, int, int]] = [None] * len(edge_idxs)  # type: ignore[list-item]
        amount = d
        ok = True
        for j in range(len(edge_idxs) - 1, -1, -1):
            ei = edge_idxs[j]
            e = net.edges[ei]
            sent = min_sent_for_delivery(amount, e.base_fee, e.ppm)
            if sent is None or sent > e.balance:
                ok = False
                break
            hops[j] = (ei, amount, sent)
            amount = sent
        if not ok:
            # 费用与所需实发额对 d 单调：更大的 d 同样不可行
            break
        table.append((amount, hops))
        cap = d
    return _PathInfo(nodes, edge_idxs, cap, table)


def solve(
    network: Network,
    source: int,
    destination: int,
    delivery: int,
    *,
    max_paths: int = MAX_PATHS,
    budget: int = DEFAULT_BUDGET,
) -> SplitResult:
    """求最优多路径分拆。

    参数:
        network: 校验后的 :class:`Network`。
        source/destination: 节点原始编号。
        delivery: 收款端要收到的整数总额 D，``0 <= D <= 100``。
        max_paths: 允许使用的最多简单路径数（1..3）。
        budget: 搜索评估上限；耗尽时抛 :class:`SearchBudgetExhausted`
            （“算不动”，不是无解结论）。

    返回:
        :class:`SplitResult`。无可行解时 ``feasible=False`` 且带
        ``bottleneck``；预算耗尽时抛异常而非返回无解。
    """
    if not isinstance(network, Network):
        raise NetworkValidationError(
            f"network: expected Network, got {type(network).__name__}"
        )
    s = network.to_internal(source, "source")
    t = network.to_internal(destination, "destination")
    if s == t:
        raise NetworkValidationError("source and destination must be different nodes")
    D = _is_real_int(delivery, "delivery")
    if D < 0:
        raise NetworkValidationError(f"delivery: must be >= 0, got {D}")
    if D > MAX_DELIVERY:
        raise NetworkValidationError(f"delivery: at most {MAX_DELIVERY}, got {D}")
    mp = _is_real_int(max_paths, "max_paths")
    if not 1 <= mp <= MAX_PATHS:
        raise NetworkValidationError(
            f"max_paths: must be in 1..{MAX_PATHS}, got {max_paths}"
        )
    bg = _is_real_int(budget, "budget")
    if bg <= 0:
        raise NetworkValidationError(f"budget: must be > 0, got {budget}")

    counter = {"n": 0}

    def tick() -> None:
        counter["n"] += 1
        if counter["n"] > bg:
            raise SearchBudgetExhausted(counter["n"], bg)

    # D=0：平凡可行，不使用任何路径
    if D == 0:
        return SplitResult(True, 0, 0, (), (), None, 0, 0)

    adj = network.adjacency()
    raw_paths = _enumerate_simple_paths(adj, s, t)
    if not raw_paths:
        return SplitResult(
            False,
            None,
            0,
            (),
            (),
            Bottleneck("unreachable", f"no simple path from {source} to {destination}"),
            D,
            0,
        )

    infos: list[_PathInfo] = []
    for nodes, edge_idxs in raw_paths:
        info = _build_path_info(network, nodes, edge_idxs, D)
        if info is not None and info.cap >= 1:
            infos.append(info)

    if not infos:
        return SplitResult(
            False,
            None,
            0,
            (),
            (),
            Bottleneck(
                "insufficient_balance",
                f"deliverable at most 0 of {D} with up to {mp} simple paths; "
                f"every {source}->{destination} path lacks capacity "
                f"(balances are charged on the actual sent amount "
                f"delivered + fee b + ceil(x*r/1e6))",
            ),
            D,
            0,
        )

    pair_tables: dict[tuple[int, int], _PairTables] = {}
    diag_cache: dict[tuple[int, ...], int] = {}
    # 全局最优：先源支付最小，同值再路径数最少，同值再路径字典序最先。
    # 因此扫描全部 k 层；后层只有“严格更便宜”才能替换前层结果。
    best_combo: tuple[_PathInfo, ...] | None = None
    best_cost: int | None = None
    best_alloc: list[int] | None = None
    for k in range(1, mp + 1):
        for combo in combinations(infos, k):
            cs = _ComboSearch(network, combo, pair_tables, diag_cache, tick, D)
            found = cs.run(D, cutoff=best_cost)
            if found is None:
                continue
            cost, alloc = found
            if best_cost is None or cost < best_cost:
                # 组合按路径节点序列字典序升序枚举：等费用保留先出现者，
                # 而等费用只会出现在同一 k 层（跨层用严格 cutoff 整枝）。
                best_cost, best_combo, best_alloc = cost, combo, alloc

    if best_combo is not None and best_alloc is not None:
        return _build_result(
            network,
            best_combo,
            best_alloc,
            best_cost,  # type: ignore[arg-type]
            D,
            counter["n"],
        )

    # ---- 已证明无解：诊断 <=D 的最大可交付额 ----
    best_achievable = 0
    for k in range(1, mp + 1):
        for combo in combinations(infos, k):
            cs = _ComboSearch(network, combo, pair_tables, diag_cache, tick, D)
            best_achievable = cs.max_deliverable(D, best_achievable)
            if best_achievable >= D:
                # 理论上不该发生（D 可行主搜索必有解），防御性终止
                break

    detail = (
        f"deliverable at most {best_achievable} of {D} with up to {mp} simple "
        f"paths; edge balances are charged on the actual sent amount "
        f"(delivered + fee b + ceil(x*r/1e6))"
    )
    return SplitResult(
        False,
        None,
        0,
        (),
        (),
        Bottleneck("insufficient_balance", detail),
        D,
        counter["n"],
    )


def _build_result(
    net: Network,
    combo: tuple[_PathInfo, ...],
    alloc: list[int],
    source_total: int,
    D: int,
    evaluations: int,
) -> SplitResult:
    path_bills: list[PathBill] = []
    # edge index -> [delivered, fee, sent, path positions]
    agg: dict[int, list] = {}
    delivered_check = 0

    for pos, (p, d) in enumerate(zip(combo, alloc)):
        _, hops = p.table[d]
        hop_bills: list[HopBill] = []
        for ei, delivered, sent in hops:
            ie = net.edges[ei]
            out_edge = Edge(
                net.to_label(ie.src),
                net.to_label(ie.dst),
                ie.balance,
                ie.base_fee,
                ie.ppm,
            )
            fee = sent - delivered
            hop_bills.append(HopBill(out_edge, delivered, fee, sent))
            row = agg.setdefault(ei, [0, 0, 0, []])
            row[0] += delivered
            row[1] += fee
            row[2] += sent
            row[3].append(pos)
        paid, _ = p.table[d]
        assert hop_bills[0].sent == paid
        assert hop_bills[-1].delivered == d
        delivered_check += d
        path_bills.append(
            PathBill(
                tuple(net.to_label(v) for v in p.nodes), d, paid, tuple(hop_bills)
            )
        )

    assert delivered_check == D
    usages: list[EdgeUsage] = []
    for ei in sorted(agg):
        delivered, fee, sent, positions = agg[ei]
        assert sent <= net.edges[ei].balance, "shared-edge capacity invariant"
        ie = net.edges[ei]
        out_edge = Edge(
            net.to_label(ie.src),
            net.to_label(ie.dst),
            ie.balance,
            ie.base_fee,
            ie.ppm,
        )
        usages.append(EdgeUsage(out_edge, delivered, fee, sent, tuple(positions)))

    assert sum(pb.source_paid for pb in path_bills) == source_total
    return SplitResult(
        True,
        source_total,
        len(path_bills),
        tuple(path_bills),
        tuple(usages),
        None,
        D,
        evaluations,
    )


def _enumerate_simple_cycles(
    adj: list[list[tuple[int, int]]], s: int
) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """枚举从 s 出发回到 s 的全部简单环（至少两条边，中间节点不重复）。

    首条边是 s 的出边（其余额被减少），末条边是 s 的入边（其余额被
    增加）；首尾同属 s 不构成零长度路径——环至少包含两条有向边。
    返回按边编号元组字典序排序的 (节点序列, 边编号序列) 列表。
    """
    found: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    node_stack = [s]
    edge_stack: list[int] = []
    seen = 1 << s

    def dfs(u: int) -> None:
        nonlocal seen
        for v, ei in adj[u]:
            if v == s:
                # 闭合回 s：当前路径至少已有一条边，加闭合边即 >= 2 条
                found.append((tuple(node_stack + [s]), tuple(edge_stack + [ei])))
                continue
            bit = 1 << v
            if not seen & bit:
                seen |= bit
                node_stack.append(v)
                edge_stack.append(ei)
                dfs(v)
                edge_stack.pop()
                node_stack.pop()
                seen ^= bit

    dfs(s)
    found.sort(key=lambda c: c[1])
    return found


def solve_cycle(
    network: Network,
    node: int,
    amount: int,
    *,
    budget: int = DEFAULT_BUDGET,
) -> CycleResult:
    """规划一笔回到自己的循环付款（再平衡）。

    从 ``node`` 出发，经过至少两条有向边回到自身：第一条边是要减
    余额的通道（源端实发），最后一条边是要增余额的通道（最终收回）。
    中间节点不重复（简单环）；每条有向边的余额按该边上的实际发送额
    单独检查；费用按原整数向上取整规则从收款端反向递推。规划是只读
    的纯函数，不修改网络余额。

    参数:
        network: 校验后的 :class:`Network`。
        node: 出发/回收节点的原始编号。
        amount: 最终收回金额（末跳交付额）D，``0 <= D <= 100``。
        budget: 搜索评估上限；耗尽时抛 :class:`SearchBudgetExhausted`。

    优化优先级（字典序）：
        1. 源端实际支出（首跳实发额）最小；
        2. 跳数最少；
        3. 边编号（输入顺序）元组字典序最先。
    """
    if not isinstance(network, Network):
        raise NetworkValidationError(
            f"network: expected Network, got {type(network).__name__}"
        )
    s = network.to_internal(node, "node")
    D = _is_real_int(amount, "amount")
    if D < 0:
        raise NetworkValidationError(f"amount: must be >= 0, got {D}")
    if D > MAX_DELIVERY:
        raise NetworkValidationError(f"amount: at most {MAX_DELIVERY}, got {D}")
    bg = _is_real_int(budget, "budget")
    if bg <= 0:
        raise NetworkValidationError(f"budget: must be > 0, got {budget}")

    counter = {"n": 0}

    def tick() -> None:
        counter["n"] += 1
        if counter["n"] > bg:
            raise SearchBudgetExhausted(counter["n"], bg)

    adj = network.adjacency()
    cycles = _enumerate_simple_cycles(adj, s)
    if not cycles:
        return CycleResult(
            False,
            None,
            D,
            (),
            (),
            Bottleneck(
                "no_cycle",
                f"no simple cycle with at least 2 edges from node {node} "
                "back to itself",
            ),
            0,
        )

    best_key: tuple[int, int, tuple[int, ...]] | None = None
    best_info: _PathInfo | None = None
    max_cap = 0
    for _nodes, edge_idxs in cycles:
        tick()
        info = _build_path_info(network, _nodes, edge_idxs, D)
        if info is None:
            continue
        if info.cap > max_cap:
            max_cap = info.cap
        if info.cap < D:
            continue
        cost = info.table[D][0]
        key = (cost, len(edge_idxs), edge_idxs)
        if best_key is None or key < best_key:
            best_key = key
            best_info = info

    if best_info is None:
        detail = (
            f"returnable at most {max_cap} of {D} around any simple cycle "
            f"from {node}; each directed edge balance is charged on the "
            f"actual sent amount (delivered + fee b + ceil(x*r/1e6))"
        )
        return CycleResult(
            False,
            None,
            D,
            (),
            (),
            Bottleneck("insufficient_balance", detail),
            counter["n"],
        )

    _, hops = best_info.table[D]
    if D == 0:
        # 表内 d=0 不存逐跳记录；补零金额跳（费用为 0，不占用余额）
        hops = [(ei, 0, 0) for ei in best_info.edge_idxs]
    hop_bills: list[HopBill] = []
    for ei, delivered, sent in hops:
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
        hop_bills[0].sent,
        D,
        tuple(network.to_label(v) for v in best_info.nodes),
        tuple(hop_bills),
        None,
        counter["n"],
    )

"""独立参考实现（仅供测试对照）。

本模块 **不导入 mp_split 的任何求解代码**，用直白的独立写法重新实现
费用递推与穷举搜索，作为期望值来源：

* 自己做简单路径 DFS；
* 自己按题面公式反向递推每条路径每跳账单；
* 穷举 1..3 条路径的全部组合与全部正整数分拆；
* 目标键 (源支付, 路径数, 路径节点元组, 分拆向量取负) 取字典序最小。

图输入用裸元组 ``(src, dst, balance, base_fee, ppm)`` 或带锁定增量的
``(src, dst, balance, base_fee, ppm, lock_delta)``（缺省为 0），节点
编号必须为连续的 ``0..n-1``（测试网络都满足），避免任何被测代码参与
期望值。
"""

from __future__ import annotations

from itertools import combinations

M = 1_000_000


def fee(x: int, b: int, r: int) -> int:
    if x <= 0:
        return 0
    return b + (x * r + M - 1) // M


def min_sent(d: int, b: int, r: int):
    """独立反推：f(x)=x-fee(x) 单调不降，倍增找上界再二分最小 x。

    （不采用逐值线性搜索——r 接近 1e6 时答案可达 1e8；也不复制被测
    模块的闭式解，保持算法形态独立。）
    """
    if d <= 0:
        return 0
    if r >= M:
        return None
    if d + b - fee(d + b, b, r) >= d:
        return d + b
    lo, hi = d + b, 2 * (d + b) + 1
    while hi - fee(hi, b, r) < d:
        lo, hi = hi, hi * 2 + 1
    while lo < hi:
        mid = (lo + hi) // 2
        if mid - fee(mid, b, r) >= d:
            hi = mid
        else:
            lo = mid + 1
    return lo


def all_simple_paths(edges, n: int, s: int, t: int):
    adj = [[] for _ in range(n)]
    for u, v, *_ in edges:
        adj[u].append(v)
    out = []

    def dfs(u, seen, path):
        if u == t:
            out.append(tuple(path))
            return
        for w in sorted(adj[u]):
            if not seen & (1 << w):
                dfs(w, seen | (1 << w), path + [w])

    dfs(s, 1 << s, [s])
    out.sort()
    return out


def edge_lookup(edges):
    out = {}
    for e in edges:
        u, v, bal, b, r = e[:5]
        dl = e[5] if len(e) >= 6 else 0
        out[(u, v)] = (bal, b, r, dl)
    return out


def path_table(edges, n: int, path, d_max: int):
    """返回 {d: (source_paid, [(u,v,delivered,sent),...])}，不可行 d 缺席。

    另外返回该路径的总锁定期（各边 lock_delta 之和），与 d 无关。
    """
    emap = edge_lookup(edges)
    lock_total = sum(emap[(u, v)][3] for u, v in zip(path[:-1], path[1:]))
    table = {0: (0, [])}
    for d in range(1, d_max + 1):
        hops = []
        amount = d
        ok = True
        for u, v in zip(reversed(path[:-1]), reversed(path[1:])):
            bal, b, r, _dl = emap[(u, v)]
            sent = min_sent(amount, b, r)
            if sent is None or sent > bal:
                ok = False
                break
            hops.append((u, v, amount, sent))
            amount = sent
        if not ok:
            break
        hops.reverse()
        table[d] = (amount, hops)
    return table, lock_total


def hop_expiries(emap, path):
    """独立直算逐跳到期：expiry_j = 本边及其后各边 delta 之和。"""
    acc = 0
    out = [0] * (len(path) - 1)
    for j in range(len(path) - 2, -1, -1):
        u, v = path[j], path[j + 1]
        acc += emap[(u, v)][3]
        out[j] = acc
    return out


def reference_solve(
    edges, n: int, s: int, t: int, D: int, max_paths: int = 3, max_lock=None
):
    """返回 (status, payload)。

    可行: ("ok", {"cost","paths","allocs","tables","locks"})；
    不可行: ("infeasible", max_deliverable)；无路: ("unreachable", None)；
    期限过滤光: ("lock_exceeded", min_lock_total)。
    """
    paths = all_simple_paths(edges, n, s, t)
    if not paths:
        return "unreachable", None
    built = [path_table(edges, n, p, D) for p in paths]
    tables = [tb for tb, _lt in built]
    lock_totals = [lt for _tb, lt in built]
    # 仅保留正容量路径
    live = [
        (p, tb, lt)
        for p, tb, lt in zip(paths, tables, lock_totals)
        if max(tb) >= 1
    ]
    if not live:
        return "infeasible", 0
    emap = edge_lookup(edges)

    # 期限硬过滤（逐路径，等号允许）
    if max_lock is not None:
        allowed = [(p, tb, lt) for p, tb, lt in live if lt <= max_lock]
        if not allowed:
            return "lock_exceeded", min(lt for _p, _tb, lt in live)
        live = allowed

    best_key = None
    best_payload = None

    def shared_ok(chosen, allocs):
        use = {}
        for (p, tb, lt), d in zip(chosen, allocs):
            for u, v, _delivered, sent in tb[d][1]:
                use[(u, v)] = use.get((u, v), 0) + sent
        for (u, v), total in use.items():
            if total > emap[(u, v)][0]:
                return False
        return True

    def compositions(k, total, caps):
        def rec(pos, remain):
            if pos == k - 1:
                if 1 <= remain <= caps[pos]:
                    yield (remain,)
                return
            for d in range(1, min(caps[pos], remain - (k - 1 - pos)) + 1):
                for tail in rec(pos + 1, remain - d):
                    yield (d,) + tail

        if total >= k:
            yield from rec(0, total)

    for k in range(1, max_paths + 1):
        for combo in combinations(range(len(live)), k):
            chosen = [live[i] for i in combo]
            caps = [max(tb) for _p, tb, _lt in chosen]
            if sum(caps) < D:
                continue
            for alloc in compositions(k, D, caps):
                cost = sum(tb[d][0] for (_p, tb, _lt), d in zip(chosen, alloc))
                if not shared_ok(chosen, alloc):
                    continue
                key = (
                    cost,
                    k,
                    tuple(p for p, _tb, _lt in chosen),
                    tuple(-a for a in alloc),
                )
                if best_key is None or key < best_key:
                    best_key = key
                    best_payload = {
                        "cost": cost,
                        "paths": [p for p, _tb, _lt in chosen],
                        "allocs": list(alloc),
                        "tables": [tb for _p, tb, _lt in chosen],
                        "locks": [lt for _p, _tb, lt in chosen],
                    }

    if best_payload is None:
        # 诊断最大可交付额：允许分量取 0，逐 target 从 D-1 往下穷举
        achievable = 0
        for target in range(D - 1, 0, -1):
            found = False
            for k in range(1, max_paths + 1):
                for combo in combinations(range(len(live)), k):
                    chosen = [live[i] for i in combo]
                    caps = [max(tb) for _p, tb, _lt in chosen]
                    if sum(caps) < target:
                        continue
                    # 允许 0：在至多 k 条路径上的非负分拆
                    def nonneg(idx, remain, alloc):
                        if idx == k:
                            return [alloc] if remain == 0 else []
                        res = []
                        for d in range(0, min(caps[idx], remain) + 1):
                            res += nonneg(idx + 1, remain - d, alloc + [d])
                        return res

                    for alloc0 in nonneg(0, target, []):
                        nz = [(c, a) for c, a in zip(chosen, alloc0) if a > 0]
                        if not nz:
                            continue
                        if shared_ok([c for c, _a in nz], [a for _c, a in nz]):
                            found = True
                            break
                    if found:
                        break
                if found:
                    break
            if found:
                achievable = target
                break
        return "infeasible", achievable

    return "ok", best_payload

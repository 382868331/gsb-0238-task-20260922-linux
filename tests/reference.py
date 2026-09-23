"""独立参考实现 —— 不导入 paychan，用最直白的递归穷举重新计算期望值。

与被测实现共享的只有 Python 标准库和测试传入的原始数据（普通 tuple/dict）。
用于交叉验证：小 D 下枚举全部路径、全部最多 3 路径组合与全部正整数拆分，
按 (源总支付, 路径数, 路径字典序) 取最优。
"""

from __future__ import annotations

from itertools import combinations


def ref_fee(base_fee, ppm, x):
    return base_fee + (x * ppm + 999_999) // 1_000_000


def ref_forward(path, deliver, edges):
    """返回 (源支付, 每跳转发额列表)。余额口径：第 i 跳转发 x_i，费 b+ceil。"""
    amounts = [0] * len(path)
    carry = deliver
    for i in range(len(path) - 1, -1, -1):
        amounts[i] = carry
        e = edges[path[i]]
        carry += ref_fee(e[3], e[4], carry)
    return carry, amounts


def ref_all_simple_paths(adj, src, dst):
    """adj: {u: [(v, eid), ...]}；返回排序后的边索引元组列表。"""
    found = set()

    def dfs(u, visited, ids):
        for v, eid in sorted(adj.get(u, []), key=lambda t: repr(t[0])):
            if v == dst:
                found.add(tuple(ids + [eid]))
            elif v not in visited:
                visited.add(v)
                dfs(v, visited, ids + [eid])
                visited.remove(v)

    dfs(src, {src}, [])
    return sorted(found)


def ref_path_cap(path, edges, d):
    """单路径最大交付（线性探测，参考实现不求快）。"""
    cap = 0
    for a in range(1, d + 1):
        _, amounts = ref_forward(path, a, edges)
        if all(amounts[i] <= edges[path[i]][2] for i in range(len(path))):
            cap = a
    return cap


def ref_split(edges, src, dst, d, max_paths=3):
    """独立穷举参考。

    edges: [(eid0, src, dst, balance, base_fee, ppm), ...]（eid 即列表位置）。
    返回 dict: {feasible, paid, paths:[(path, alloc)], usage:{eid: amount}}。
    """
    elist = [(s, t, bal, b, r) for (_i, s, t, bal, b, r) in edges]
    adj = {}
    for i, (s, t, _bal, _b, _r) in enumerate(elist):
        adj.setdefault(s, []).append((t, i))

    paths = ref_all_simple_paths(adj, src, dst)
    if d == 0:
        return {"feasible": True, "paid": 0, "paths": [], "usage": {}}
    if not paths:
        return {"feasible": False, "reason": "no_path"}

    # 路径节点序列（用于字典序）。
    def nodes_of(p):
        ns = [src]
        for eid in p:
            ns.append(elist[eid][1])
        return tuple(ns)

    cap = {p: ref_path_cap(p, elist, d) for p in paths}
    live = [p for p in paths if cap[p] > 0]

    def label_key(v):
        return (type(v).__name__, repr(v))

    def path_sort_key(p):
        return tuple(label_key(n) for n in nodes_of(p))

    best = None  # (paid, nused, active-node-keys, chosen list[(path, alloc)])

    for k in range(1, max_paths + 1):
        for combo in combinations(sorted(live, key=path_sort_key), k):
            # 正整数分配（被使用路径必须 >0），允许实际使用数 < k 通过……不，
            # combo 中每条均须 >=1；更少路径的情形由更小的 k 覆盖。
            def split_ways(total, parts):
                if parts == 1:
                    yield (total,)
                    return
                for head in range(1, total):
                    for tail in split_ways(total - head, parts - 1):
                        yield (head,) + tail

            if sum(cap[p] for p in combo) < d or d < k:
                continue
            for alloc in split_ways(d, k):
                if any(a > cap[p] for a, p in zip(alloc, combo)):
                    continue
                usage = {}
                paid_total = 0
                ok = True
                for p, a in zip(combo, alloc):
                    sp, amounts = ref_forward(p, a, elist)
                    paid_total += sp
                    for i, eid in enumerate(p):
                        usage[eid] = usage.get(eid, 0) + amounts[i]
                        if usage[eid] > elist[eid][2]:
                            ok = False
                if not ok:
                    continue
                active = sorted(
                    (path_sort_key(p), a) for p, a in zip(combo, alloc) if a > 0
                )
                cand = (paid_total, len(active), tuple(active),
                        sorted(((p, a) for p, a in zip(combo, alloc)),
                               key=lambda pa: path_sort_key(pa[0])))
                if best is None or cand[:3] < best[:3]:
                    best = cand

    if best is None:
        return {"feasible": False, "reason": "insufficient_capacity"}
    chosen = best[3]
    usage = {}
    for p, a in chosen:
        _sp, amounts = ref_forward(p, a, elist)
        for i, eid in enumerate(p):
            usage[eid] = usage.get(eid, 0) + amounts[i]
    return {
        "feasible": True,
        "paid": best[0],
        "paths": [(list(p), a) for p, a in chosen],
        "usage": usage,
    }

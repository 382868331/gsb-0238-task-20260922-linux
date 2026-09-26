"""循环付款规划的独立参考实现（仅供测试对照）。

不导入 mp_split 的任何求解代码：自己 DFS 枚举经过指定首/末边的
简单环，自己用倍增+二分反推每边最小实发额（复用 tests/reference.py
中独立的 min_sent，该实现与被测库算法形态不同），目标键
(源端实付, 跳数, 边编号序列) 取字典序最小。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.reference import min_sent


def reference_cycle(edges, n, node, dec, inc, receive):
    """edges: [(src,dst,balance,b,ppm), ...]；dec/inc 为 (src,dst) 对。

    返回 ("ok", {"cost", "path", "hops"}) 或 ("no_cycle", None) 或
    ("insufficient_balance", None)。hops 为
    [(u, v, delivered, sent), ...]（转发顺序）。
    """
    emap = {}
    eid = {}
    for i, (u, v, bal, b, r) in enumerate(edges):
        emap[(u, v)] = (bal, b, r)
        eid[(u, v)] = i
    if dec not in emap or inc not in emap:
        raise AssertionError("test setup: specified edge missing")
    if dec[0] != node or inc[1] != node or dec == inc:
        raise AssertionError("test setup: invalid first/last edge")

    adj = {}
    for u, v, *_ in edges:
        adj.setdefault(u, []).append(v)

    # 枚举 dec[1] -> inc[1] 且不经过 node 的简单路径（含空路径）
    mids = []

    def dfs(u, seen, path):
        if u == inc[0]:
            mids.append(tuple(path))
            return
        for w in sorted(adj.get(u, [])):
            if w != node and not seen & (1 << w):
                dfs(w, seen | (1 << w), path + [w])

    if dec[1] == inc[0]:
        mids.append((dec[1],))
    if dec[1] != node:
        dfs(dec[1], (1 << node) | (1 << dec[1]), [dec[1]])

    best_key = None
    best = None
    saw_cap_fail = False
    for mid in mids:
        seq = [dec] + list(zip(mid, mid[1:])) + [inc]
        # 反向递推，逐边独立检查容量
        hops = [None] * len(seq)
        cur = receive
        ok = True
        for j in range(len(seq) - 1, -1, -1):
            bal, b, r = emap[seq[j]]
            sent = min_sent(cur, b, r)
            if sent is None or sent > bal:
                ok = False
                saw_cap_fail = True
                break
            hops[j] = (seq[j][0], seq[j][1], cur, sent)
            cur = sent
        if not ok:
            continue
        key = (cur, len(seq), tuple(eid[e] for e in seq))
        if best_key is None or key < best_key:
            best_key = key
            best = {"cost": cur, "path": (node,) + mid + (node,), "hops": hops}

    if best is None:
        return ("insufficient_balance" if saw_cap_fail else "no_cycle"), None
    return "ok", best

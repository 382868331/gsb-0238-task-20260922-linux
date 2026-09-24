"""与独立参考实现的穷举交叉验证。

参考 ``tests.reference`` 不导入被测核心；这里对大量确定性生成的小网络
逐一比对：可行性状态、最小源支付、路径数、路径序列、分拆向量、
逐跳账单，以及不可行时的最大可交付额。只使用小 D（<=12），以便
参考实现穷举所有路径拆分。
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mp_split import Edge, Network, solve
from tests.reference import reference_solve


def make_cases():
    """确定性生成 (edges, n, s, t, D) 用例。"""
    rng = random.Random(20260923)
    cases = []

    # 1) 手工小网络，覆盖多路径/共享边/高固定费
    cases.append(
        (
            [
                (0, 1, 6, 0, 0), (1, 3, 6, 0, 0),
                (0, 2, 6, 0, 0), (2, 3, 6, 0, 0),
            ],
            4,
            0,
            3,
            10,
        )
    )
    cases.append(
        (
            [
                (0, 1, 100, 5, 0), (1, 3, 100, 0, 0),
                (0, 2, 100, 5, 0), (2, 3, 100, 0, 0),
            ],
            4,
            0,
            3,
            10,
        )
    )
    cases.append(
        (
            [
                (0, 1, 10, 0, 0), (1, 2, 10, 0, 0), (2, 4, 10, 0, 0),
                (0, 3, 10, 0, 0), (3, 2, 10, 0, 0),
            ],
            5,
            0,
            4,
            12,
        )
    )

    # 2) 随机小网络：3..5 节点，D <= 12
    for seed in range(400):
        n = rng.choice([3, 4, 4, 5])
        all_edges = [(u, v) for u in range(n) for v in range(n) if u != v]
        m = rng.randint(n - 1, min(len(all_edges), 9))
        pairs = rng.sample(all_edges, m)
        edges = []
        for u, v in pairs:
            bal = rng.choice([0, 1, 2, 4, 8, 12, 50])
            b = rng.choice([0, 0, 0, 1, 2, 5])
            r = rng.choice([0, 0, 1, 100, 1000, 500_000, 999_999, 1_000_000])
            edges.append((u, v, bal, b, r))
        s, t = rng.sample(range(n), 2)
        D = rng.randint(1, 12)
        cases.append((edges, n, s, t, D))
    return cases


CASES = make_cases()


class CrossValidateTests(unittest.TestCase):
    def test_against_independent_reference(self):
        mismatches = []
        for idx, (edges, n, s, t, D) in enumerate(CASES):
            net = Network([Edge(*e) for e in edges], node_count=n)
            result = solve(net, s, t, D)
            status, payload = reference_solve(edges, n, s, t, D)

            if status == "unreachable":
                if result.feasible or result.bottleneck.kind != "unreachable":
                    mismatches.append((idx, "expected unreachable", result))
                continue

            if status == "infeasible":
                if result.feasible:
                    mismatches.append((idx, "expected infeasible", result))
                    continue
                if result.bottleneck.kind != "insufficient_balance":
                    mismatches.append((idx, "wrong bottleneck kind", result.bottleneck))
                    continue
                # 诊断的最大可交付额一致
                if f"at most {payload} of {D}" not in result.bottleneck.detail:
                    mismatches.append(
                        (idx, f"achievable {payload} vs {result.bottleneck.detail}", None)
                    )
                continue

            # feasible
            if not result.feasible:
                mismatches.append((idx, "expected feasible", result.bottleneck))
                continue

            if result.source_total != payload["cost"]:
                mismatches.append(
                    (idx, f"cost {result.source_total} vs {payload['cost']}", None)
                )
                continue
            if result.used_paths != len(payload["paths"]):
                mismatches.append((idx, "path count differs", None))
                continue
            got_paths = [pb.path for pb in result.paths]
            if got_paths != [tuple(p) for p in payload["paths"]]:
                mismatches.append((idx, f"paths {got_paths} vs {payload['paths']}", None))
                continue
            got_alloc = [pb.delivered for pb in result.paths]
            if got_alloc != payload["allocs"]:
                mismatches.append(
                    (idx, f"alloc {got_alloc} vs {payload['allocs']}", None)
                )
                continue

            # 逐跳账单与参考的反向递推完全一致
            for pb, table, d in zip(result.paths, payload["tables"], got_alloc):
                exp_paid, exp_hops = table[d]
                if pb.source_paid != exp_paid:
                    mismatches.append((idx, "path source_paid mismatch", pb))
                    break
                got_hops = [
                    (h.edge.src, h.edge.dst, h.delivered, h.sent, h.lock)
                    for h in pb.hops
                ]
                if got_hops != exp_hops:
                    mismatches.append((idx, f"hops {got_hops} vs {exp_hops}", None))
                    break

            # 独立复核：交付总额、每边累计余额、源付总额
            total_delivered = sum(pb.delivered for pb in result.paths)
            self.assertEqual(total_delivered, D)
            emap = {(u, v): bal for u, v, bal, _b, _r in edges}
            usage = {}
            for pb in result.paths:
                for h in pb.hops:
                    key = (h.edge.src, h.edge.dst)
                    usage[key] = usage.get(key, 0) + h.sent
                    # 单跳恒等式：sent - fee == delivered
                    self.assertEqual(h.sent - h.fee, h.delivered)
            for key, sent_total in usage.items():
                self.assertLessEqual(sent_total, emap[key])
            self.assertEqual(
                sum(pb.source_paid for pb in result.paths), result.source_total
            )

        if mismatches:
            preview = "\n".join(str(m[:2]) for m in mismatches[:10])
            self.fail(f"{len(mismatches)} mismatches, first:\n{preview}")

    def test_max_paths_limits(self):
        # max_paths=1 与参考 max_paths=1 一致（抽样）
        edges = [
            (0, 1, 6, 0, 0), (1, 3, 6, 0, 0),
            (0, 2, 6, 0, 0), (2, 3, 6, 0, 0),
        ]
        net = Network([Edge(*e) for e in edges], node_count=4)
        r = solve(net, 0, 3, 10, max_paths=1)
        self.assertFalse(r.feasible)
        status, payload = reference_solve(edges, 4, 0, 3, 10, max_paths=1)
        self.assertEqual(status, "infeasible")
        self.assertIn(f"at most {payload} of 10", r.bottleneck.detail)


def make_lock_cases():
    """确定性生成带 lock_delta 与 max_lock 的 (edges,n,s,t,D,max_lock)。"""
    rng = random.Random(20260924)
    cases = []
    # 手工：便宜长锁 vs 贵短锁
    cases.append(
        (
            [
                (0, 1, 100, 0, 0, 25), (1, 3, 100, 0, 0, 25),
                (0, 2, 100, 5, 0, 10), (2, 3, 100, 0, 0, 10),
            ],
            4, 0, 3, 10, 20,
        )
    )
    for seed in range(300):
        n = rng.choice([3, 4, 4, 5])
        all_edges = [(u, v) for u in range(n) for v in range(n) if u != v]
        m = rng.randint(n - 1, min(len(all_edges), 9))
        pairs = rng.sample(all_edges, m)
        edges = []
        for u, v in pairs:
            bal = rng.choice([0, 1, 2, 4, 8, 12, 50])
            b = rng.choice([0, 0, 0, 1, 2, 5])
            r = rng.choice([0, 0, 1, 100, 1000, 500_000, 999_999])
            dl = rng.choice([0, 0, 0, 1, 5, 10, 20, 40])
            edges.append((u, v, bal, b, r, dl))
        s, t = rng.sample(range(n), 2)
        D = rng.randint(1, 12)
        max_lock = rng.choice([None, None, 0, 5, 10, 20, 60])
        cases.append((edges, n, s, t, D, max_lock))
    return cases


LOCK_CASES = make_lock_cases()


class CrossValidateLocksTests(unittest.TestCase):
    def test_against_independent_reference_with_locks(self):
        mismatches = []
        for idx, (edges, n, s, t, D, ml) in enumerate(LOCK_CASES):
            network = Network([Edge(*e) for e in edges], node_count=n)
            result = solve(network, s, t, D, max_lock=ml)
            status, payload = reference_solve(
                edges, n, s, t, D, max_lock=ml
            )

            if status == "unreachable":
                if result.feasible or result.bottleneck.kind != "unreachable":
                    mismatches.append((idx, "expected unreachable", result))
                continue

            if status == "deadline":
                if result.feasible:
                    mismatches.append((idx, "expected deadline_exceeded", result))
                    continue
                if result.bottleneck.kind != "deadline_exceeded":
                    mismatches.append(
                        (idx, f"got {result.bottleneck.kind}, want deadline", None)
                    )
                    continue
                # detail 中必须含最小可达到期
                if f"lock is {payload}" not in result.bottleneck.detail:
                    mismatches.append(
                        (idx, f"min lock {payload} vs {result.bottleneck.detail}", None)
                    )
                continue

            if status == "infeasible":
                if result.feasible:
                    mismatches.append((idx, "expected infeasible", result))
                    continue
                if result.bottleneck.kind != "insufficient_balance":
                    mismatches.append((idx, "wrong bottleneck kind", result.bottleneck))
                    continue
                if f"at most {payload} of {D}" not in result.bottleneck.detail:
                    mismatches.append(
                        (idx, f"achievable {payload} vs {result.bottleneck.detail}", None)
                    )
                continue

            # feasible
            if not result.feasible:
                mismatches.append((idx, "expected feasible", result.bottleneck))
                continue
            if result.source_total != payload["cost"]:
                mismatches.append(
                    (idx, f"cost {result.source_total} vs {payload['cost']}", None)
                )
                continue
            got_paths = [pb.path for pb in result.paths]
            if got_paths != [tuple(p) for p in payload["paths"]]:
                mismatches.append((idx, "paths differ", None))
                continue
            got_alloc = [pb.delivered for pb in result.paths]
            if got_alloc != payload["allocs"]:
                mismatches.append((idx, "alloc differs", None))
                continue

            # 逐跳（含到期）与参考的独立反向累计完全一致
            for pb, table, d in zip(result.paths, payload["tables"], got_alloc):
                exp_paid, exp_hops = table[d]
                if pb.source_paid != exp_paid:
                    mismatches.append((idx, "path source_paid mismatch", pb))
                    break
                got_hops = [
                    (h.edge.src, h.edge.dst, h.delivered, h.sent, h.lock)
                    for h in pb.hops
                ]
                if got_hops != exp_hops:
                    mismatches.append(
                        (idx, f"hops {got_hops} vs {exp_hops}", None)
                    )
                    break
                # source_lock 与首跳到期、路径增量之和一致
                emap = {
                    (u, v): dl for (u, v, _b1, _b2, _r, dl) in edges
                }
                exp_lock = sum(
                    emap[(u, v)] for u, v in zip(pb.path[:-1], pb.path[1:])
                )
                if pb.source_lock != exp_lock or pb.hops[0].lock != exp_lock:
                    mismatches.append((idx, "source_lock mismatch", pb))
                    break
                if ml is not None and pb.source_lock > ml:
                    mismatches.append((idx, "source_lock exceeds max_lock", pb))
                    break

        if mismatches:
            preview = "\n".join(str(m[:2]) for m in mismatches[:10])
            self.fail(f"{len(mismatches)} mismatches, first:\n{preview}")

    def test_deadline_filter_is_additive(self):
        # 解析关系：收紧 max_lock 只会（非严格）减少可行路径集合，
        # 因此同一问题随 max_lock 减小，结论只能按
        # feasible -> insufficient_balance/deadline_exceeded 单调退化。
        edges = [
            (0, 1, 6, 0, 0, 5), (1, 3, 6, 0, 0, 5),
            (0, 2, 6, 0, 0, 30), (2, 3, 6, 0, 0, 30),
        ]
        network = Network([Edge(*e) for e in edges], node_count=4)
        kinds = []
        for ml in [60, 30, 10, 9, 0]:
            r = solve(network, 0, 3, 10, max_lock=ml)
            kinds.append("ok" if r.feasible else r.bottleneck.kind)
        # A 锁 10 / cap 6，B 锁 60 / cap 6：
        # 60 两条都可用（拆 6+4 可行）；30、10 只余 A（cap 6，余额不足）；
        # 9、0 无任何路径满足期限（deadline_exceeded）。
        self.assertEqual(
            kinds,
            ["ok", "insufficient_balance", "insufficient_balance",
             "deadline_exceeded", "deadline_exceeded"],
        )


if __name__ == "__main__":
    unittest.main()

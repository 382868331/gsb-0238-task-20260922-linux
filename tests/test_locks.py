"""每边锁定增量 / 反向累计到期 / 付款人最大期限过滤的测试。

期望值来源：

* 手算（例子在注释里给出逐跳展开）；
* 与 ``tests.reference.reference_solve``（独立穷举，不导入被测核心）
  的随机小网络对拍，包括逐跳到期的独立直算。
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mp_split import Edge, Network, solve
from mp_split.errors import NetworkValidationError
from tests.reference import edge_lookup, hop_expiries, reference_solve


def net(edges, n=None):
    return Network([Edge(*e) for e in edges], node_count=n)


def hop_rows(pb):
    return [
        ((h.edge.src, h.edge.dst), h.delivered, h.fee, h.sent, h.expiry)
        for h in pb.hops
    ]


class ReverseAccumulationTests(unittest.TestCase):
    def test_expiry_backwards_accumulation_three_hops(self):
        # 路径 0->1->2->3，各边锁定增量（按转发顺序）10, 20, 30。
        # 反向累计：末跳到期 30；中间 20+30=50；首跳 10+20+30=60。
        network = net(
            [
                (0, 1, 100, 0, 0, 10),
                (1, 2, 100, 0, 0, 20),
                (2, 3, 100, 0, 0, 30),
            ],
            4,
        )
        r = solve(network, 0, 3, 1, max_lock=60)
        self.assertTrue(r.feasible)
        (pb,) = r.paths
        self.assertEqual(pb.lock_total, 60)
        self.assertEqual([h.expiry for h in pb.hops], [60, 50, 30])
        # 末跳到期恰为末边自己的增量
        self.assertEqual(pb.hops[-1].expiry, 30)
        # 期限与分拆金额无关：D=7 时到期逐跳相同
        r7 = solve(network, 0, 3, 7, max_lock=60)
        self.assertTrue(r7.feasible)
        self.assertEqual([h.expiry for h in r7.paths[0].hops], [60, 50, 30])
        self.assertEqual(r7.paths[0].lock_total, 60)

    def test_equality_boundary_allowed_vs_rejected(self):
        network = net(
            [
                (0, 1, 100, 0, 0, 10),
                (1, 2, 100, 0, 0, 20),
                (2, 3, 100, 0, 0, 30),
            ],
            4,
        )
        # lock_total == 60 == max_lock：等号允许
        self.assertTrue(solve(network, 0, 3, 1, max_lock=60).feasible)
        # 61 也行（更松）；59 恰好不行
        self.assertTrue(solve(network, 0, 3, 1, max_lock=61).feasible)
        r = solve(network, 0, 3, 1, max_lock=59)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "lock_limit_exceeded")
        self.assertIn("minimum achievable lock total is 60", r.bottleneck.detail)
        self.assertEqual(r.max_lock, 59)

    def test_zero_delta_means_zero_expiry(self):
        network = net([(0, 1, 100, 2, 0, 0), (1, 2, 100, 3, 0, 0)], 3)
        r = solve(network, 0, 2, 5, max_lock=0)
        self.assertTrue(r.feasible)
        (pb,) = r.paths
        self.assertEqual(pb.lock_total, 0)
        self.assertEqual([h.expiry for h in pb.hops], [0, 0])
        # 费用照常：5+3=8, 8+2=10
        self.assertEqual(pb.source_paid, 10)


class LockFilterTests(unittest.TestCase):
    def setUp(self):
        # P1: 0->1->2，增量 3+5=8（字典序最先）
        # P2: 0->3->2，增量 2+4=6
        self.edges = [
            (0, 1, 100, 0, 0, 3),
            (1, 2, 100, 0, 0, 5),
            (0, 3, 100, 0, 0, 2),
            (3, 2, 100, 0, 0, 4),
        ]
        self.n = 4

    def test_no_filter_keeps_lexicographic_choice(self):
        r = solve(net(self.edges, self.n), 0, 2, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.paths[0].path, (0, 1, 2))
        self.assertEqual(r.paths[0].lock_total, 8)
        self.assertEqual([h.expiry for h in r.paths[0].hops], [8, 5])
        self.assertIsNone(r.max_lock)

    def test_equality_keeps_path(self):
        r = solve(net(self.edges, self.n), 0, 2, 10, max_lock=8)
        self.assertTrue(r.feasible)
        self.assertEqual(r.paths[0].path, (0, 1, 2))

    def test_filter_switches_cheapest_path(self):
        r = solve(net(self.edges, self.n), 0, 2, 10, max_lock=7)
        self.assertTrue(r.feasible)
        (pb,) = r.paths
        self.assertEqual(pb.path, (0, 3, 2))
        self.assertEqual(pb.lock_total, 6)
        self.assertEqual(
            hop_rows(pb),
            [
                ((0, 3), 10, 0, 10, 6),
                ((3, 2), 10, 0, 10, 4),
            ],
        )

    def test_equality_on_remaining_path(self):
        r = solve(net(self.edges, self.n), 0, 2, 10, max_lock=6)
        self.assertTrue(r.feasible)
        self.assertEqual(r.paths[0].path, (0, 3, 2))

    def test_all_paths_exceed_limit(self):
        r = solve(net(self.edges, self.n), 0, 2, 10, max_lock=5)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "lock_limit_exceeded")
        self.assertIn("minimum achievable lock total is 6", r.bottleneck.detail)
        self.assertIn("per-path", r.bottleneck.detail)

    def test_lock_forces_more_expensive_route(self):
        # A: 免费路径，总锁定期 10；B: 固定费 5 的路径，总锁定期 3。
        edges = [
            (0, 1, 100, 0, 0, 10),
            (1, 2, 100, 0, 0, 0),
            (0, 3, 100, 5, 0, 3),
            (3, 2, 100, 0, 0, 0),
        ]
        r0 = solve(net(edges, 4), 0, 2, 10)
        self.assertEqual(r0.source_total, 10)
        self.assertEqual(r0.paths[0].path, (0, 1, 2))
        r = solve(net(edges, 4), 0, 2, 10, max_lock=3)
        self.assertTrue(r.feasible)
        self.assertEqual(r.source_total, 15)
        (pb,) = r.paths
        self.assertEqual(pb.path, (0, 3, 2))
        self.assertEqual(pb.lock_total, 3)
        self.assertEqual(
            hop_rows(pb),
            [
                ((0, 3), 10, 5, 15, 3),
                ((3, 2), 10, 0, 10, 0),
            ],
        )


class SharedBottleneckAndLockTests(unittest.TestCase):
    def build(self):
        # 三条路径在 4->5 汇合（共享边，增量 4）；分支首跳增量 0，
        # 第二跳增量 1，故每条路径总锁定期都是 0+1+4 = 5。
        return [
            (0, 1, 40, 1, 0, 0),
            (1, 4, 40, 2, 5_000, 1),
            (0, 2, 40, 1, 0, 0),
            (2, 4, 40, 0, 0, 1),
            (0, 3, 40, 1, 0, 0),
            (3, 4, 40, 0, 0, 1),
            (4, 5, 100, 1, 1_000, 4),
        ]

    def test_shared_balance_still_cumulative_lock_is_per_path(self):
        network = net(self.build(), 6)
        r = solve(network, 0, 5, 60, max_lock=5)
        self.assertTrue(r.feasible)
        # 每条选中路径的总锁定期都是 5（等号允许），不是跨路径相加
        for pb in r.paths:
            self.assertEqual(pb.lock_total, 5)
            self.assertLessEqual(pb.lock_total, 5)
            # 共享末跳到期恰为该边增量 4
            self.assertEqual(pb.hops[-1].edge.dst, 5)
            self.assertEqual(pb.hops[-1].expiry, 4)
            # 首跳到期 = 路径总期限
            self.assertEqual(pb.hops[0].expiry, 5)
        # 余额仍按实发额跨路径累计
        shared = [
            u
            for u in r.edge_usages
            if (u.edge.src, u.edge.dst) == (4, 5)
        ]
        self.assertEqual(len(shared), 1)
        self.assertLessEqual(shared[0].sent_total, 100)
        self.assertEqual(sum(p.delivered for p in r.paths), 60)

    def test_tight_lock_removes_split_alternative(self):
        # max_lock=4：三条路径总锁定期都是 5，全部被过滤
        network = net(self.build(), 6)
        r = solve(network, 0, 5, 60, max_lock=4)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "lock_limit_exceeded")
        self.assertIn("minimum achievable lock total is 5", r.bottleneck.detail)

    def test_disjoint_paths_split_under_equality_lock(self):
        # 两条不相交免费路径，各自容量 6、总锁定期 1；D=10 必须拆分，
        # max_lock=1（恰等号）拆分仍可行。
        edges = [
            (0, 1, 6, 0, 0, 0), (1, 3, 6, 0, 0, 1),
            (0, 2, 6, 0, 0, 0), (2, 3, 6, 0, 0, 1),
        ]
        r = solve(net(edges, 4), 0, 3, 10, max_lock=1)
        self.assertTrue(r.feasible)
        self.assertEqual(r.used_paths, 2)
        self.assertEqual(sorted(p.delivered for p in r.paths), [4, 6])
        self.assertTrue(all(p.lock_total == 1 for p in r.paths))
        # max_lock=0：两条路径都被过滤（末跳增量 1）
        r0 = solve(net(edges, 4), 0, 3, 10, max_lock=0)
        self.assertFalse(r0.feasible)
        self.assertEqual(r0.bottleneck.kind, "lock_limit_exceeded")


class BalanceVsLockPrecedenceTests(unittest.TestCase):
    def test_unreachable_takes_priority(self):
        network = net([(0, 1, 100, 0, 0, 9)], 3)
        r = solve(network, 0, 2, 5, max_lock=0)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "unreachable")

    def test_insufficient_balance_among_lock_allowed_paths(self):
        # 短锁路径 A 容量只有 3（总锁 1）；长锁路径 B 容量充足（总锁 10）。
        edges = [
            (0, 1, 3, 0, 0, 1), (1, 2, 3, 0, 0, 0),
            (0, 3, 100, 0, 0, 10), (3, 2, 100, 0, 0, 0),
        ]
        r = solve(net(edges, 4), 0, 2, 5, max_lock=2)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "insufficient_balance")
        self.assertIn("at most 3 of 5", r.bottleneck.detail)
        self.assertIn("lock total <= 2", r.bottleneck.detail)
        # 放宽期限后长锁路径免费交付 5（源付 5）
        r2 = solve(net(edges, 4), 0, 2, 5, max_lock=10)
        self.assertTrue(r2.feasible)
        self.assertEqual(r2.source_total, 5)

    def test_capable_paths_all_exceed_lock_reports_lock(self):
        # 短锁路径连 1 都交不起（余额 0）；唯一能交付的路径锁定期 10。
        edges = [
            (0, 1, 0, 0, 0, 1), (1, 2, 100, 0, 0, 0),
            (0, 3, 100, 0, 0, 10), (3, 2, 100, 0, 0, 0),
        ]
        r = solve(net(edges, 4), 0, 2, 5, max_lock=2)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "lock_limit_exceeded")
        self.assertIn("minimum achievable lock total is 10", r.bottleneck.detail)

    def test_delivery_zero_echoes_max_lock(self):
        network = net([(0, 1, 100, 0, 0, 9)], 2)
        r = solve(network, 0, 1, 0, max_lock=0)
        self.assertTrue(r.feasible)
        self.assertEqual(r.source_total, 0)
        self.assertEqual(r.paths, ())
        self.assertEqual(r.max_lock, 0)


class LockValidationTests(unittest.TestCase):
    def test_bad_lock_delta(self):
        for fn in [
            lambda: Edge(0, 1, 10, lock_delta=-1),
            lambda: Edge(0, 1, 10, lock_delta=1.5),
            lambda: Edge(0, 1, 10, lock_delta=True),
            lambda: Edge(0, 1, 10, lock_delta="1"),
        ]:
            with self.assertRaises(NetworkValidationError):
                fn()

    def test_bad_max_lock(self):
        network = net([(0, 1, 100, 0, 0), (1, 2, 100, 0, 0)], 3)
        for fn in [
            lambda: solve(network, 0, 2, 5, max_lock=-1),
            lambda: solve(network, 0, 2, 5, max_lock=True),
            lambda: solve(network, 0, 2, 5, max_lock=1.5),
            lambda: solve(network, 0, 2, 5, max_lock="3"),
        ]:
            with self.assertRaises(NetworkValidationError):
                fn()

    def test_default_and_none_identical(self):
        edges = [
            (0, 1, 100, 0, 0, 10),
            (1, 2, 100, 0, 0, 20),
        ]
        network = net(edges, 3)
        r1 = solve(network, 0, 2, 5)
        r2 = solve(network, 0, 2, 5, max_lock=None)
        self.assertEqual(r1.source_total, r2.source_total)
        self.assertEqual(
            [p.lock_total for p in r1.paths], [p.lock_total for p in r2.paths]
        )

    def test_lock_delta_propagates_through_network_relabel(self):
        # Network 内部重编号不能丢字段
        network = Network([Edge(2, 5, 10, 0, 0, 7), Edge(5, 9, 10, 0, 0, 1)])
        r = solve(network, 2, 9, 1)
        self.assertTrue(r.feasible)
        self.assertEqual(r.paths[0].lock_total, 8)
        self.assertEqual([h.edge.lock_delta for h in r.paths[0].hops], [7, 1])
        self.assertEqual([h.expiry for h in r.paths[0].hops], [8, 1])


class CrossValidateLockTests(unittest.TestCase):
    """带锁定增量的随机小网络：与独立穷举参考逐字段对拍。"""

    def test_against_reference_with_locks(self):
        rng = random.Random(20260924)
        mismatches = []
        n_cases = 0
        for seed in range(220):
            n = rng.choice([3, 4, 4, 5])
            all_edges = [(u, v) for u in range(n) for v in range(n) if u != v]
            m = rng.randint(n - 1, min(len(all_edges), 9))
            pairs = rng.sample(all_edges, m)
            edges = []
            for u, v in pairs:
                bal = rng.choice([0, 1, 2, 4, 8, 12, 50])
                b = rng.choice([0, 0, 0, 1, 2, 5])
                r = rng.choice([0, 0, 1, 1000, 500_000, 999_999, 1_000_000])
                dl = rng.choice([0, 0, 0, 1, 2, 3, 6])
                edges.append((u, v, bal, b, r, dl))
            s, t = rng.sample(range(n), 2)
            D = rng.randint(1, 12)
            for ml in [None, 0, 1, 2, 3, 6, 1000]:
                n_cases += 1
                network = Network([Edge(*e) for e in edges], node_count=n)
                got = solve(network, s, t, D, max_lock=ml)
                status, payload = reference_solve(edges, n, s, t, D, max_lock=ml)
                emap = edge_lookup(edges)

                if status == "unreachable":
                    if got.feasible or got.bottleneck.kind != "unreachable":
                        mismatches.append((seed, ml, "unreachable", got.bottleneck))
                    continue

                if status == "lock_exceeded":
                    if got.feasible:
                        mismatches.append((seed, ml, "expected lock_exceeded", got))
                        continue
                    if got.bottleneck.kind != "lock_limit_exceeded":
                        mismatches.append(
                            (seed, ml, "wrong kind", got.bottleneck.kind)
                        )
                        continue
                    if f"minimum achievable lock total is {payload}" not in (
                        got.bottleneck.detail
                    ):
                        mismatches.append(
                            (seed, ml, f"min lock {payload}", got.bottleneck.detail)
                        )
                    continue

                if status == "infeasible":
                    if got.feasible:
                        mismatches.append((seed, ml, "expected infeasible", got))
                        continue
                    if got.bottleneck.kind != "insufficient_balance":
                        mismatches.append(
                            (seed, ml, "wrong kind", got.bottleneck.kind)
                        )
                        continue
                    if f"at most {payload} of {D}" not in got.bottleneck.detail:
                        mismatches.append(
                            (seed, ml, f"achievable {payload}", got.bottleneck.detail)
                        )
                    continue

                # feasible
                if not got.feasible:
                    mismatches.append((seed, ml, "expected feasible", got.bottleneck))
                    continue
                if got.source_total != payload["cost"]:
                    mismatches.append(
                        (seed, ml, f"cost {got.source_total} vs {payload['cost']}")
                    )
                    continue
                got_paths = [pb.path for pb in got.paths]
                exp_paths = [tuple(p) for p in payload["paths"]]
                if got_paths != exp_paths:
                    mismatches.append((seed, ml, "paths", got_paths, exp_paths))
                    continue
                got_alloc = [pb.delivered for pb in got.paths]
                if got_alloc != payload["allocs"]:
                    mismatches.append((seed, ml, "alloc", got_alloc, payload["allocs"]))
                    continue
                if [pb.lock_total for pb in got.paths] != payload["locks"]:
                    mismatches.append(
                        (seed, ml, "locks", [pb.lock_total for pb in got.paths],
                         payload["locks"])
                    )
                    continue
                # 逐跳到期与参考独立直算的后缀和一致
                for pb, path in zip(got.paths, exp_paths):
                    exp_exp = hop_expiries(emap, list(path))
                    got_exp = [h.expiry for h in pb.hops]
                    if got_exp != exp_exp:
                        mismatches.append((seed, ml, "expiry", path, got_exp, exp_exp))
                        break
                else:
                    # 往返自洽：到期非负、首跳=总期限、末跳=末边增量
                    for pb in got.paths:
                        assert pb.hops[0].expiry == pb.lock_total
                        assert pb.hops[-1].expiry >= 0
                        if ml is not None:
                            assert pb.lock_total <= ml

        if mismatches:
            preview = "\n".join(str(m[:3]) for m in mismatches[:10])
            self.fail(
                f"{len(mismatches)} mismatches among {n_cases} cases, first:\n{preview}"
            )


if __name__ == "__main__":
    unittest.main()

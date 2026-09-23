"""求解器场景测试：共享瓶颈、固定费抑制拆分、取整、无路等。"""

import unittest

from mp_split import Edge, Network, solve
from mp_split.errors import NetworkValidationError, SearchBudgetExhausted


def net(edges, n=None):
    return Network([Edge(*e) for e in edges], node_count=n)


class BasicScenarioTests(unittest.TestCase):
    def test_single_path_reverse_recursion(self):
        # 0->1 (b=1,r=0), 1->2 (b=2,r=0)，D=10：
        # 第二跳需送 12（fee 2），第一跳需送 13（fee 1），源付 13
        network = net([(0, 1, 100, 1, 0), (1, 2, 100, 2, 0)], 3)
        r = solve(network, 0, 2, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.source_total, 13)
        self.assertEqual(r.used_paths, 1)
        (pb,) = r.paths
        self.assertEqual(pb.path, (0, 1, 2))
        self.assertEqual(
            [(h.delivered, h.fee, h.sent) for h in pb.hops],
            [(12, 1, 13), (10, 2, 12)],
        )

    def test_rounding_ppm_ceil(self):
        # 单边 r=1, 发送 1：fee=ceil(1e-6)=1，收到 0；
        # 要收到 1，需发 x 使 x-ceil(x/1e6)>=1 -> x=2
        network = net([(0, 1, 100, 0, 1)], 2)
        r = solve(network, 0, 1, 1)
        self.assertTrue(r.feasible)
        self.assertEqual(r.source_total, 2)
        self.assertEqual(r.paths[0].hops[0].fee, 1)

    def test_no_route(self):
        network = net([(0, 1, 100, 0, 0)], 3)
        r = solve(network, 0, 2, 10)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "unreachable")

    def test_insufficient_balance_single(self):
        # 需要发 13，边余额只有 12
        network = net([(0, 1, 12, 1, 0), (1, 2, 12, 2, 0)], 3)
        r = solve(network, 0, 2, 10)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "insufficient_balance")

    def test_balance_charged_on_sent_not_delivered(self):
        # 余额按“实际发送额”：交付 5、fee 高，发 10 才够，余额 9 不可行
        network = net([(0, 1, 9, 5, 0)], 2)
        r = solve(network, 0, 1, 5)
        self.assertFalse(r.feasible)
        network2 = net([(0, 1, 10, 5, 0)], 2)
        r2 = solve(network2, 0, 1, 5)
        self.assertTrue(r2.feasible)
        self.assertEqual(r2.source_total, 10)

    def test_delivery_zero(self):
        network = net([(0, 1, 100, 0, 0)], 2)
        r = solve(network, 0, 1, 0)
        self.assertTrue(r.feasible)
        self.assertEqual(r.source_total, 0)
        self.assertEqual(r.paths, ())


class SharedBottleneckTests(unittest.TestCase):
    def test_shared_edge_cumulative_balance(self):
        # 两条路径共享边 1->2，余额 10；忽略固定费时总额最多交付 10。
        # 拓扑：0->1->2->3 与 0->4->1->2->5->3（构造真正共享 1->2）
        edges = [
            (0, 1, 100, 0, 0),
            (1, 2, 10, 0, 0),
            (2, 3, 100, 0, 0),
            (0, 4, 100, 0, 0),
            (4, 1, 100, 0, 0),
            (2, 5, 100, 0, 0),
            (5, 3, 100, 0, 0),
        ]
        network = net(edges, 6)
        r = solve(network, 0, 3, 10)
        self.assertTrue(r.feasible)
        # 共享边总占用恰为 10
        usage = [u for u in r.edge_usages if (u.edge.src, u.edge.dst) == (1, 2)]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0].sent_total, 10)
        # D=11 不可行，瓶颈说明给出最多 10
        r11 = solve(network, 0, 3, 11)
        self.assertFalse(r11.feasible)
        self.assertIn("at most 10 of 11", r11.bottleneck.detail)

    def test_split_helps_when_parallel_distinct(self):
        # 两条边不相交路径各容量 6，D=10：必须拆分
        edges = [
            (0, 1, 6, 0, 0), (1, 3, 6, 0, 0),
            (0, 2, 6, 0, 0), (2, 3, 6, 0, 0),
        ]
        network = net(edges, 4)
        r = solve(network, 0, 3, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.used_paths, 2)
        self.assertEqual(r.source_total, 10)
        delivered = sorted(p.delivered for p in r.paths)
        self.assertEqual(delivered, [4, 6])


class FixedFeeSuppressesSplitTests(unittest.TestCase):
    def test_fixed_fee_makes_single_path_cheaper(self):
        # 两条费用相同的并行路径，每条固定费 5。D=10：
        # 单路径 15；拆成两条 5+5 要付 20。必须选单路径。
        edges = [
            (0, 1, 100, 5, 0), (1, 3, 100, 0, 0),
            (0, 2, 100, 5, 0), (2, 3, 100, 0, 0),
        ]
        network = net(edges, 4)
        r = solve(network, 0, 3, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.used_paths, 1)
        self.assertEqual(r.source_total, 15)

    def test_three_paths_still_beats_expensive_one(self):
        # 更便宜的多路径应击败更贵的单路径（费用优先于路径数）。
        # 路径 A 两跳高额固定费且容量仅 2（交付 2 需首跳发 20）；
        # B、C 零费但各自容量 5。
        edges = [
            (0, 1, 20, 9, 0), (1, 4, 20, 9, 0),       # A: +18, cap 2
            (0, 2, 5, 0, 0), (2, 4, 5, 0, 0),          # B 容量 5
            (0, 3, 5, 0, 0), (3, 4, 5, 0, 0),          # C 容量 5
        ]
        network = net(edges, 5)
        r = solve(network, 0, 4, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.used_paths, 2)
        self.assertEqual(r.source_total, 10)
        # D=11：B+C 零费容量各 5（最多 10），必须启用 A 补 1，共 3 条。
        # A 交付 1 的源付为 19（两跳各 9），源总支付 = 19 + 10 = 29。
        r11 = solve(network, 0, 4, 11)
        self.assertTrue(r11.feasible)
        self.assertEqual(r11.source_total, 29)
        self.assertEqual(r11.used_paths, 3)


class TieBreakTests(unittest.TestCase):
    def test_lexicographic_path_choice(self):
        # 两条完全等价并行路径：(0,1,3) 与 (0,2,3)，字典序前者先
        edges = [
            (0, 1, 100, 0, 0), (1, 3, 100, 0, 0),
            (0, 2, 100, 0, 0), (2, 3, 100, 0, 0),
        ]
        network = net(edges, 4)
        r = solve(network, 0, 3, 10)
        self.assertEqual(r.paths[0].path, (0, 1, 3))

    def test_allocation_deterministic_on_equal_cost(self):
        edges = [
            (0, 1, 100, 0, 0), (1, 3, 100, 0, 0),
            (0, 2, 100, 0, 0), (2, 3, 100, 0, 0),
        ]
        network = net(edges, 4)
        r1 = solve(network, 0, 3, 10)
        r2 = solve(network, 0, 3, 10)
        self.assertEqual(
            [p.delivered for p in r1.paths], [p.delivered for p in r2.paths]
        )


class InputAndBudgetTests(unittest.TestCase):
    def setUp(self):
        self.network = net([(0, 1, 100, 0, 0), (1, 2, 100, 0, 0)], 3)

    def test_bad_solve_arguments(self):
        for fn in [
            lambda: solve("x", 0, 1, 5),
            lambda: solve(self.network, 0, 0, 5),
            lambda: solve(self.network, 0, 2, -1),
            lambda: solve(self.network, 0, 2, 101),
            lambda: solve(self.network, 0, 2, 5, max_paths=0),
            lambda: solve(self.network, 0, 2, 5, max_paths=4),
            lambda: solve(self.network, 0, 2, 5, budget=0),
            lambda: solve(self.network, 0, 2, True),
            lambda: solve(self.network, 7, 2, 5),
        ]:
            self.assertRaises(NetworkValidationError, fn)

    def test_budget_exhaustion_is_not_infeasibility(self):
        # budget=1 在需要多叶子评估的问题上必然耗尽
        edges = [
            (0, 1, 6, 0, 0), (1, 3, 6, 0, 0),
            (0, 2, 6, 0, 0), (2, 3, 6, 0, 0),
        ]
        network = net(edges, 4)
        with self.assertRaises(SearchBudgetExhausted):
            solve(network, 0, 3, 10, budget=1)


if __name__ == "__main__":
    unittest.main()

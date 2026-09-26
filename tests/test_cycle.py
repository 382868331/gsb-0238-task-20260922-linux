"""循环付款规划测试：简单环枚举独立核对、无环、首边费用超额、并列环取舍。"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mp_split import CycleResult, Edge, Network, solve, solve_cycle
from mp_split.errors import NetworkValidationError, SearchBudgetExhausted
from tests.reference import reference_cycle


def net(edges, n=None):
    return Network([Edge(*e) for e in edges], node_count=n)


def hop_triples(result):
    return [(h.delivered, h.fee, h.sent) for h in result.hops]


class CycleScenarioTests(unittest.TestCase):
    def test_two_edge_cycle_reverse_recursion(self):
        # 0->1 (b=1), 1->0 (b=2)：末跳收回 10 需发 12，首跳需发 13
        network = net([(0, 1, 100, 1, 0), (1, 0, 100, 2, 0)], 2)
        r = solve_cycle(network, 0, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.cycle, (0, 1, 0))
        self.assertEqual(r.source_total, 13)
        self.assertEqual(r.amount, 10)
        self.assertEqual(hop_triples(r), [(12, 1, 13), (10, 2, 12)])
        for h in r.hops:
            self.assertEqual(h.sent - h.fee, h.delivered)

    def test_two_edge_cycle_is_not_zero_length(self):
        # 首尾同属节点 0 但不是零长度路径：必须真走 0->1->0 两条边
        network = net([(0, 1, 50, 3, 0), (1, 0, 50, 0, 0)], 2)
        r = solve_cycle(network, 0, 7)
        self.assertTrue(r.feasible)
        self.assertEqual(len(r.hops), 2)
        self.assertEqual(r.source_total, 10)  # 7 + 首边固定费 3

    def test_no_cycle(self):
        network = net([(0, 1, 100, 0, 0), (1, 2, 100, 0, 0)], 3)
        r = solve_cycle(network, 0, 10)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "no_cycle")
        self.assertIsNone(r.source_total)

    def test_first_edge_fee_overflow(self):
        # 手续费导致首边超额：末跳发 10，首跳需发 10+5=15 > 余额 12
        network = net([(0, 1, 12, 5, 0), (1, 0, 100, 0, 0)], 2)
        r = solve_cycle(network, 0, 10)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "insufficient_balance")
        self.assertIn("returnable at most", r.bottleneck.detail)
        # 余额放宽到 15 则可行
        network2 = net([(0, 1, 15, 5, 0), (1, 0, 100, 0, 0)], 2)
        self.assertTrue(solve_cycle(network2, 0, 10).feasible)

    def test_ppm_fee_overflow_on_first_edge(self):
        # 比例费向上取整：收回 10，末跳免费发 10，首跳 ppm=200000
        # x - ceil(x/5) >= 10 -> x = 13（ceil(13/5)=3），余额 12 不够
        network = net([(0, 1, 12, 0, 200_000), (1, 0, 100, 0, 0)], 2)
        r = solve_cycle(network, 0, 10)
        self.assertFalse(r.feasible)
        network2 = net([(0, 1, 13, 0, 200_000), (1, 0, 100, 0, 0)], 2)
        r2 = solve_cycle(network2, 0, 10)
        self.assertTrue(r2.feasible)
        self.assertEqual(r2.source_total, 13)

    def test_tie_two_cycles_prefers_edge_order(self):
        # 两条环费用、跳数完全相同：取边编号元组更小者（0->1->0）
        network = net(
            [
                (0, 1, 100, 1, 0),  # 边 0
                (1, 0, 100, 0, 0),  # 边 1
                (0, 2, 100, 1, 0),  # 边 2
                (2, 0, 100, 0, 0),  # 边 3
            ],
            3,
        )
        r = solve_cycle(network, 0, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.cycle, (0, 1, 0))
        self.assertEqual(r.source_total, 11)

    def test_tie_cost_prefers_fewer_hops(self):
        # 两跳环与三跳环同费用：取跳数更少者
        network = net(
            [
                (0, 1, 100, 1, 0),  # 边 0：两跳环首边
                (1, 0, 100, 0, 0),  # 边 1
                (0, 2, 100, 0, 0),  # 边 2：三跳环（免费）
                (2, 3, 100, 1, 0),  # 边 3
                (3, 0, 100, 0, 0),  # 边 4
            ],
            4,
        )
        r = solve_cycle(network, 0, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.cycle, (0, 1, 0))
        self.assertEqual(len(r.hops), 2)

    def test_cheaper_longer_cycle_beats_shorter(self):
        # 源支出优先于跳数：三跳环免费，击败两跳环的固定费
        network = net(
            [
                (0, 1, 100, 5, 0),  # 两跳环首边，固定费 5
                (1, 0, 100, 0, 0),
                (0, 2, 100, 0, 0),  # 三跳环，全免费
                (2, 3, 100, 0, 0),
                (3, 0, 100, 0, 0),
            ],
            4,
        )
        r = solve_cycle(network, 0, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.cycle, (0, 2, 3, 0))
        self.assertEqual(r.source_total, 10)

    def test_intermediate_nodes_not_repeated(self):
        # 0->1->2->1->0 不是简单环（中间节点 1 重复），只能走 0->1->0
        network = net(
            [
                (0, 1, 100, 0, 0),
                (1, 2, 100, 0, 0),
                (2, 1, 100, 0, 0),
                (1, 0, 100, 0, 0),
            ],
            3,
        )
        r = solve_cycle(network, 0, 5)
        self.assertTrue(r.feasible)
        self.assertEqual(r.cycle, (0, 1, 0))

    def test_cycle_from_nonzero_node(self):
        network = net([(0, 1, 100, 1, 0), (1, 0, 100, 2, 0)], 2)
        r = solve_cycle(network, 1, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.cycle, (1, 0, 1))
        # 从 1 出发：末边 0->1 (b=1) 交付 10 需发 11，首边 1->0 (b=2) 发 13
        self.assertEqual(hop_triples(r), [(11, 2, 13), (10, 1, 11)])
        self.assertEqual(r.source_total, 13)

    def test_balances_not_modified(self):
        network = net([(0, 1, 100, 1, 0), (1, 0, 100, 2, 0)], 2)
        before = [(e.balance, e.base_fee, e.ppm) for e in network.edges]
        solve_cycle(network, 0, 10)
        after = [(e.balance, e.base_fee, e.ppm) for e in network.edges]
        self.assertEqual(before, after)

    def test_result_is_value_object(self):
        network = net([(0, 1, 100, 0, 0), (1, 0, 100, 0, 0)], 2)
        r = solve_cycle(network, 0, 3)
        self.assertIsInstance(r, CycleResult)
        with self.assertRaises(Exception):
            r.source_total = 999  # frozen dataclass


class CycleValidationTests(unittest.TestCase):
    def test_node_must_exist(self):
        network = net([(0, 1, 100, 0, 0), (1, 0, 100, 0, 0)], 2)
        with self.assertRaises(NetworkValidationError):
            solve_cycle(network, 5, 10)

    def test_amount_bounds(self):
        network = net([(0, 1, 100, 0, 0), (1, 0, 100, 0, 0)], 2)
        with self.assertRaises(NetworkValidationError):
            solve_cycle(network, 0, -1)
        with self.assertRaises(NetworkValidationError):
            solve_cycle(network, 0, 101)
        with self.assertRaises(NetworkValidationError):
            solve_cycle(network, 0, True)
        with self.assertRaises(NetworkValidationError):
            solve_cycle(network, 0, 1.5)

    def test_budget_validation_and_exhaustion(self):
        network = net([(0, 1, 100, 0, 0), (1, 0, 100, 0, 0)], 2)
        with self.assertRaises(NetworkValidationError):
            solve_cycle(network, 0, 10, budget=0)
        two_cycles = net(
            [(0, 1, 100, 0, 0), (1, 0, 100, 0, 0), (0, 2, 100, 0, 0), (2, 0, 100, 0, 0)],
            3,
        )
        with self.assertRaises(SearchBudgetExhausted):
            solve_cycle(two_cycles, 0, 10, budget=1)

    def test_zero_amount_trivial_cycle(self):
        network = net(
            [(0, 1, 100, 9, 0), (1, 0, 100, 9, 0), (0, 2, 100, 0, 0), (2, 0, 100, 0, 0)],
            3,
        )
        r = solve_cycle(network, 0, 0)
        self.assertTrue(r.feasible)
        self.assertEqual(r.source_total, 0)
        self.assertEqual(len(r.hops), 2)
        self.assertTrue(all(h.sent == 0 and h.fee == 0 for h in r.hops))


class CycleCrossValidateTests(unittest.TestCase):
    """小图枚举简单环独立核对：期望值来自 tests.reference（不导入被测核心）。"""

    def test_random_small_graphs(self):
        rng = random.Random(20260926)
        cases = 0
        for _ in range(400):
            n = rng.randint(2, 5)
            pairs = [(u, v) for u in range(n) for v in range(n) if u != v]
            rng.shuffle(pairs)
            m = rng.randint(0, min(8, len(pairs)))
            edges = [
                (
                    u,
                    v,
                    rng.randint(0, 60),
                    rng.randint(0, 4),
                    rng.choice([0, 0, 0, 1, 5_000, 200_000, 1_000_000]),
                )
                for u, v in pairs[:m]
            ]
            s = rng.randrange(n)
            D = rng.randint(0, 30)
            network = net(edges, n)
            got = solve_cycle(network, s, D)
            status, payload = reference_cycle(edges, n, s, D)
            if status == "no_cycle":
                self.assertFalse(got.feasible)
                self.assertEqual(got.bottleneck.kind, "no_cycle")
            elif status == "infeasible":
                self.assertFalse(got.feasible)
                self.assertEqual(got.bottleneck.kind, "insufficient_balance")
            else:
                self.assertTrue(got.feasible, msg=f"{edges} s={s} D={D}")
                self.assertEqual(got.source_total, payload["cost"])
                self.assertEqual(got.cycle, payload["nodes"])
                self.assertEqual(len(got.hops), len(payload["edge_idxs"]))
                got_edges = [(h.edge.src, h.edge.dst) for h in got.hops]
                ref_edges = [(u, v) for u, v, _d, _s in payload["hops"]]
                self.assertEqual(got_edges, ref_edges)
                got_triples = hop_triples(got)
                ref_triples = [(d, s_ - d, s_) for _u, _v, d, s_ in payload["hops"]]
                self.assertEqual(got_triples, ref_triples)
            cases += 1
        self.assertEqual(cases, 400)

    def test_infeasible_detail_matches_reference_max(self):
        rng = random.Random(778899)
        checked = 0
        for _ in range(300):
            n = rng.randint(2, 4)
            pairs = [(u, v) for u in range(n) for v in range(n) if u != v]
            rng.shuffle(pairs)
            m = rng.randint(2, min(6, len(pairs)))
            edges = [
                (u, v, rng.randint(0, 25), rng.randint(0, 6), rng.choice([0, 100_000]))
                for u, v in pairs[:m]
            ]
            s = rng.randrange(n)
            D = rng.randint(10, 40)
            got = solve_cycle(net(edges, n), s, D)
            status, payload = reference_cycle(edges, n, s, D)
            if status == "infeasible":
                self.assertFalse(got.feasible)
                self.assertIn(f"returnable at most {payload} of {D}", got.bottleneck.detail)
                checked += 1
            elif status == "no_cycle":
                self.assertFalse(got.feasible)
            else:
                self.assertTrue(got.feasible)
        self.assertGreater(checked, 0, "随机用例应覆盖到余额不足的环")


class SolveRegressionTests(unittest.TestCase):
    """旧源到目标付款规划不变（冒烟回归，完整回归见 test_solver 等）。"""

    def test_solve_still_works(self):
        network = net([(0, 1, 100, 1, 0), (1, 2, 100, 2, 0)], 3)
        r = solve(network, 0, 2, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.source_total, 13)
        self.assertEqual(r.paths[0].path, (0, 1, 2))


if __name__ == "__main__":
    unittest.main()

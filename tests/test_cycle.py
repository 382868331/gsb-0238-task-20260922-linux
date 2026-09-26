"""循环付款规划（plan_cycle）的测试。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mp_split import (
    CycleResult,
    Edge,
    Network,
    NetworkValidationError,
    SearchBudgetExhausted,
    plan_cycle,
)
from mp_split.errors import MPSplitError

from tests.cycle_reference import reference_cycle


def raw_edges(net: Network):
    return [
        (net.labels[e.src], net.labels[e.dst], e.balance, e.base_fee, e.ppm)
        for e in net.edges
    ]


class CycleBasicsTest(unittest.TestCase):
    def test_two_edge_cycle(self):
        # 0->1 减余额，1->0 增余额：最简 2 边环，不是零长度路径
        net = Network([
            Edge(0, 1, balance=50, base_fee=1, ppm=0),
            Edge(1, 0, balance=50, base_fee=2, ppm=0),
        ])
        r = plan_cycle(net, 0, (0, 1), (1, 0), 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.path, (0, 1, 0))
        self.assertEqual(len(r.hops), 2)
        # 末跳：交付 10，费 2，实发 12；首跳：交付 12，费 1，实发 13
        self.assertEqual(r.hops[1].sent, 12)
        self.assertEqual(r.hops[1].fee, 2)
        self.assertEqual(r.hops[0].sent, 13)
        self.assertEqual(r.source_paid, 13)
        self.assertEqual(r.received, 10)
        for h in r.hops:
            self.assertEqual(h.sent - h.fee, h.delivered)

    def test_network_not_modified(self):
        net = Network([
            Edge(0, 1, balance=50, base_fee=1, ppm=0),
            Edge(1, 0, balance=50, base_fee=2, ppm=0),
        ])
        before = net.edges
        plan_cycle(net, 0, (0, 1), (1, 0), 10)
        self.assertEqual(net.edges, before)

    def test_no_cycle(self):
        # 有首边无回路：1 无法回到 0
        net = Network([
            Edge(0, 1, balance=50, base_fee=0, ppm=0),
            Edge(2, 0, balance=50, base_fee=0, ppm=0),
        ])
        r = plan_cycle(net, 0, (0, 1), (2, 0), 5)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "no_cycle")

    def test_fee_overflows_first_edge(self):
        # 环存在，但费用倒推后首边实发额超过其余额
        net = Network([
            Edge(0, 1, balance=13, base_fee=1, ppm=0),
            Edge(1, 2, balance=50, base_fee=3, ppm=0),
            Edge(2, 0, balance=50, base_fee=0, ppm=0),
        ])
        # 收回 10：2->0 实发 10，1->2 实发 13，0->1 需 14 > 13
        r = plan_cycle(net, 0, (0, 1), (2, 0), 10)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "insufficient_balance")
        # 首边余额放宽到 14 即可行
        net2 = Network([
            Edge(0, 1, balance=14, base_fee=1, ppm=0),
            Edge(1, 2, balance=50, base_fee=3, ppm=0),
            Edge(2, 0, balance=50, base_fee=0, ppm=0),
        ])
        r2 = plan_cycle(net2, 0, (0, 1), (2, 0), 10)
        self.assertTrue(r2.feasible)
        self.assertEqual(r2.source_paid, 14)

    def test_ppm_edge_blocks_cycle(self):
        # ppm >= 1e6 的中间边使任何正金额无法通过
        net = Network([
            Edge(0, 1, balance=1000, base_fee=0, ppm=0),
            Edge(1, 0, balance=1000, base_fee=0, ppm=1_000_000),
        ])
        r = plan_cycle(net, 0, (0, 1), (1, 0), 5)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "insufficient_balance")


class CycleTieBreakTest(unittest.TestCase):
    def test_two_tied_cycles_edge_order(self):
        # 两条费用、跳数都相同的环并列：取边编号序列字典序最先者。
        # 边 0: 0->1, 边 1: 1->3, 边 2: 0->2, 边 3: 2->3, 边 4: 3->0
        # 环 A: 0->1->3->0 (边 0,1,4)；环 B: 0->2->3->0 (边 2,3,4)
        # 但首边固定为 (0,1)，故并列需在同一首边下：再造一条 1->3 平行
        # 不可能（同向边唯一）。改为两条等费中间路径：
        # 环 A: 0->1->3->0，环 B: 0->1->2->3->0？跳数不同。
        # 用同跳数并列：0->1 为首边，3->0 为末边，
        # 中间 1->3 直达（边1）与 1->2->3（边5,6）费用相同但跳数不同。
        edges = [
            Edge(0, 1, balance=100, base_fee=0, ppm=0),   # 0
            Edge(1, 3, balance=100, base_fee=1, ppm=0),   # 1
            Edge(3, 0, balance=100, base_fee=0, ppm=0),   # 2
            Edge(1, 2, balance=100, base_fee=0, ppm=0),   # 3
            Edge(2, 3, balance=100, base_fee=1, ppm=0),   # 4
        ]
        net = Network(edges)
        # 直达：1->3 费 1；绕行：1->2 费 0 + 2->3 费 1，同费 1
        # 直达 3 跳，绕行 4 跳 -> 跳数少者胜
        r = plan_cycle(net, 0, (0, 1), (3, 0), 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.path, (0, 1, 3, 0))
        self.assertEqual(r.source_paid, 11)

    def test_same_cost_same_hops_edge_id_order(self):
        # 费用与跳数都相同的两条环：按边编号序列取先。
        # 首边 (0,1) 边0，末边 (4,0) 边3；中间 1->4 可经 2（边1,2）
        # 或经 3（边4,5），费用相同。
        edges = [
            Edge(0, 1, balance=100, base_fee=0, ppm=0),   # 0
            Edge(1, 2, balance=100, base_fee=1, ppm=0),   # 1
            Edge(2, 4, balance=100, base_fee=1, ppm=0),   # 2
            Edge(4, 0, balance=100, base_fee=0, ppm=0),   # 3
            Edge(1, 3, balance=100, base_fee=1, ppm=0),   # 4
            Edge(3, 4, balance=100, base_fee=1, ppm=0),   # 5
        ]
        net = Network(edges)
        r = plan_cycle(net, 0, (0, 1), (4, 0), 10)
        self.assertTrue(r.feasible)
        # 边序列 (0,1,2,3) < (0,4,5,3)：选经节点 2 的环
        self.assertEqual(r.path, (0, 1, 2, 4, 0))
        self.assertEqual(r.source_paid, 12)

    def test_min_cost_beats_fewer_hops(self):
        # 跳数多但更便宜的环优先（源端支出是第一优先级）
        edges = [
            Edge(0, 1, balance=100, base_fee=0, ppm=0),    # 0
            Edge(1, 4, balance=100, base_fee=5, ppm=0),    # 1 直达但贵
            Edge(4, 0, balance=100, base_fee=0, ppm=0),    # 2
            Edge(1, 2, balance=100, base_fee=0, ppm=0),    # 3
            Edge(2, 3, balance=100, base_fee=0, ppm=0),    # 4
            Edge(3, 4, balance=100, base_fee=0, ppm=0),    # 5
        ]
        net = Network(edges)
        r = plan_cycle(net, 0, (0, 1), (4, 0), 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.path, (0, 1, 2, 3, 4, 0))
        self.assertEqual(r.source_paid, 10)


class CycleValidationTest(unittest.TestCase):
    def setUp(self):
        self.net = Network([
            Edge(0, 1, balance=50, base_fee=0, ppm=0),
            Edge(1, 0, balance=50, base_fee=0, ppm=0),
        ])

    def test_first_edge_must_start_at_node(self):
        with self.assertRaises(NetworkValidationError):
            plan_cycle(self.net, 0, (1, 0), (1, 0), 5)

    def test_last_edge_must_end_at_node(self):
        with self.assertRaises(NetworkValidationError):
            plan_cycle(self.net, 0, (0, 1), (0, 1), 5)

    def test_missing_edge_rejected(self):
        with self.assertRaises(NetworkValidationError):
            plan_cycle(self.net, 0, (0, 2), (1, 0), 5)

    def test_receive_bounds(self):
        with self.assertRaises(NetworkValidationError):
            plan_cycle(self.net, 0, (0, 1), (1, 0), -1)
        with self.assertRaises(NetworkValidationError):
            plan_cycle(self.net, 0, (0, 1), (1, 0), 101)
        with self.assertRaises(NetworkValidationError):
            plan_cycle(self.net, 0, (0, 1), (1, 0), True)

    def test_unknown_node_rejected(self):
        with self.assertRaises(NetworkValidationError):
            plan_cycle(self.net, 9, (0, 1), (1, 0), 5)

    def test_budget_exhausted(self):
        # 两条候选环，预算 1 只够评估一条
        net = Network([
            Edge(0, 1, balance=100, base_fee=0, ppm=0),
            Edge(1, 3, balance=100, base_fee=0, ppm=0),
            Edge(3, 0, balance=100, base_fee=0, ppm=0),
            Edge(1, 2, balance=100, base_fee=0, ppm=0),
            Edge(2, 3, balance=100, base_fee=0, ppm=0),
        ])
        with self.assertRaises(SearchBudgetExhausted):
            plan_cycle(net, 0, (0, 1), (3, 0), 5, budget=1)
        # 预算是“算不动”，不是无解
        self.assertTrue(
            issubclass(SearchBudgetExhausted, MPSplitError)
        )


class CycleCrossValidateTest(unittest.TestCase):
    """小图上与独立参考实现对照（参考不调用被测核心）。"""

    def check(self, edges, node, dec, inc, receive):
        net = Network(edges)
        n = net.node_count
        raw = raw_edges(net)
        status, payload = reference_cycle(raw, n, node, dec, inc, receive)
        r = plan_cycle(net, node, dec, inc, receive)
        if status == "ok":
            self.assertTrue(r.feasible, f"expected feasible, got {r.bottleneck}")
            self.assertEqual(r.source_paid, payload["cost"])
            self.assertEqual(r.path, payload["path"])
            got = [
                (h.edge.src, h.edge.dst, h.delivered, h.sent) for h in r.hops
            ]
            self.assertEqual(got, [tuple(h) for h in payload["hops"]])
            for h in r.hops:
                self.assertEqual(h.sent - h.fee, h.delivered)
                self.assertLessEqual(h.sent, h.edge.balance)
            self.assertEqual(r.hops[-1].delivered, receive)
        else:
            self.assertFalse(r.feasible)
            self.assertEqual(r.bottleneck.kind, status)

    def test_hand_picked_graphs(self):
        # 菱形 + 回路
        edges = [
            Edge(0, 1, balance=40, base_fee=1, ppm=0),
            Edge(1, 2, balance=40, base_fee=2, ppm=5_000),
            Edge(2, 0, balance=40, base_fee=1, ppm=0),
            Edge(0, 3, balance=40, base_fee=0, ppm=0),
            Edge(3, 2, balance=40, base_fee=0, ppm=1_000),
            Edge(1, 3, balance=40, base_fee=3, ppm=0),
        ]
        for receive in (1, 7, 20, 33):
            self.check(edges, 0, (0, 1), (2, 0), receive)
            self.check(edges, 0, (0, 3), (2, 0), receive)

    def test_exhaustive_small_graphs(self):
        # 4 节点候选边池的全部子集（2^8=256 个图）x 若干 receive
        pool = [
            (0, 1), (0, 2), (1, 0), (1, 2),
            (2, 0), (2, 1), (1, 3), (3, 0),
        ]
        fees = [(0, 0), (1, 0), (2, 5_000)]
        count = 0
        for mask in range(1, 1 << len(pool)):
            pairs = [pool[i] for i in range(len(pool)) if mask >> i & 1]
            out0 = [p for p in pairs if p[0] == 0]
            in0 = [p for p in pairs if p[1] == 0]
            if not out0 or not in0:
                continue
            edges = []
            for i, (u, v) in enumerate(pairs):
                b, r = fees[(i + mask) % len(fees)]
                edges.append(Edge(u, v, balance=15 + 5 * (i % 3), base_fee=b, ppm=r))
            for dec in out0:
                for inc in in0:
                    for receive in (1, 5, 12):
                        self.check(edges, 0, dec, inc, receive)
                        count += 1
        self.assertGreater(count, 100)


if __name__ == "__main__":
    unittest.main()

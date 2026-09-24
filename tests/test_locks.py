"""每边锁定增量（期限）功能测试。

期限模型（纯整数、与金额无关）：

* 每条边有非负整数 ``lock_delta``；
* 以收款端到期 0 为基准，到期沿路径**从收款端向源端反向累计**；
* ``HopBill.lock`` 是该跳上游侧到期，``PathBill.source_lock`` 是源端
  累计到期（= 路径各边增量之和）；
* ``solve(..., max_lock=L)`` 过滤掉 ``source_lock > L`` 的路径
  （等号允许）；全部超限时瓶颈为 ``deadline_exceeded``。

所有期望值均手算或由独立参考给出，不调用被测实现生成。
"""

import unittest

from mp_split import Edge, Network, solve
from mp_split.errors import NetworkValidationError


def net(edges, n=None):
    return Network([Edge(*e) for e in edges], node_count=n)


class ReverseAccumulationTests(unittest.TestCase):
    def test_locks_accumulate_backward_single_path(self):
        # 0->1 (b=1, dl=10), 1->2 (b=2, dl=20)，D=10
        # 金额反向递推：末跳送 12，首跳送 13（沿用旧测试，源付 13）
        # 到期反向累计：末跳上游 = 20，首跳上游 = 20+10 = 30
        network = net(
            [(0, 1, 100, 1, 0, 10), (1, 2, 100, 2, 0, 20)], 3
        )
        r = solve(network, 0, 2, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.source_total, 13)
        (pb,) = r.paths
        self.assertEqual(pb.source_lock, 30)
        self.assertEqual(
            [(h.edge.src, h.edge.dst, h.lock) for h in pb.hops],
            [(0, 1, 30), (1, 2, 20)],
        )
        # 相邻跳到期差恰为前一跳边的增量；末跳到期等于末边增量
        self.assertEqual(pb.hops[0].lock - pb.hops[1].lock, 10)
        self.assertEqual(pb.hops[-1].lock, 20)
        # 默认不限制时结果回显 max_lock=None
        self.assertIsNone(r.max_lock)

    def test_lock_independent_of_amount(self):
        # 期限只取决于拓扑上的边增量，与分拆金额无关
        network = net(
            [(0, 1, 100, 1, 0, 7), (1, 2, 100, 2, 0, 11)], 3
        )
        r5 = solve(network, 0, 2, 5)
        r10 = solve(network, 0, 2, 10)
        self.assertTrue(r5.feasible and r10.feasible)
        self.assertEqual(r5.paths[0].source_lock, 18)
        self.assertEqual(r10.paths[0].source_lock, 18)
        self.assertEqual(
            [h.lock for h in r5.paths[0].hops],
            [h.lock for h in r10.paths[0].hops],
        )

    def test_three_hop_chain_cumulative(self):
        # 三跳 dl = 3, 5, 8 -> 末跳 8，中跳 13，首跳 16
        network = net(
            [
                (0, 1, 100, 0, 0, 3),
                (1, 2, 100, 0, 0, 5),
                (2, 3, 100, 0, 0, 8),
            ],
            4,
        )
        r = solve(network, 0, 3, 4)
        self.assertTrue(r.feasible)
        self.assertEqual(r.paths[0].source_lock, 16)
        self.assertEqual([h.lock for h in r.paths[0].hops], [16, 13, 8])


class DeadlineFilterTests(unittest.TestCase):
    """期限过滤改变路径选择：便宜但期限长的路径被剔除。"""

    def build(self):
        # A: 0->1->3 免费，dl 25+25 = 50
        # B: 0->2->3 首跳固定费 5，dl 10+10 = 20；D=10 时源付 15
        return net(
            [
                (0, 1, 100, 0, 0, 25), (1, 3, 100, 0, 0, 25),
                (0, 2, 100, 5, 0, 10), (2, 3, 100, 0, 0, 10),
            ],
            4,
        )

    def test_no_deadline_picks_cheap_long_path(self):
        r = solve(self.build(), 0, 3, 10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.source_total, 10)
        self.assertEqual(r.paths[0].path, (0, 1, 3))
        self.assertEqual(r.paths[0].source_lock, 50)

    def test_equality_is_allowed(self):
        # max_lock 恰等于 A 的累计到期 50：等号不超限，A 仍可选
        r = solve(self.build(), 0, 3, 10, max_lock=50)
        self.assertTrue(r.feasible)
        self.assertEqual(r.source_total, 10)
        self.assertEqual(r.paths[0].path, (0, 1, 3))
        self.assertEqual(r.max_lock, 50)

    def test_deadline_forces_more_expensive_short_path(self):
        # max_lock=20：A(50) 被过滤，只剩 B(20)，源付 15
        r = solve(self.build(), 0, 3, 10, max_lock=20)
        self.assertTrue(r.feasible)
        self.assertEqual(r.source_total, 15)
        (pb,) = r.paths
        self.assertEqual(pb.path, (0, 2, 3))
        self.assertEqual(pb.source_lock, 20)
        self.assertEqual([h.lock for h in pb.hops], [20, 10])

    def test_all_paths_exceed_deadline(self):
        # max_lock=19：最短可达到期是 B 的 20，全部超限
        r = solve(self.build(), 0, 3, 10, max_lock=19)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "deadline_exceeded")
        self.assertIn("20", r.bottleneck.detail)
        self.assertIn("19", r.bottleneck.detail)

    def test_max_lock_zero_keeps_only_zero_delta_paths(self):
        # 只有全 0 增量路径能通过 max_lock=0
        network = net(
            [
                (0, 1, 100, 0, 0, 0), (1, 3, 100, 0, 0, 0),
                (0, 2, 100, 0, 0, 1), (2, 3, 100, 0, 0, 0),
            ],
            4,
        )
        r = solve(network, 0, 3, 10, max_lock=0)
        self.assertTrue(r.feasible)
        self.assertEqual(r.paths[0].path, (0, 1, 3))
        self.assertEqual(r.paths[0].source_lock, 0)


class DeadlinePlusBalanceTests(unittest.TestCase):
    def test_eligible_path_set_shrunk_then_balance_infeasible(self):
        # A: 0->1->3 各边余额 6、dl 5+5=10
        # B: 0->2->3 各边余额 6、dl 30+30=60
        # D=10 零费：两条都在时拆 6+4 可行；max_lock=10 只留 A（cap 6）
        network = net(
            [
                (0, 1, 6, 0, 0, 5), (1, 3, 6, 0, 0, 5),
                (0, 2, 6, 0, 0, 30), (2, 3, 6, 0, 0, 30),
            ],
            4,
        )
        r = solve(network, 0, 3, 10, max_lock=10)
        self.assertFalse(r.feasible)
        # 这是余额不足（期限之后仍有可交付路径），不是 deadline_exceeded
        self.assertEqual(r.bottleneck.kind, "insufficient_balance")
        self.assertIn("at most 6 of 10", r.bottleneck.detail)
        self.assertIn("max_lock 10", r.bottleneck.detail)

        r2 = solve(network, 0, 3, 10, max_lock=60)
        self.assertTrue(r2.feasible)
        self.assertEqual(r2.used_paths, 2)
        self.assertEqual(r2.source_total, 10)
        self.assertEqual(
            sorted(pb.source_lock for pb in r2.paths), [10, 60]
        )

    def test_deadline_says_infeasible_before_balance_check(self):
        # 唯一路径余额足够但期限过长：deadline_exceeded
        network = net([(0, 1, 100, 0, 0, 40)], 2)
        r = solve(network, 0, 1, 10, max_lock=30)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "deadline_exceeded")

    def test_unreachable_takes_precedence(self):
        # 根本无路：仍是 unreachable，不被期限语义吞掉
        network = net([(0, 1, 100, 0, 0, 0)], 3)
        r = solve(network, 0, 2, 10, max_lock=0)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "unreachable")

    def test_noncontiguous_labels_in_deadline_detail(self):
        network = Network([Edge(2, 5, 100, 0, 0, lock_delta=9)])
        r = solve(network, 2, 5, 10, max_lock=3)
        self.assertFalse(r.feasible)
        self.assertEqual(r.bottleneck.kind, "deadline_exceeded")
        self.assertIn("2 -> 5", r.bottleneck.detail)


class LockValidationTests(unittest.TestCase):
    def test_bad_lock_delta_on_edge(self):
        for fn in [
            lambda: Edge(0, 1, 10, 0, 0, -1),
            lambda: Edge(0, 1, 10, lock_delta=True),
            lambda: Edge(0, 1, 10, lock_delta=1.5),
        ]:
            self.assertRaises(NetworkValidationError, fn)

    def test_bad_max_lock(self):
        network = net([(0, 1, 100, 0, 0)], 2)
        for fn in [
            lambda: solve(network, 0, 1, 5, max_lock=-1),
            lambda: solve(network, 0, 1, 5, max_lock=True),
            lambda: solve(network, 0, 1, 5, max_lock=1.5),
            lambda: solve(network, 0, 1, 5, max_lock="10"),
        ]:
            self.assertRaises(NetworkValidationError, fn)

    def test_delivery_zero_echoes_max_lock(self):
        network = net([(0, 1, 100, 0, 0, 100)], 2)
        r = solve(network, 0, 1, 0, max_lock=0)
        self.assertTrue(r.feasible)
        self.assertEqual(r.source_total, 0)
        self.assertEqual(r.max_lock, 0)
        self.assertEqual(r.paths, ())

    def test_lock_delta_defaults_zero(self):
        # 旧 5 参数构造仍合法：lock_delta 缺省 0，期限恒为 0
        network = net([(0, 1, 100, 1, 0), (1, 2, 100, 2, 0)], 3)
        r = solve(network, 0, 2, 10, max_lock=0)
        self.assertTrue(r.feasible)
        self.assertEqual(r.paths[0].source_lock, 0)
        self.assertTrue(all(h.lock == 0 for h in r.paths[0].hops))


if __name__ == "__main__":
    unittest.main()

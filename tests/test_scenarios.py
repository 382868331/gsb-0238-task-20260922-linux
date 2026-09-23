"""专题场景测试：共享瓶颈、固定费抑制拆分、ceil 取整、无路、
路径字典序平局裁决、逐跳账单与每边余额核对。"""

import unittest

from paychan import (
    Edge,
    InfeasibleResult,
    Network,
    SplitResult,
    split_payment,
)


class ScenarioTests(unittest.TestCase):
    def test_shared_bottleneck_actual_amount_accumulates(self):
        # S 有两条支路到 R，但 R->T 只有 40 余额：拆再多也送不过 40。
        net = Network([
            Edge("S", "A", 100), Edge("S", "B", 100),
            Edge("A", "R", 100), Edge("B", "R", 100),
            Edge("R", "T", 40),
        ])
        result = split_payment(net, "S", "T", 60)
        self.assertIsInstance(result, InfeasibleResult)
        self.assertEqual(result.reason, "insufficient_capacity")
        # 瓶颈诊断指向唯一割边 R->T。
        self.assertEqual([b.edge_index for b in result.bottlenecks], [4])
        self.assertEqual(result.bottlenecks[0].margin, 20)

        # D=40 可行，且 R->T 上两条路径的实际转发额累计恰为 40。
        ok = split_payment(net, "S", "T", 40)
        self.assertIsInstance(ok, SplitResult)
        self.assertEqual(ok.edge_usage()[4], 40)
        for eid, used in ok.edge_usage().items():
            self.assertLessEqual(used, net.edges[eid].balance)

    def test_fixed_fee_suppresses_split(self):
        # 两条费用完全对称的路径；每多走一条路径，每条经过的边都收一次
        # 固定费。ppm=0 时比例费为 0，拆成 2 条必然多付 2*b_total。
        b = 2
        net = Network([
            Edge("S", "A", 100, base_fee=b),
            Edge("S", "B", 100, base_fee=b),
            Edge("A", "T", 100, base_fee=b),
            Edge("B", "T", 100, base_fee=b),
        ])
        result = split_payment(net, "S", "T", 30)
        self.assertEqual(result.paths_used, 1)
        # 单路径：2 跳各收 b，源付 30 + 2b。
        self.assertEqual(result.source_total_paid, 30 + 2 * b)
        plan = result.plans[0]
        self.assertEqual(plan.hops[-1].amount, 30)
        self.assertEqual(plan.hops[-1].fee, b)
        self.assertEqual(plan.hops[0].amount, 30 + b)
        self.assertEqual(plan.source_paid, 30 + 2 * b)

    def test_split_happens_when_balance_forces(self):
        # 单条路径容量 6（首跳转发额含下游固定费），D=10 必须拆两条。
        net = Network([
            Edge("S", "A", 8, base_fee=2),
            Edge("S", "B", 8, base_fee=2),
            Edge("A", "T", 6, base_fee=2),
            Edge("B", "T", 6, base_fee=2),
        ])
        result = split_payment(net, "S", "T", 10)
        self.assertEqual(result.paths_used, 2)
        self.assertEqual(sum(p.deliver for p in result.plans), 10)
        self.assertEqual(result.source_total_paid, 10 + 4 * 2)

    def test_ceil_rounding_fee_math(self):
        # ppm=1：转发 1..999_999 时 ceil 至少 1；本库金额 <=100，故只要 x>=1
        # 费用的 ppm 部分就是 1。手工核对递推。
        net = Network([Edge("S", "M", 100, base_fee=0, ppm=1),
                       Edge("M", "T", 100, base_fee=0, ppm=0)])
        result = split_payment(net, "S", "T", 1)
        plan = result.plans[0]
        # M->T 转发 1（费 0）；S->M 须转发 1，ppm 费 ceil(1/1e6)=1。
        self.assertEqual(plan.hops[1].amount, 1)
        self.assertEqual(plan.hops[1].fee, 0)
        self.assertEqual(plan.hops[0].amount, 1)
        self.assertEqual(plan.hops[0].fee, 1)
        self.assertEqual(plan.source_paid, 2)

    def test_ceil_rounding_333333ppm(self):
        # 333_333 ppm：ceil(x/3) 的精确值核对若干点。
        edge = Edge("S", "T", 100, base_fee=0, ppm=333_333)
        self.assertEqual(edge.fee_for(2), 1)   # ceil(0.666..)
        self.assertEqual(edge.fee_for(3), 1)   # ceil(0.999..)
        self.assertEqual(edge.fee_for(4), 2)   # ceil(1.333..)
        self.assertEqual(edge.fee_for(100), 34)  # ceil(33.3333)
        result = split_payment(Network([edge]), "S", "T", 100)
        self.assertEqual(result.source_total_paid, 134)

    def test_no_path_disconnected(self):
        net = Network([Edge("S", "A", 10), Edge("B", "T", 10)])
        result = split_payment(net, "S", "T", 1)
        self.assertIsInstance(result, InfeasibleResult)
        self.assertEqual(result.reason, "no_path")
        self.assertEqual(result.bottlenecks, ())

    def test_no_path_wrong_direction(self):
        net = Network([Edge("T", "S", 100)])  # 有向边方向相反
        result = split_payment(net, "S", "T", 5)
        self.assertEqual(result.reason, "no_path")

    def test_zero_amount_uses_no_paths_and_no_fees(self):
        net = Network([Edge("S", "A", 10, base_fee=5), Edge("A", "T", 10, base_fee=5)])
        result = split_payment(net, "S", "T", 0)
        self.assertEqual(result.plans, ())
        self.assertEqual(result.source_total_paid, 0)
        self.assertEqual(result.paths_used, 0)

    def test_lexicographic_tie_break(self):
        # 两条长度相同、费用为 0、容量充足的路径；应选字典序更小的节点序列。
        net = Network([
            Edge(0, 1, 100), Edge(0, 2, 100),
            Edge(1, 3, 100), Edge(2, 3, 100),
        ])
        result = split_payment(net, 0, 3, 10)
        self.assertEqual(result.paths_used, 1)
        self.assertEqual(result.plans[0].nodes, (0, 1, 3))

    def test_fewer_paths_preferred_on_equal_cost(self):
        # 费用全 0：单路径与拆分源支付相同，优先最少路径（1 条）。
        net = Network([
            Edge("S", "A", 100), Edge("S", "B", 100),
            Edge("A", "T", 100), Edge("B", "T", 100),
        ])
        result = split_payment(net, "S", "T", 10)
        self.assertEqual(result.paths_used, 1)
        self.assertEqual(result.source_total_paid, 10)

    def test_per_hop_bills_chain_consistently(self):
        # 三跳路径：每跳账单首尾相接，inflow(i) = amount(i)+fee(i)，
        # 且 amount(i) = 下游跳的 inflow。
        net = Network([
            Edge("S", "A", 100, base_fee=1, ppm=2000),
            Edge("A", "B", 100, base_fee=0, ppm=5000),
            Edge("B", "T", 100, base_fee=2, ppm=0),
        ])
        result = split_payment(net, "S", "T", 10)
        plan = result.plans[0]
        hops = plan.hops
        self.assertEqual(hops[-1].amount, 10)
        self.assertEqual(hops[-1].inflow, 12)          # 10 + b2
        self.assertEqual(hops[1].amount, 12)          # 中跳转发 12
        self.assertEqual(hops[1].inflow, 12 + 1)      # ceil(12*5000/1e6)=1
        self.assertEqual(hops[0].amount, 13)
        self.assertEqual(hops[0].inflow, 13 + 2)      # b1=1, ceil(13*2000/1e6)=1 → 2
        self.assertEqual(plan.source_paid, 15)
        self.assertEqual(result.source_total_paid, 15)
        for h in hops:
            self.assertEqual(h.inflow, h.amount + h.fee)
            self.assertEqual(h.balance_used, h.amount)

    def test_shared_edge_usage_with_fees(self):
        # 两条路径共享尾部边，金额不同、ppm 非零；累计的是各自转发额。
        net = Network([
            Edge("S", "A", 100, base_fee=0, ppm=0),
            Edge("S", "B", 100, base_fee=0, ppm=0),
            Edge("A", "M", 100, base_fee=0, ppm=10_000),
            Edge("B", "M", 100, base_fee=1, ppm=0),
            Edge("M", "T", 12, base_fee=0, ppm=0),
        ])
        result = split_payment(net, "S", "T", 12)
        self.assertIsInstance(result, SplitResult)
        # 共享边 M->T（索引 4）累计恰为 12，不超过余额。
        self.assertEqual(result.edge_usage()[4], 12)
        for eid, used in result.edge_usage().items():
            self.assertLessEqual(used, net.edges[eid].balance)

    def test_balance_check_is_against_forward_amount_not_inflow(self):
        # 边余额=5，固定费=10：交付 5 时转发额=5<=5 可行，源多付 10；
        # 交付 6 时转发额=6>5 不可行（即使上游送 16 也不行）。
        net = Network([Edge("S", "M", 100), Edge("M", "T", 5, base_fee=10)])
        ok = split_payment(net, "S", "T", 5)
        self.assertIsInstance(ok, SplitResult)
        self.assertEqual(ok.source_total_paid, 15)
        bad = split_payment(net, "S", "T", 6)
        self.assertIsInstance(bad, InfeasibleResult)

    def test_three_path_limit(self):
        # 四条容量各 3 的并行路径，D=10：任意 3 条总容量 9 < 10，不可行。
        spec = []
        for m in (1, 2, 3, 4):
            spec.append(Edge(0, m, 3))
            spec.append(Edge(m, 6, 3))
        net = Network(spec)
        result = split_payment(net, 0, 6, 10)
        self.assertIsInstance(result, InfeasibleResult)
        # D=9 恰好 3 条可行。
        ok = split_payment(net, 0, 6, 9)
        self.assertIsInstance(ok, SplitResult)
        self.assertEqual(ok.paths_used, 3)


if __name__ == "__main__":
    unittest.main()

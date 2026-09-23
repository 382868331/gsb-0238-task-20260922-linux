"""结果结构不变量 + 确定性随机网络对拍（独立参考不导入 paychan）。"""

import random
import unittest

from paychan import Edge, InfeasibleResult, Network, SplitResult, split_payment

try:
    from .reference import ref_split
except ImportError:  # pragma: no cover
    from reference import ref_split


PPM_CHOICES = (0, 1, 1000, 333_333, 500_000, 1_000_000)


class InvariantTests(unittest.TestCase):
    def setUp(self):
        self.net = Network([
            Edge("S", "A", 50, base_fee=1, ppm=2000),
            Edge("S", "B", 50, base_fee=0, ppm=5000),
            Edge("A", "R", 40, base_fee=1, ppm=0),
            Edge("B", "R", 40, base_fee=0, ppm=1000),
            Edge("R", "T", 80, base_fee=1, ppm=3000),
        ])

    def test_delivery_conservation(self):
        for d in range(0, 41):
            r = split_payment(self.net, "S", "T", d)
            if isinstance(r, InfeasibleResult):
                continue
            self.assertEqual(r.amount_delivered, d)
            self.assertLessEqual(r.paths_used, 3)
            self.assertEqual(sum(p.deliver for p in r.plans), d)
            self.assertTrue(all(p.deliver >= 1 for p in r.plans))

    def test_bill_chain_and_source_payment(self):
        r = split_payment(self.net, "S", "T", 30)
        self.assertIsInstance(r, SplitResult)
        for plan in r.plans:
            hops = plan.hops
            # 目的端最后一跳转发额 = 路径交付额。
            self.assertEqual(hops[-1].amount, plan.deliver)
            # 逐跳：inflow = amount + fee；上游跳 amount = 下游跳 inflow。
            for i, h in enumerate(hops):
                self.assertEqual(h.inflow, h.amount + h.fee)
                self.assertEqual(h.balance_used, h.amount)
                self.assertGreaterEqual(h.fee, 0)
                if i:
                    self.assertEqual(hops[i - 1].amount, h.inflow)
            self.assertEqual(hops[0].inflow, plan.source_paid)
            self.assertEqual(plan.source_paid - plan.deliver, plan.total_fee())
        self.assertEqual(sum(p.source_paid for p in r.plans), r.source_total_paid)

    def test_edge_usage_within_balance_and_matches_hops(self):
        r = split_payment(self.net, "S", "T", 35)
        self.assertIsInstance(r, SplitResult)
        recomputed = {}
        for plan in r.plans:
            for h in plan.hops:
                recomputed[h.edge_index] = recomputed.get(h.edge_index, 0) + h.amount
        self.assertEqual(recomputed, r.edge_usage())
        for eid, used in r.edge_usage().items():
            self.assertLessEqual(used, self.net.edges[eid].balance)

    def test_plans_sorted_lexicographically(self):
        r = split_payment(self.net, "S", "T", 40)
        self.assertIsInstance(r, SplitResult)
        keys = [tuple(map(repr, p.nodes)) for p in r.plans]
        self.assertEqual(keys, sorted(keys))


class RandomizedCrossCheck(unittest.TestCase):
    """200 组固定种子随机网络，与独立参考逐项核对（含不可行原因）。"""

    def test_seeded_networks(self):
        rng = random.Random(4242)
        checked = 0
        for _ in range(200):
            n = rng.randint(2, 6)
            all_pairs = [(u, v) for u in range(n) for v in range(n) if u != v]
            rng.shuffle(all_pairs)
            m = rng.randint(1, min(10, len(all_pairs)))
            spec = [
                (u, v, rng.randint(0, 10), rng.randint(0, 2), rng.choice(PPM_CHOICES))
                for (u, v) in all_pairs[:m]
            ]
            present = sorted({x for s in spec for x in s[:2]})
            if len(present) < 2:
                continue
            src, dst = rng.sample(present, 2)
            d = rng.randint(0, 12)

            net = Network([Edge(*s) for s in spec])
            ref_edges = [(i, *s) for i, s in enumerate(spec)]
            result = split_payment(net, src, dst, d)
            ref = ref_split(ref_edges, src, dst, d)

            if isinstance(result, InfeasibleResult):
                self.assertFalse(ref["feasible"])
                self.assertEqual(result.reason, ref["reason"])
                continue
            self.assertTrue(ref["feasible"])
            self.assertEqual(result.source_total_paid, ref["paid"])
            got = {tuple(p.edge_ids): p.deliver for p in result.plans}
            want = {tuple(p): a for p, a in ref["paths"]}
            self.assertEqual(got, want)
            for eid, used in result.edge_usage().items():
                self.assertLessEqual(used, net.edges[eid].balance)
            checked += 1
        self.assertGreater(checked, 50)  # 确保大多数样本确实可行并被核对


if __name__ == "__main__":
    unittest.main()

"""公开 API 面：构造形式、simple_paths 工具、结果属性与类型层级。"""

import unittest

from paychan import (
    DEFAULT_BUDGET,
    Edge,
    InfeasibleResult,
    InvalidInputError,
    Network,
    PaymentSplitError,
    SearchBudgetExhausted,
    SplitResult,
    split_payment,
    simple_paths,
)


class ApiSurfaceTests(unittest.TestCase):
    def test_edge_construction_forms_equivalent(self):
        e1 = Edge("A", "B", 10, 1, 2000)
        n1 = Network([e1])
        n2 = Network([{"src": "A", "dst": "B", "balance": 10, "base_fee": 1, "ppm": 2000}])
        n3 = Network([("A", "B", 10, 1, 2000)])
        n4 = Network([("A", "B", 10)])  # 默认费用 0
        for net, fee in ((n1, 2), (n2, 2), (n3, 2), (n4, 0)):
            self.assertEqual(len(net), 1)
            self.assertEqual(net.edges[0].fee_for(10), fee)

    def test_network_read_only_views(self):
        net = Network([Edge(0, 1, 5), Edge(1, 2, 5)])
        self.assertEqual(net.nodes, frozenset({0, 1, 2}))
        self.assertEqual(net.out_ids(0), (0,))
        self.assertEqual(net.edge_id(1, 2), 1)
        self.assertIsNone(net.edge_id(2, 1))
        self.assertEqual(net.edges[0].balance, 5)

    def test_simple_paths_public(self):
        net = Network([
            Edge("S", "A", 10), Edge("S", "B", 10),
            Edge("A", "T", 10), Edge("B", "T", 10),
            Edge("A", "B", 10),
        ])
        paths = simple_paths(net, "S", "T")
        nodes = [p[0] for p in paths]
        self.assertIn(("S", "A", "T"), nodes)
        self.assertIn(("S", "A", "B", "T"), nodes)
        self.assertIn(("S", "B", "T"), nodes)
        self.assertEqual(nodes, sorted(nodes, key=lambda t: tuple((type(x).__name__, repr(x)) for x in t)))
        with self.assertRaises(InvalidInputError):
            simple_paths(net, "S", "T", budget=0)
        with self.assertRaises(InvalidInputError):
            simple_paths("x", "S", "T")  # type: ignore[arg-type]
        with self.assertRaises(SearchBudgetExhausted):
            simple_paths(net, "S", "T", budget=1)

    def test_result_feasible_flags_and_properties(self):
        net = Network([Edge("S", "A", 10, base_fee=1), Edge("A", "T", 10, base_fee=0)])
        ok = split_payment(net, "S", "T", 4)
        self.assertIsInstance(ok, SplitResult)
        self.assertTrue(ok.feasible)
        self.assertEqual(ok.amount, 4)
        self.assertEqual(ok.source, "S")
        self.assertEqual(ok.destination, "T")
        self.assertEqual(ok.amount_delivered + ok.total_fee, ok.source_total_paid)

        bad = split_payment(net, "S", "T", 100)  # A->T 余额仅 10
        self.assertIsInstance(bad, InfeasibleResult)
        self.assertFalse(bad.feasible)

    def test_zero_amount_disconnected_is_still_empty_success(self):
        net = Network([Edge("S", "A", 10), Edge("B", "T", 10)])
        r = split_payment(net, "S", "T", 0)
        self.assertIsInstance(r, SplitResult)
        self.assertEqual(r.plans, ())
        self.assertEqual(r.source_total_paid, 0)

    def test_exception_hierarchy_and_default_budget(self):
        self.assertIsInstance(InvalidInputError("x", "y"), PaymentSplitError)
        self.assertIsInstance(SearchBudgetExhausted(1, 1), PaymentSplitError)
        self.assertIsInstance(SearchBudgetExhausted(1, 1), RuntimeError)
        self.assertGreater(DEFAULT_BUDGET, 0)

    def test_ppm_one_million_fee_equals_amount(self):
        net = Network([Edge("S", "T", 100, base_fee=0, ppm=1_000_000)])
        r = split_payment(net, "S", "T", 50)
        self.assertEqual(r.source_total_paid, 100)  # fee(x)=x
        self.assertEqual(r.plans[0].hops[0].fee, 50)

    def test_hop_bill_references_real_edge(self):
        net = Network([Edge("S", "T", 10, base_fee=2, ppm=0)])
        r = split_payment(net, "S", "T", 3)
        hop = r.plans[0].hops[0]
        self.assertEqual(hop.edge_index, 0)
        self.assertEqual((hop.src, hop.dst), ("S", "T"))
        self.assertEqual((hop.amount, hop.fee, hop.inflow), (3, 2, 5))


if __name__ == "__main__":
    unittest.main()

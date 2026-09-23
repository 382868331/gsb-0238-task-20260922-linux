"""输入校验与错误语义测试。"""

import math
import unittest

from paychan import (
    Edge,
    InvalidInputError,
    Network,
    NetworkLimitError,
    SearchBudgetExhausted,
    split_payment,
)
from paychan.errors import DuplicateEdgeError, UnknownNodeError


def tiny_net():
    return Network([Edge("A", "B", 10), Edge("B", "C", 10)])


class ValidationTests(unittest.TestCase):
    def test_reject_nan_amount(self):
        net = tiny_net()
        with self.assertRaises(InvalidInputError) as cm:
            split_payment(net, "A", "C", float("nan"))
        self.assertEqual(cm.exception.locator, "amount")

    def test_reject_infinity_amount(self):
        net = tiny_net()
        for bad in (float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError) as cm:
                    split_payment(net, "A", "C", bad)
                self.assertEqual(cm.exception.locator, "amount")

    def test_reject_bool_amount(self):
        net = tiny_net()
        for bad in (True, False):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError) as cm:
                    split_payment(net, "A", "C", bad)
                self.assertEqual(cm.exception.locator, "amount")

    def test_reject_bool_numeric_fields(self):
        with self.assertRaises(InvalidInputError) as cm:
            Edge("A", "B", balance=True)  # type: ignore[arg-type]
        self.assertIn("balance", cm.exception.locator)
        with self.assertRaises(InvalidInputError):
            Edge("A", "B", 10, base_fee=False)  # type: ignore[arg-type]
        with self.assertRaises(InvalidInputError):
            Edge("A", "B", 10, ppm=True)  # type: ignore[arg-type]

    def test_reject_fractional_float_no_silent_rounding(self):
        net = tiny_net()
        with self.assertRaises(InvalidInputError) as cm:
            split_payment(net, "A", "C", 2.5)
        self.assertEqual(cm.exception.locator, "amount")
        # 值恰为整数的 float 可以使用（无需舍入）。
        ok = split_payment(net, "A", "C", 3.0)
        self.assertEqual(ok.amount_delivered, 3)

    def test_reject_negative_and_wrong_types(self):
        net = tiny_net()
        with self.assertRaises(InvalidInputError):
            split_payment(net, "A", "C", -1)
        with self.assertRaises(InvalidInputError):
            split_payment(net, "A", "C", "7")  # type: ignore[arg-type]
        with self.assertRaises(InvalidInputError) as cm:
            split_payment("not-a-net", "A", "C", 1)  # type: ignore[arg-type]
        self.assertEqual(cm.exception.locator, "network")

    def test_amount_limit_100(self):
        net = tiny_net()
        with self.assertRaises(NetworkLimitError) as cm:
            split_payment(net, "A", "C", 101)
        self.assertEqual(cm.exception.locator, "amount")
        self.assertEqual(split_payment(net, "A", "C", 0).amount_delivered, 0)

    def test_node_and_edge_limits(self):
        # 9 个节点。
        edges = [Edge(i, i + 1, 10) for i in range(8)]
        with self.assertRaises(NetworkLimitError):
            Network(edges)
        # 17 条边（节点数不超过 8：用 8 节点完全图的一部分 + …… 直接构造）。
        many = []
        for u in range(8):
            for v in range(8):
                if u != v:
                    many.append((u, v, 10))
        with self.assertRaises(NetworkLimitError):
            Network(many[:17])
        # 恰好 16 条边 / 8 节点是允许的。
        Network(many[:16])

    def test_duplicate_and_self_loop_and_unknown(self):
        with self.assertRaises(DuplicateEdgeError):
            Network([Edge("A", "B", 1), Edge("A", "B", 2)])
        with self.assertRaises(InvalidInputError):
            Edge("A", "A", 1)
        net = tiny_net()
        with self.assertRaises(UnknownNodeError) as cm:
            split_payment(net, "A", "Z", 1)
        self.assertEqual(cm.exception.locator, "destination")
        with self.assertRaises(InvalidInputError):
            split_payment(net, "A", "A", 1)

    def test_edge_field_validation_locators(self):
        with self.assertRaises(InvalidInputError) as cm:
            Network([{"src": "A", "dst": "B", "balance": -1}])
        self.assertIn("balance", cm.exception.locator)
        with self.assertRaises(InvalidInputError):
            Network([{"src": "A", "dst": "B", "balance": float("nan")}])
        with self.assertRaises(InvalidInputError):
            Network([{"dst": "B", "balance": 1}])  # 缺 src
        with self.assertRaises(InvalidInputError):
            Network([42])  # type: ignore[list-item]

    def test_budget_validation(self):
        net = tiny_net()
        for bad in (0, -3, True, 1.5, "100"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError):
                    split_payment(net, "A", "C", 1, budget=bad)  # type: ignore[arg-type]

    def test_budget_exhaustion_is_distinct_from_infeasible(self):
        # 同一网络：预算极小 -> 耗尽（结论未知）；预算充足 -> 确定可行。
        net = tiny_net()
        with self.assertRaises(SearchBudgetExhausted):
            split_payment(net, "A", "C", 1, budget=1)
        result = split_payment(net, "A", "C", 1, budget=1_000_000)
        self.assertEqual(result.source_total_paid, 1)

    def test_nan_in_edge_fee_math_domain(self):
        edge = Edge("A", "B", 10)
        with self.assertRaises(InvalidInputError):
            edge.fee_for(-1)
        self.assertEqual(edge.fee_for(0), 0)


if __name__ == "__main__":
    unittest.main()

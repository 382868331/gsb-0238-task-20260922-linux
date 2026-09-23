"""与独立参考实现的交叉验证（参考实现不导入 paychan）。

小 D 下对多种网络遍历 D=0..上限，比较：
  * 可行性 / 不可行原因；
  * 最优源总支付；
  * 使用路径集合与金额分配；
  * 每边实际转发额占用。
"""

import unittest

from paychan import Edge, InfeasibleResult, Network, SplitResult, split_payment

try:  # 同时兼容 ``python -m unittest discover -s tests`` 与从仓库根目录包式导入。
    from .reference import ref_split
except ImportError:  # pragma: no cover
    from reference import ref_split


def make_case(spec):
    """spec: [(src, dst, balance, base_fee, ppm), ...] -> (Network, ref_edges)。"""
    edges = [Edge(s, t, bal, b, r) for (s, t, bal, b, r) in spec]
    ref_edges = [(i, s, t, bal, b, r) for i, (s, t, bal, b, r) in enumerate(spec)]
    return Network(edges), ref_edges


def unpack(name):
    src, dst, spec = CASES[name]
    return (src, dst) + make_case(spec)


# 一组结构各异的小网络（节点 0..5，边数 <=16）；显式给出每例的源与目的。
CASES = {
    "single": (0, 1, [(0, 1, 100, 1, 1000)]),
    "linear": (0, 2, [(0, 1, 100, 1, 500), (1, 2, 100, 0, 2000)]),
    "diamond": (
        0, 3,
        [
            (0, 1, 60, 1, 0),
            (0, 2, 60, 2, 1000),
            (1, 3, 60, 0, 500),
            (2, 3, 60, 1, 0),
        ],
    ),
    "shared_bottleneck": (
        0, 4,
        [
            (0, 1, 100, 0, 0),
            (0, 2, 100, 0, 0),
            (1, 3, 100, 0, 0),
            (2, 3, 100, 0, 0),
            (3, 4, 45, 0, 0),
        ],
    ),
    "fixed_fee_split": (
        0, 3,
        [
            (0, 1, 100, 3, 0),
            (0, 2, 100, 3, 0),
            (1, 3, 100, 3, 0),
            (2, 3, 100, 3, 0),
        ],
    ),
    "ppm_rounding": (
        0, 3,
        [
            (0, 1, 100, 0, 333_333),
            (0, 2, 100, 0, 1000),
            (1, 3, 100, 0, 333_333),
            (2, 3, 100, 1, 0),
        ],
    ),
    "tight_balances": (
        0, 4,
        [
            (0, 1, 7, 0, 0),
            (0, 2, 6, 0, 0),
            (1, 3, 7, 0, 0),
            (2, 3, 6, 0, 0),
            (3, 4, 13, 0, 0),
        ],
    ),
    "fee_eats_balance": (
        0, 3,
        [
            (0, 1, 100, 0, 0),
            (1, 2, 5, 10, 0),  # 转发额必须 <=5，固定费 10 由上游额外出
            (2, 3, 100, 0, 0),
        ],
    ),
    "parallel_routes_3": (
        0, 5,
        [
            (0, 1, 30, 1, 100),
            (0, 2, 30, 0, 200),
            (0, 4, 30, 2, 0),
            (1, 5, 30, 1, 100),
            (2, 5, 30, 0, 200),
            (4, 5, 30, 2, 0),
        ],
    ),
    "detour": (
        0, 3,
        [
            (0, 1, 5, 0, 0),
            (0, 2, 100, 10, 0),
            (1, 2, 5, 0, 0),
            (2, 3, 100, 0, 0),
            (1, 4, 100, 5, 0),
            (4, 3, 100, 5, 0),
        ],
    ),
}


class CrossReferenceTests(unittest.TestCase):
    def assert_matches_reference(self, net, ref_edges, src, dst, d):
        result = split_payment(net, src, dst, d)
        ref = ref_split(ref_edges, src, dst, d)

        if isinstance(result, InfeasibleResult):
            self.assertFalse(
                ref["feasible"],
                msg=f"core said infeasible ({result.reason}) but ref feasible for D={d}",
            )
            self.assertEqual(result.reason, ref["reason"])
            return

        self.assertIsInstance(result, SplitResult)
        self.assertTrue(ref["feasible"], msg=f"ref infeasible but core feasible D={d}")
        self.assertEqual(result.source_total_paid, ref["paid"], msg=f"D={d}")
        self.assertEqual(result.amount_delivered, d)

        # 路径集合与金额（按节点序列对齐参考）。
        got = {}
        for plan in result.plans:
            eids = tuple(plan.edge_ids)
            got[eids] = plan.deliver
        ref_alloc = {tuple(p): a for p, a in ref["paths"]}
        self.assertEqual(got, ref_alloc, msg=f"D={d}")
        self.assertEqual(
            result.edge_usage(), {int(k): v for k, v in ref["usage"].items()},
            msg=f"D={d}",
        )

    def test_all_cases_small_d(self):
        for name in CASES:
            src, dst, net, ref_edges = unpack(name)
            with self.subTest(case=name):
                for d in range(0, 21):
                    self.assert_matches_reference(net, ref_edges, src, dst, d)

    def test_full_range_100_on_key_cases(self):
        for name in ("single", "diamond", "shared_bottleneck", "fixed_fee_split",
                     "ppm_rounding", "parallel_routes_3", "detour"):
            src, dst, net, ref_edges = unpack(name)
            with self.subTest(case=name):
                for d in (25, 50, 75, 100):
                    self.assert_matches_reference(net, ref_edges, src, dst, d)

    def test_determinism(self):
        src, dst, net, _ = unpack("detour")
        r1 = split_payment(net, src, dst, 12)
        r2 = split_payment(net, src, dst, 12)
        self.assertEqual(
            [(p.edge_ids, p.deliver) for p in r1.plans],
            [(p.edge_ids, p.deliver) for p in r2.plans],
        )


if __name__ == "__main__":
    unittest.main()

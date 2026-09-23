import math
import unittest

from mp_split.fees import PPM_DENOM, edge_fee, min_sent_for_delivery


class FeeMathTests(unittest.TestCase):
    def test_zero_amount_no_fee(self):
        self.assertEqual(edge_fee(0, 5, 999), 0)
        self.assertEqual(min_sent_for_delivery(0, 5, 999), 0)

    def test_basic_formula(self):
        # fee(x) = b + ceil(x*r/1e6)
        self.assertEqual(edge_fee(10, 3, 0), 3)
        self.assertEqual(edge_fee(10, 0, 1_000_000), 10)
        # ceil 取整：x=1, r=1 -> ceil(1e-6) = 1
        self.assertEqual(edge_fee(1, 0, 1), 1)
        # 300_000 * 1 / 1e6 = 0.3 -> ceil = 1
        self.assertEqual(edge_fee(300_000, 0, 1), 1)
        # 刚好整除不多取
        self.assertEqual(edge_fee(1_000_000, 0, 1), 1)
        # 2_500_000 * 400_000 / 1e6 = 1_000_000，fee = 2 + 1_000_000
        self.assertEqual(edge_fee(2_500_000, 2, 400_000), 1_000_002)

    def test_closed_form_matches_bruteforce(self):
        # 独立验证手段：f(x)=x-fee(x) 单调不降，倍增 + 二分求最小 x
        # （不用被测模块的闭式公式本身）。
        def brute_min_x(b, r, d):
            def f(x):
                return x - edge_fee(x, b, r)

            lo, hi = d + b, max(1, 2 * (d + b))
            while f(hi) < d:
                lo, hi = hi, hi * 2 + 1
            while lo < hi:
                mid = (lo + hi) // 2
                if f(mid) >= d:
                    hi = mid
                else:
                    lo = mid + 1
            return lo

        cases = [
            (b, r, d)
            for b in range(0, 4)
            for r in (0, 1, 7, 1000, 333_333, 500_000, 999_999)
            for d in (1, 2, 3, 17, 100)
        ]
        for b, r, d in cases:
            with self.subTest(b=b, r=r, d=d):
                x_star = min_sent_for_delivery(d, b, r)
                x = brute_min_x(b, r, d)
                self.assertEqual(x_star, x)
                # 且 x-1 确实不够（边界，不用容差）
                self.assertLess((x - 1) - edge_fee(x - 1, b, r), d)

    def test_ppm_at_or_above_denom_undeliverable(self):
        # r = 1e6：任何正金额都送不到
        self.assertIsNone(min_sent_for_delivery(1, 0, PPM_DENOM))
        self.assertIsNone(min_sent_for_delivery(100, 0, 2_000_000))
        # 但 d=0 仍为 0
        self.assertEqual(min_sent_for_delivery(0, 0, PPM_DENOM), 0)

    def test_all_integer_no_float(self):
        # 大数值精确：r=999_999, d=100 闭式解与定义吻合
        x = min_sent_for_delivery(100, 0, 999_999)
        self.assertIsInstance(x, int)
        self.assertEqual(x, 100 * PPM_DENOM)  # ceil(100*1e6/1) = 1e8
        self.assertEqual(edge_fee(x, 0, 999_999), x - 100)


if __name__ == "__main__":
    unittest.main()

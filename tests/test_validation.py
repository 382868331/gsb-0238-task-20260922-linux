import math
import unittest

from mp_split import Edge, Network
from mp_split.errors import NetworkValidationError


def err_on(fn):
    try:
        fn()
    except NetworkValidationError:
        return True
    return False


class EdgeValidationTests(unittest.TestCase):
    def test_bool_rejected(self):
        # bool 虽是 int 子类，契约要求拒绝
        self.assertTrue(err_on(lambda: Edge(True, 1, 10)))
        self.assertTrue(err_on(lambda: Edge(0, 1, True)))
        self.assertTrue(err_on(lambda: Edge(0, 1, 10, base_fee=False)))
        self.assertTrue(err_on(lambda: Edge(0, 1, 10, ppm=True)))

    def test_nan_infinity_rejected(self):
        for bad in (math.nan, math.inf, -math.inf):
            self.assertTrue(err_on(lambda b=bad: Edge(b, 1, 10)))
            self.assertTrue(err_on(lambda b=bad: Edge(0, 1, b)))
            self.assertTrue(err_on(lambda b=bad: Edge(0, 1, 10, ppm=b)))

    def test_float_not_silently_rounded(self):
        self.assertTrue(err_on(lambda: Edge(0.0, 1, 10)))
        self.assertTrue(err_on(lambda: Edge(0, 1, 10.5)))

    def test_negative_and_self_loop(self):
        self.assertTrue(err_on(lambda: Edge(-1, 1, 10)))
        self.assertTrue(err_on(lambda: Edge(0, 1, -1)))
        self.assertTrue(err_on(lambda: Edge(0, 1, 10, base_fee=-1)))
        self.assertTrue(err_on(lambda: Edge(0, 1, 10, ppm=-1)))
        self.assertTrue(err_on(lambda: Edge(1, 1, 10)))

    def test_wrong_type_message_locatable(self):
        try:
            Edge(0, 1, "10")
        except NetworkValidationError as e:
            self.assertIn("balance", str(e))
        else:
            self.fail("expected NetworkValidationError")

    def test_valid_construction(self):
        e = Edge(0, 1, 100, 3, 500)
        self.assertEqual((e.src, e.dst, e.balance, e.base_fee, e.ppm), (0, 1, 100, 3, 500))


class NetworkValidationTests(unittest.TestCase):
    def test_basic(self):
        net = Network([Edge(0, 1, 10), Edge(1, 2, 10)], node_count=3)
        self.assertEqual(net.node_count, 3)
        self.assertEqual(net.labels, (0, 1, 2))

    def test_duplicate_directed_edge(self):
        self.assertTrue(
            err_on(lambda: Network([Edge(0, 1, 10), Edge(0, 1, 20)]))
        )

    def test_too_many_edges(self):
        es = [Edge(i, i + 1, 10) for i in range(17)]
        # 18 个节点本身超限；直接构造 16 条以上、8 节点：
        es = [
            Edge(u, v, 10)
            for u in range(8)
            for v in range(8)
            if u != v
        ][:17]
        self.assertTrue(err_on(lambda: Network(es)))

    def test_too_many_nodes(self):
        es = [Edge(i, i + 1, 10) for i in range(8)]  # 9 个不同节点
        self.assertTrue(err_on(lambda: Network(es)))

    def test_node_count_mismatch(self):
        self.assertTrue(
            err_on(lambda: Network([Edge(0, 1, 10), Edge(1, 5, 10)], node_count=3))
        )
        self.assertTrue(err_on(lambda: Network([Edge(0, 1, 10)], node_count=0)))
        self.assertTrue(err_on(lambda: Network([Edge(0, 1, 10)], node_count=9)))

    def test_node_count_bool_rejected(self):
        self.assertTrue(err_on(lambda: Network([Edge(0, 1, 10)], node_count=True)))

    def test_non_edge_element(self):
        self.assertTrue(err_on(lambda: Network([(0, 1, 10)])))

    def test_noncontiguous_labels_kept(self):
        net = Network([Edge(2, 5, 10), Edge(5, 9, 10)])
        self.assertEqual(net.labels, (2, 5, 9))
        self.assertEqual(net.to_internal(5, "x"), 1)
        self.assertEqual(net.to_label(0), 2)
        self.assertTrue(err_on(lambda: net.to_internal(7, "source")))


if __name__ == "__main__":
    unittest.main()

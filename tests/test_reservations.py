"""预留事务测试：争用、abort、到期边界、不可达、幂等与守恒核对。"""

import unittest

from mp_split import Edge, Ledger, Network
from mp_split.errors import (
    DuplicateRequestError,
    NetworkValidationError,
    ReservationStateError,
    UnknownReservationError,
)


def bottleneck_ledger(ttl=5):
    # 0->1 是唯一瓶颈（余额 30，免费），1->2 充裕
    net = Network(
        [Edge(0, 1, balance=30, base_fee=0, ppm=0),
         Edge(1, 2, balance=100, base_fee=0, ppm=0)],
        node_count=3,
    )
    return Ledger(net, ttl=ttl)


def assert_conservation(testcase, ledger, now):
    for row in ledger.audit(now=now):
        testcase.assertEqual(
            row.balance, row.settled + row.reserved + row.available,
            f"conservation violated on {row.src}->{row.dst}",
        )
        testcase.assertGreaterEqual(row.available, 0)


class ContentionTests(unittest.TestCase):
    def test_two_payments_contend_bottleneck_edge(self):
        ledger = bottleneck_ledger()
        a = ledger.prepare("A", 0, 2, 20, now=0)
        self.assertEqual(a.status, "held")
        # 瓶颈边 0->1 可用只剩 10，第二笔 D=20 必须失败
        b = ledger.prepare("B", 0, 2, 20, now=0)
        self.assertEqual(b.status, "failed")
        self.assertEqual(b.result.bottleneck.kind, "insufficient_balance")
        self.assertEqual(b.holds, ())
        # 失败的 prepare 不留任何部分边占用
        rows = { (r.src, r.dst): r for r in ledger.audit(now=0) }
        self.assertEqual(rows[(0, 1)].reserved, 20)
        self.assertEqual(rows[(0, 1)].available, 10)
        self.assertEqual(rows[(1, 2)].reserved, 20)
        # 提交 A 后 B 用新 ID 以更小金额成功
        ledger.commit("A", now=1)
        b2 = ledger.prepare("B2", 0, 2, 10, now=1)
        self.assertEqual(b2.status, "held")
        assert_conservation(self, ledger, 1)

    def test_shared_edge_charged_by_actual_sent(self):
        # ppm 边：实发额 > 交付额，预留必须按实发额占住
        net = Network(
            [Edge(0, 1, balance=50, base_fee=0, ppm=100_000),
             Edge(1, 2, balance=50, base_fee=0, ppm=0)],
            node_count=3,
        )
        ledger = Ledger(net, ttl=5)
        rec = ledger.prepare("P", 0, 2, 10, now=0)
        self.assertEqual(rec.status, "held")
        sent = rec.result.paths[0].hops[0].sent
        self.assertEqual(sent, 12)  # ceil(10*1e6/0.9e6)=12
        holds = { (h.src, h.dst): h.sent for h in rec.holds }
        self.assertEqual(holds[(0, 1)], 12)
        rows = { (r.src, r.dst): r for r in ledger.audit(now=0) }
        self.assertEqual(rows[(0, 1)].reserved, 12)
        assert_conservation(self, ledger, 0)


class AbortTests(unittest.TestCase):
    def test_prepare_then_abort_releases_capacity(self):
        ledger = bottleneck_ledger()
        ledger.prepare("A", 0, 2, 20, now=0)
        ledger.abort("A", now=1)
        # 容量全部回来，第二笔足额成功
        b = ledger.prepare("B", 0, 2, 30, now=1)
        self.assertEqual(b.status, "held")
        rows = { (r.src, r.dst): r for r in ledger.audit(now=1) }
        self.assertEqual(rows[(0, 1)].reserved, 30)
        self.assertEqual(rows[(0, 1)].settled, 0)
        # 重复 abort 幂等，不重复归还
        again = ledger.abort("A", now=2)
        self.assertEqual(again.status, "aborted")
        rows = { (r.src, r.dst): r for r in ledger.audit(now=2) }
        self.assertEqual(rows[(0, 1)].reserved, 30)
        # abort 之后不能再 commit
        with self.assertRaises(ReservationStateError):
            ledger.commit("A", now=2)
        assert_conservation(self, ledger, 2)


class ExpiryTests(unittest.TestCase):
    def test_expiry_releases_capacity(self):
        ledger = bottleneck_ledger(ttl=5)
        ledger.prepare("A", 0, 2, 20, now=10)  # expires_at = 15
        rows = { (r.src, r.dst): r for r in ledger.audit(now=14) }
        self.assertEqual(rows[(0, 1)].reserved, 20)
        rows = { (r.src, r.dst): r for r in ledger.audit(now=15) }
        self.assertEqual(rows[(0, 1)].reserved, 0)
        self.assertEqual(rows[(0, 1)].available, 30)
        self.assertEqual(ledger.get("A").status, "expired")

    def test_commit_at_expiry_moment_loses_to_clock(self):
        ledger = bottleneck_ledger(ttl=5)
        ledger.prepare("A", 0, 2, 20, now=10)  # expires_at = 15
        # 同刻先处理到期：commit 不能复活已过期预留
        with self.assertRaises(ReservationStateError):
            ledger.commit("A", now=15)
        rows = { (r.src, r.dst): r for r in ledger.audit(now=15) }
        self.assertEqual(rows[(0, 1)].settled, 0)
        self.assertEqual(rows[(0, 1)].reserved, 0)
        # 到期前一刻 commit 仍然有效
        ledger2 = bottleneck_ledger(ttl=5)
        ledger2.prepare("A", 0, 2, 20, now=10)
        rec = ledger2.commit("A", now=14)
        self.assertEqual(rec.status, "committed")
        rows = { (r.src, r.dst): r for r in ledger2.audit(now=15) }
        self.assertEqual(rows[(0, 1)].settled, 20)
        assert_conservation(self, ledger2, 15)

    def test_abort_at_expiry_moment_is_not_double_refund(self):
        ledger = bottleneck_ledger(ttl=5)
        ledger.prepare("A", 0, 2, 20, now=10)
        rec = ledger.abort("A", now=15)  # 已被时钟释放
        self.assertEqual(rec.status, "expired")
        rows = { (r.src, r.dst): r for r in ledger.audit(now=15) }
        self.assertEqual(rows[(0, 1)].available, 30)


class UnreachableTests(unittest.TestCase):
    def test_unreachable_prepare_fails_without_holds(self):
        net = Network([Edge(0, 1, balance=10, base_fee=0, ppm=0)], node_count=3)
        ledger = Ledger(net, ttl=5)
        rec = ledger.prepare("U", 0, 2, 5, now=0)
        self.assertEqual(rec.status, "failed")
        self.assertEqual(rec.result.bottleneck.kind, "unreachable")
        self.assertEqual(rec.holds, ())
        rows = { (r.src, r.dst): r for r in ledger.audit(now=0) }
        self.assertEqual(rows[(0, 1)].reserved, 0)
        # 同参数重试返回原失败状态
        again = ledger.prepare("U", 0, 2, 5, now=1)
        self.assertEqual(again.status, "failed")
        # 失败记录不能 commit / abort
        with self.assertRaises(ReservationStateError):
            ledger.commit("U", now=1)
        with self.assertRaises(ReservationStateError):
            ledger.abort("U", now=1)


class IdempotencyTests(unittest.TestCase):
    def test_same_id_same_params_returns_original_state(self):
        ledger = bottleneck_ledger()
        first = ledger.prepare("A", 0, 2, 20, now=0)
        second = ledger.prepare("A", 0, 2, 20, now=1)
        self.assertIs(first, second)
        rows = { (r.src, r.dst): r for r in ledger.audit(now=1) }
        self.assertEqual(rows[(0, 1)].reserved, 20)  # 没有重复占住

    def test_same_id_different_params_rejected(self):
        ledger = bottleneck_ledger()
        ledger.prepare("A", 0, 2, 20, now=0)
        with self.assertRaises(DuplicateRequestError):
            ledger.prepare("A", 0, 2, 21, now=0)
        with self.assertRaises(DuplicateRequestError):
            ledger.prepare("A", 0, 2, 20, max_paths=2, now=0)

    def test_commit_twice_does_not_double_settle(self):
        ledger = bottleneck_ledger()
        ledger.prepare("A", 0, 2, 20, now=0)
        ledger.commit("A", now=1)
        again = ledger.commit("A", now=2)
        self.assertEqual(again.status, "committed")
        rows = { (r.src, r.dst): r for r in ledger.audit(now=2) }
        self.assertEqual(rows[(0, 1)].settled, 20)
        self.assertEqual(rows[(0, 1)].reserved, 0)
        # committed 之后不能 abort
        with self.assertRaises(ReservationStateError):
            ledger.abort("A", now=2)
        assert_conservation(self, ledger, 2)

    def test_unknown_id_rejected(self):
        ledger = bottleneck_ledger()
        with self.assertRaises(UnknownReservationError):
            ledger.commit("nope", now=0)
        with self.assertRaises(UnknownReservationError):
            ledger.abort("nope", now=0)

    def test_validation(self):
        ledger = bottleneck_ledger()
        with self.assertRaises(NetworkValidationError):
            ledger.prepare("", 0, 2, 5, now=0)
        with self.assertRaises(NetworkValidationError):
            ledger.prepare("A", 0, 2, 5, now=-1)
        with self.assertRaises(NetworkValidationError):
            ledger.prepare("A", 0, 2, 5, now=True)
        with self.assertRaises(NetworkValidationError):
            Ledger(bottleneck_ledger().network, ttl=0)
        with self.assertRaises(NetworkValidationError):
            Ledger("not-a-network", ttl=1)


class ConservationTests(unittest.TestCase):
    def test_mixed_lifecycle_conservation(self):
        # 两跳网络，ppm 取整保留原费率；多笔事务混合生命周期
        net = Network(
            [Edge(0, 1, balance=60, base_fee=1, ppm=5_000),
             Edge(1, 2, balance=60, base_fee=2, ppm=0)],
            node_count=3,
        )
        ledger = Ledger(net, ttl=4)
        ledger.prepare("p1", 0, 2, 10, now=0)
        ledger.prepare("p2", 0, 2, 10, now=1)
        ledger.prepare("p3", 0, 2, 10, now=2)
        ledger.commit("p1", now=3)
        ledger.abort("p2", now=3)
        # p3 在 now=6 到期（expires_at=6）
        rows = { (r.src, r.dst): r for r in ledger.audit(now=6) }
        sent01 = ledger.get("p1").result.edge_usages
        e01 = [u for u in sent01 if (u.edge.src, u.edge.dst) == (0, 1)][0]
        self.assertEqual(rows[(0, 1)].settled, e01.sent_total)
        self.assertEqual(rows[(0, 1)].reserved, 0)
        self.assertEqual(ledger.get("p3").status, "expired")
        assert_conservation(self, ledger, 6)
        # 结算后容量被永久占用：新规划只看到剩余可用量
        rec = ledger.prepare("p4", 0, 2, 100, now=6)
        self.assertEqual(rec.status, "failed")
        assert_conservation(self, ledger, 6)


if __name__ == "__main__":
    unittest.main()

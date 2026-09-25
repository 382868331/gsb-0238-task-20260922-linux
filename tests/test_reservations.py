"""预留事务层测试：争用、abort、到期边界、不可达、幂等与守恒。"""

import unittest

from mp_split import (
    ABORTED,
    COMMITTED,
    EXPIRED,
    HELD,
    Edge,
    Network,
    ReservationLedger,
)
from mp_split.errors import (
    NetworkValidationError,
    ReservationConflictError,
    ReservationStateError,
    UnknownReservationError,
)


def net(edges, n=None):
    return Network([Edge(*e) for e in edges], node_count=n)


def bottleneck_ledger():
    # 0->1 与 1->2 是必经瓶颈：容量 20，b=1，r=0
    network = net([(0, 1, 20, 1, 0), (1, 2, 20, 1, 0)], 3)
    return ReservationLedger(network)


def state(ledger, src, dst):
    for st in ledger.edge_states():
        if st.edge.src == src and st.edge.dst == dst:
            return st
    raise AssertionError("edge not found")


class ContentionTests(unittest.TestCase):
    def test_two_payments_contend_bottleneck_edge(self):
        ledger = bottleneck_ledger()
        # D=10：第二跳送 11，第一跳送 12，占每边 12
        r1 = ledger.prepare("p1", 0, 2, 10, ttl=100, now=0)
        self.assertEqual(r1.status, HELD)
        self.assertEqual(state(ledger, 0, 1).reserved, 12)
        self.assertEqual(state(ledger, 1, 2).reserved, 11)
        self.assertEqual(state(ledger, 0, 1).available, 8)
        # 第二笔 D=10 需要 12 > 可用 8：不可行，且不留下任何占用
        r2 = ledger.prepare("p2", 0, 2, 10, ttl=100, now=0)
        self.assertEqual(r2.status, ABORTED)
        self.assertIsNone(r2.result)
        self.assertIn("insufficient_balance", r2.failure)
        self.assertEqual(state(ledger, 0, 1).reserved, 12)
        self.assertEqual(state(ledger, 1, 2).reserved, 11)
        ledger.audit()
        # 较小的一笔仍可成功：D=5 占 7/6
        r3 = ledger.prepare("p3", 0, 2, 5, ttl=100, now=0)
        self.assertEqual(r3.status, HELD)
        self.assertEqual(state(ledger, 0, 1).reserved, 19)
        ledger.audit()

    def test_prepare_then_abort_releases(self):
        ledger = bottleneck_ledger()
        ledger.prepare("p1", 0, 2, 10, ttl=100, now=0)
        r = ledger.abort("p1", now=1)
        self.assertEqual(r.status, ABORTED)
        self.assertEqual(state(ledger, 0, 1).reserved, 0)
        self.assertEqual(state(ledger, 0, 1).available, 20)
        self.assertEqual(state(ledger, 0, 1).settled, 0)
        ledger.audit()
        # 释放后同额支付再次可行
        r2 = ledger.prepare("p2", 0, 2, 10, ttl=100, now=1)
        self.assertEqual(r2.status, HELD)

    def test_commit_settles_once(self):
        ledger = bottleneck_ledger()
        ledger.prepare("p1", 0, 2, 10, ttl=100, now=0)
        r = ledger.commit("p1", now=1)
        self.assertEqual(r.status, COMMITTED)
        self.assertEqual(state(ledger, 0, 1).settled, 12)
        self.assertEqual(state(ledger, 1, 2).settled, 11)
        self.assertEqual(state(ledger, 0, 1).reserved, 0)
        # 重复 commit：幂等，不重复扣
        r2 = ledger.commit("p1", now=2)
        self.assertEqual(r2.status, COMMITTED)
        self.assertEqual(state(ledger, 0, 1).settled, 12)
        ledger.audit()
        # 已 commit 后 abort 是反向操作
        with self.assertRaises(ReservationStateError):
            ledger.abort("p1", now=3)


class ExpiryTests(unittest.TestCase):
    def test_expiry_before_commit_at_same_tick(self):
        ledger = bottleneck_ledger()
        ledger.prepare("p1", 0, 2, 10, ttl=5, now=0)  # expires_at = 5
        # now == expires_at：先处理到期，commit 被拒绝
        with self.assertRaises(ReservationStateError) as ctx:
            ledger.commit("p1", now=5)
        self.assertEqual(ctx.exception.status, EXPIRED)
        self.assertEqual(ledger.reservation("p1").status, EXPIRED)
        # 容量已释放
        self.assertEqual(state(ledger, 0, 1).reserved, 0)
        self.assertEqual(state(ledger, 0, 1).settled, 0)
        ledger.audit()

    def test_expired_cannot_be_revived(self):
        ledger = bottleneck_ledger()
        ledger.prepare("p1", 0, 2, 10, ttl=5, now=0)
        ledger.reservation("p1", now=6)  # 推进时钟触发到期
        self.assertEqual(ledger.reservation("p1").status, EXPIRED)
        with self.assertRaises(ReservationStateError):
            ledger.commit("p1", now=7)
        # 同参数幂等重放仍返回原（已过期）状态，不复活
        r = ledger.prepare("p1", 0, 2, 10, ttl=5, now=0)
        self.assertEqual(r.status, EXPIRED)
        ledger.audit()

    def test_commit_before_expiry_succeeds(self):
        ledger = bottleneck_ledger()
        ledger.prepare("p1", 0, 2, 10, ttl=5, now=0)
        r = ledger.commit("p1", now=4)
        self.assertEqual(r.status, COMMITTED)
        self.assertEqual(state(ledger, 0, 1).settled, 12)
        ledger.audit(now=10)

    def test_abort_after_expiry_is_idempotent_noop(self):
        ledger = bottleneck_ledger()
        ledger.prepare("p1", 0, 2, 10, ttl=5, now=0)
        r = ledger.abort("p1", now=9)  # 先到期，abort 为幂等 no-op
        self.assertEqual(r.status, EXPIRED)
        self.assertEqual(state(ledger, 0, 1).settled, 0)
        self.assertEqual(state(ledger, 0, 1).available, 20)
        ledger.audit()


class UnreachableTests(unittest.TestCase):
    def test_unreachable_leaves_no_holds(self):
        # 0->1 有边，2 不可达
        ledger = ReservationLedger(net([(0, 1, 50, 0, 0)], 3))
        r = ledger.prepare("p1", 0, 2, 10, ttl=10, now=0)
        self.assertEqual(r.status, ABORTED)
        self.assertIn("unreachable", r.failure)
        self.assertEqual(r.holds, ())
        self.assertEqual(state(ledger, 0, 1).reserved, 0)
        self.assertEqual(state(ledger, 0, 1).available, 50)
        ledger.audit()

    def test_partial_path_failure_is_atomic(self):
        # 0->1 充裕，1->2 不足：不得只在 0->1 上留下占用
        ledger = ReservationLedger(
            net([(0, 1, 100, 1, 0), (1, 2, 5, 1, 0)], 3)
        )
        r = ledger.prepare("p1", 0, 2, 10, ttl=10, now=0)
        self.assertEqual(r.status, ABORTED)
        self.assertEqual(state(ledger, 0, 1).reserved, 0)
        self.assertEqual(state(ledger, 1, 2).reserved, 0)
        self.assertEqual(state(ledger, 0, 1).available, 100)
        ledger.audit()


class IdempotencyTests(unittest.TestCase):
    def test_same_id_same_params_returns_original(self):
        ledger = bottleneck_ledger()
        r1 = ledger.prepare("p1", 0, 2, 10, ttl=100, now=0)
        r2 = ledger.prepare("p1", 0, 2, 10, ttl=100, now=0)
        self.assertIs(r1, r2)
        self.assertEqual(state(ledger, 0, 1).reserved, 12)  # 未重复占用
        ledger.audit()

    def test_same_id_different_params_rejected(self):
        ledger = bottleneck_ledger()
        ledger.prepare("p1", 0, 2, 10, ttl=100, now=0)
        with self.assertRaises(ReservationConflictError):
            ledger.prepare("p1", 0, 2, 11, ttl=100, now=0)
        with self.assertRaises(ReservationConflictError):
            ledger.prepare("p1", 0, 2, 10, ttl=99, now=0)
        # 冲突不影响原预留
        self.assertEqual(state(ledger, 0, 1).reserved, 12)
        ledger.audit()

    def test_terminal_repeats_do_not_double_refund(self):
        ledger = bottleneck_ledger()
        ledger.prepare("p1", 0, 2, 10, ttl=100, now=0)
        ledger.abort("p1", now=1)
        r = ledger.abort("p1", now=2)
        self.assertEqual(r.status, ABORTED)
        self.assertEqual(state(ledger, 0, 1).available, 20)
        self.assertEqual(state(ledger, 0, 1).reserved, 0)
        # abort 后 commit 是反向操作
        with self.assertRaises(ReservationStateError):
            ledger.commit("p1", now=3)
        ledger.audit()

    def test_unknown_id(self):
        ledger = bottleneck_ledger()
        with self.assertRaises(UnknownReservationError):
            ledger.commit("nope", now=0)
        with self.assertRaises(UnknownReservationError):
            ledger.abort("nope", now=0)
        with self.assertRaises(UnknownReservationError):
            ledger.reservation("nope")


class ConservationTests(unittest.TestCase):
    def test_mixed_sequence_conservation(self):
        # 共享边被两笔不同路径的预留同时占用，按每跳实际发送额计
        network = net(
            [
                (0, 1, 30, 1, 0),
                (1, 3, 30, 1, 0),
                (0, 2, 30, 0, 0),
                (2, 3, 30, 0, 0),
            ],
            4,
        )
        ledger = ReservationLedger(network)
        a = ledger.prepare("a", 0, 3, 10, ttl=10, now=0)
        b = ledger.prepare("b", 0, 3, 8, ttl=10, now=0)
        self.assertEqual(a.status, HELD)
        self.assertEqual(b.status, HELD)
        ledger.audit()
        # 两笔预留的逐边占用之和 == 各边 reserved
        totals = {}
        for r in (a, b):
            for h in r.holds:
                key = (h.edge.src, h.edge.dst)
                totals[key] = totals.get(key, 0) + h.sent
        for st in ledger.edge_states():
            self.assertEqual(
                st.reserved, totals.get((st.edge.src, st.edge.dst), 0)
            )
        ledger.commit("a", now=1)
        ledger.audit()
        ledger.abort("b", now=2)
        ledger.audit()
        # b 释放后：只剩 a 的 settled
        settled_sum = sum(st.settled for st in ledger.edge_states())
        self.assertEqual(settled_sum, sum(h.sent for h in a.holds))
        self.assertEqual(sum(st.reserved for st in ledger.edge_states()), 0)

    def test_fee_rounding_preserved_in_holds(self):
        # ppm 取整沿用原费率：r=5000, b=1, D=10 单边
        # x* = max(11, ceil(11*1e6/995000)) = max(11, 12) = 12
        ledger = ReservationLedger(net([(0, 1, 50, 1, 5000)], 2))
        r = ledger.prepare("p1", 0, 1, 10, ttl=10, now=0)
        self.assertEqual(r.status, HELD)
        (h,) = r.holds
        self.assertEqual(h.sent, 12)
        ledger.commit("p1", now=1)
        self.assertEqual(state(ledger, 0, 1).settled, 12)
        ledger.audit()


class ValidationTests(unittest.TestCase):
    def test_bad_params(self):
        ledger = bottleneck_ledger()
        with self.assertRaises(NetworkValidationError):
            ledger.prepare("", 0, 2, 10, ttl=10, now=0)
        with self.assertRaises(NetworkValidationError):
            ledger.prepare("p", 0, 2, 10, ttl=0, now=0)
        with self.assertRaises(NetworkValidationError):
            ledger.prepare("p", 0, 2, 10, ttl=10, now=-1)
        with self.assertRaises(NetworkValidationError):
            ledger.prepare("p", 0, 2, 10, ttl=10, now=True)
        with self.assertRaises(NetworkValidationError):
            ledger.prepare("p", 0, 2, 101, ttl=10, now=0)
        # 校验失败不留记录
        with self.assertRaises(UnknownReservationError):
            ledger.reservation("p")


if __name__ == "__main__":
    unittest.main()


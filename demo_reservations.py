"""预留事务演示：prepare/commit/abort、到期边界、争用与守恒核对。

真实计算，无 sleep、无预录输出。运行：
    ./.venv/bin/python demo_reservations.py
"""

import time

from mp_split import Edge, Network, ReservationLedger
from mp_split.errors import ReservationConflictError, ReservationStateError


def show_edges(ledger, title):
    print(f"  [{title}] 每边状态 (src->dst: settled/reserved/available)")
    for st in ledger.edge_states():
        e = st.edge
        print(
            f"    {e.src}->{e.dst}: balance={e.balance} "
            f"settled={st.settled} reserved={st.reserved} available={st.available}"
        )


def main() -> None:
    t0 = time.perf_counter()

    # 唯一路径 0->1->2：瓶颈边 0->1 容量 20（b=1），1->2 容量 20（b=2）
    network = Network(
        [
            Edge(0, 1, 20, 1, 0),
            Edge(1, 2, 20, 2, 0),
        ],
        node_count=3,
    )
    ledger = ReservationLedger(network)

    print("== 1. prepare 占住容量但不扣余额 ==")
    r1 = ledger.prepare("pay-1", 0, 2, 10, ttl=5, now=0)
    print(f"  pay-1 status={r1.status} expires_at={r1.expires_at}")
    for pb in r1.result.paths:
        hops = ", ".join(
            f"{h.edge.src}->{h.edge.dst} sent={h.sent} fee={h.fee}" for h in pb.hops
        )
        print(f"  路径 {pb.path}: delivered={pb.delivered} paid={pb.source_paid} | {hops}")
    show_edges(ledger, "prepare 后")

    print("== 2. 第二笔争用瓶颈边：可用量不足被拒绝，且不留部分占用 ==")
    r2 = ledger.prepare("pay-2", 0, 2, 10, ttl=5, now=0)
    print(f"  pay-2 status={r2.status} failure={r2.failure}")
    show_edges(ledger, "pay-2 失败后（占用未变）")

    print("== 3. 幂等：同 ID 同参数返回原状态，异参数拒绝 ==")
    again = ledger.prepare("pay-1", 0, 2, 10, ttl=5, now=0)
    print(f"  同参数重放: 同一对象={again is r1} status={again.status}")
    try:
        ledger.prepare("pay-1", 0, 2, 11, ttl=5, now=0)
    except ReservationConflictError as exc:
        print(f"  异参数拒绝: {exc}")

    print("== 4. 到期边界：now == expires_at 时先处理到期，commit 被拒绝 ==")
    try:
        ledger.commit("pay-1", now=5)
    except ReservationStateError as exc:
        print(f"  commit@t=5 拒绝: {exc}")
    print(f"  pay-1 状态={ledger.reservation('pay-1').status}（已过期不能复活）")
    show_edges(ledger, "到期释放后")

    print("== 5. abort 释放后容量恢复，commit 只结算一次 ==")
    r3 = ledger.prepare("pay-3", 0, 2, 10, ttl=50, now=6)
    print(f"  pay-3 status={r3.status}")
    ledger.abort("pay-3", now=7)
    print(f"  pay-3 abort 后 available 恢复: "
          f"{[(st.edge.src, st.edge.dst, st.available) for st in ledger.edge_states()]}")
    r4 = ledger.prepare("pay-4", 0, 2, 10, ttl=50, now=8)
    ledger.commit("pay-4", now=9)
    ledger.commit("pay-4", now=10)  # 幂等，不重复扣
    show_edges(ledger, "pay-4 commit（重复 commit 幂等）")

    print("== 6. 源到目标不可达 ==")
    r5 = ledger.prepare("pay-5", 2, 0, 5, ttl=10, now=10)
    print(f"  pay-5 status={r5.status} failure={r5.failure}")

    print("== 7. 独立账本守恒核对：balance == settled + reserved + available ==")
    ledger.audit(now=10)
    print("  audit 通过")

    dt = time.perf_counter() - t0
    print(f"完成，真实计算耗时 {dt * 1000:.1f} ms")


if __name__ == "__main__":
    main()

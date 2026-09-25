"""固定输入演示：离线通道的预留事务（真实计算，无预录结果）。

运行：./.venv/bin/python demo_reservations.py

场景（同一 4 节点网络，瓶颈边 0->1 余额 30）：
1. 两笔付款争一条瓶颈边：A 先 prepare 占住，B 超额失败且不留占用；
2. A 被 abort 后容量释放，B 用新 ID 成功；
3. 到期边界：ttl=5，now=15 与到期同刻的 commit 先看到期、被拒绝；
4. 源到目标不可达的 prepare 记录失败且可幂等重查；
5. 每步用独立账本核对 可用+预留+已结算 == 余额。
"""

from mp_split import Edge, Ledger, Network
from mp_split.errors import DuplicateRequestError, ReservationStateError


def build_network() -> Network:
    return Network(
        [
            Edge(0, 1, balance=30, base_fee=1, ppm=0),   # 瓶颈边
            Edge(1, 3, balance=100, base_fee=1, ppm=2_000),
            Edge(0, 2, balance=20, base_fee=0, ppm=0),
            Edge(2, 3, balance=20, base_fee=0, ppm=0),
        ],
        node_count=4,
    )


def show_ledger(ledger: Ledger, now: int) -> None:
    print(f"  账本核对 (now={now})：")
    for row in ledger.audit(now=now):
        ok = row.balance == row.settled + row.reserved + row.available
        print(
            f"    边 {row.src}->{row.dst}: 余额 {row.balance:>3} = "
            f"已结算 {row.settled:>3} + 预留 {row.reserved:>3} + "
            f"可用 {row.available:>3}  {'OK' if ok else 'VIOLATION'}"
        )


def main() -> None:
    ledger = Ledger(build_network(), ttl=5)

    print("=" * 72)
    print("场景 1：两笔付款争瓶颈边 0->1（余额 30）")
    print("=" * 72)
    a = ledger.prepare("pay-A", 0, 3, 40, now=0)
    print(f"pay-A prepare: {a.status}，源付 {a.result.source_total}，"
          f"占用 {[(h.src, h.dst, h.sent) for h in a.holds]}")
    b = ledger.prepare("pay-B", 0, 3, 40, now=0)
    print(f"pay-B prepare: {b.status}（{b.result.bottleneck.kind}），"
          f"holds={list(b.holds)}（失败不留部分占用）")
    show_ledger(ledger, 0)

    print()
    print("=" * 72)
    print("场景 2：abort 释放容量，B 换新 ID 成功并 commit")
    print("=" * 72)
    ledger.abort("pay-A", now=1)
    print(f"pay-A abort 后状态: {ledger.get('pay-A').status}")
    b2 = ledger.prepare("pay-B2", 0, 3, 40, now=1)
    print(f"pay-B2 prepare: {b2.status}，源付 {b2.result.source_total}")
    ledger.commit("pay-B2", now=2)
    again = ledger.commit("pay-B2", now=2)
    print(f"pay-B2 commit: committed（重复 commit 幂等 -> {again.status}，"
          "不重复扣减）")
    show_ledger(ledger, 2)

    print()
    print("=" * 72)
    print("场景 3：到期边界（全新账本，ttl=5，now=10 创建，now=15 同刻 commit）")
    print("=" * 72)
    ledger = Ledger(build_network(), ttl=5)
    c = ledger.prepare("pay-C", 0, 3, 10, now=10)
    print(f"pay-C prepare: {c.status}，expires_at={c.expires_at}")
    try:
        ledger.commit("pay-C", now=15)
    except ReservationStateError as exc:
        print(f"now=15 commit 被拒绝（先处理到期，已过期不能复活）：{exc}")
    print(f"pay-C 状态: {ledger.get('pay-C').status}")
    show_ledger(ledger, 15)

    print()
    print("=" * 72)
    print("场景 4：不可达 + 请求 ID 幂等")
    print("=" * 72)
    d = ledger.prepare("pay-D", 3, 0, 5, now=15)
    print(f"pay-D (3->0 不可达) prepare: {d.status}"
          f"（{d.result.bottleneck.kind}）")
    same = ledger.prepare("pay-D", 3, 0, 5, now=16)
    print(f"同 ID 同参数重试: 返回原状态 {same.status}（同一记录: {same is d}）")
    try:
        ledger.prepare("pay-D", 3, 0, 6, now=16)
    except DuplicateRequestError as exc:
        print(f"同 ID 异参数被拒绝：{exc}")
    show_ledger(ledger, 16)


if __name__ == "__main__":
    main()

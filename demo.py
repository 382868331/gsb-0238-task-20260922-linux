"""固定输入演示：多路径可行分拆 + 每边锁定增量（离线真实计算，无预录结果）。

运行：python demo.py

内容：
1. 正常案例：6 节点网络（边带 lock_delta），三条候选路径在末跳
   4->5 汇合（共享边），求 D=60 的最低源支付分拆，逐跳打印金额账单
   与到期，并核对每边余额；
2. 期限边界案例：并行两条路径（便宜长锁 vs 较贵短锁），展示
   max_lock 等号允许、严格超过被过滤后改选较贵路径，以及全部超限时
   的 deadline_exceeded 瓶颈；
3. 同案例 1 网络 D=100：打印“已证明无解”的不可行瓶颈（可交付上界）；
4. 边界拒绝：构造余额为 NaN 的边、以及非法 max_lock，展示可定位的
   输入校验异常。
"""

from mp_split import Edge, Network, solve
from mp_split.errors import NetworkValidationError


def build_network() -> Network:
    # 确定性固定数据（金额均为整数单位），括号内为锁定增量
    #   0->1 b=1 dl=4 ; 1->4 b=2 ppm=5000 dl=6   （较贵的路径，锁 10）
    #   0->2 b=1 dl=3 ; 2->4 免费           dl=2  （锁 5）
    #   0->3 b=1 dl=8 ; 3->4 免费           dl=9  （锁 17）
    #   4->5 b=1 ppm=1000 dl=14，余额 100   （三条路径共享的末跳）
    edges = [
        Edge(0, 1, balance=40, base_fee=1, ppm=0, lock_delta=4),
        Edge(1, 4, balance=40, base_fee=2, ppm=5_000, lock_delta=6),
        Edge(0, 2, balance=40, base_fee=1, ppm=0, lock_delta=3),
        Edge(2, 4, balance=40, base_fee=0, ppm=0, lock_delta=2),
        Edge(0, 3, balance=40, base_fee=1, ppm=0, lock_delta=8),
        Edge(3, 4, balance=40, base_fee=0, ppm=0, lock_delta=9),
        Edge(4, 5, balance=100, base_fee=1, ppm=1_000, lock_delta=14),
    ]
    return Network(edges, node_count=6)


def build_parallel_network() -> Network:
    # A: 0->1->3 免费，锁 25+25 = 50
    # B: 0->2->3 首跳固定费 5，锁 10+10 = 20；D=10 时源付 15
    edges = [
        Edge(0, 1, balance=100, base_fee=0, ppm=0, lock_delta=25),
        Edge(1, 3, balance=100, base_fee=0, ppm=0, lock_delta=25),
        Edge(0, 2, balance=100, base_fee=5, ppm=0, lock_delta=10),
        Edge(2, 3, balance=100, base_fee=0, ppm=0, lock_delta=10),
    ]
    return Network(edges, node_count=4)


def main() -> None:
    net = build_network()

    print("=" * 72)
    print("案例 1（正常）：0 -> 5，接收 D = 60，边带锁定增量")
    print("=" * 72)
    r = solve(net, source=0, destination=5, delivery=60, max_lock=40)
    assert r.feasible
    print(f"源总支付 = {r.source_total}（整数单位），使用 {r.used_paths} 条简单路径")
    for i, pb in enumerate(r.paths):
        print(f"\n路径 {i + 1}: {' -> '.join(map(str, pb.path))}"
              f"  交付 {pb.delivered}，源付 {pb.source_paid}，源端到期 {pb.source_lock}")
        print("  逐跳账单（转发顺序；到期从收款端反向累计）：")
        for h in pb.hops:
            print(
                f"    {h.edge.src}->{h.edge.dst}: "
                f"转发 {h.sent:>3} = 交付下游 {h.delivered:>3} + 费用 {h.fee:>2}"
                f"  本跳到期 {h.lock:>3}（增量 {h.edge.lock_delta:>2}）"
            )
        # 相邻跳到期差恰为前一跳边增量（手算核对关系）
        for hj, hn in zip(pb.hops, pb.hops[1:]):
            assert hj.lock - hn.lock == hj.edge.lock_delta

    print("\n每边余额核对（共享边按各路径实际发送额累计）：")
    used = {(u.edge.src, u.edge.dst): u.sent_total for u in r.edge_usages}
    balances = {
        (net.labels[e.src], net.labels[e.dst]): e.balance for e in net.edges
    }
    all_ok = True
    for (src, dst), balance in sorted(balances.items()):
        sent_total = used.get((src, dst), 0)
        ok = sent_total <= balance
        all_ok &= ok
        print(
            f"  边 {src}->{dst}: 累计实发 {sent_total:>3} / 余额 {balance:<4}"
            f" {'OK' if ok else 'VIOLATION'}"
        )
    print(f"交付总额核对：{sum(p.delivered for p in r.paths)} == 60")
    print(f"源支付合计核对：{sum(p.source_paid for p in r.paths)} == {r.source_total}")
    print("余额核对结果：", "全部通过" if all_ok else "存在违约")

    print()
    print("=" * 72)
    print("案例 2（期限边界）：并行路径，付款人最大期限 max_lock")
    print("=" * 72)
    pnet = build_parallel_network()

    r_eq = solve(pnet, 0, 3, 10, max_lock=50)
    assert r_eq.feasible and r_eq.paths[0].path == (0, 1, 3)
    print("max_lock = 50（恰等于便宜路径 A 的累计到期，等号允许）：")
    print(f"  选中 {r_eq.paths[0].path}，源付 {r_eq.source_total}，"
          f"源端到期 {r_eq.paths[0].source_lock}")

    r_strict = solve(pnet, 0, 3, 10, max_lock=20)
    assert r_strict.feasible and r_strict.paths[0].path == (0, 2, 3)
    print("max_lock = 20（A 的 50 严格超过被过滤，改选较贵短锁路径 B）：")
    print(f"  选中 {r_strict.paths[0].path}，源付 {r_strict.source_total}，"
          f"逐跳到期 {[h.lock for h in r_strict.paths[0].hops]}")

    r_dead = solve(pnet, 0, 3, 10, max_lock=19)
    assert not r_dead.feasible
    print("max_lock = 19（最短可达到期 20 仍超过，全部路径被过滤）：")
    print(f"  瓶颈类型：{r_dead.bottleneck.kind}")
    print(f"  说明：{r_dead.bottleneck.detail}")

    print()
    print("=" * 72)
    print("案例 3（已证明无解）：0 -> 5，接收 D = 100")
    print("=" * 72)
    r100 = solve(net, source=0, destination=5, delivery=100, max_lock=40)
    if not r100.feasible:
        print(f"瓶颈类型：{r100.bottleneck.kind}")
        print(f"说明：{r100.bottleneck.detail}")
        print(f"（完整穷举后的无解结论，不是预算耗尽；评估数 {r100.evaluations}）")
    else:
        print("意外可行，源付", r100.source_total)

    print()
    print("=" * 72)
    print("案例 4（输入拒绝边界）：NaN 余额 / 非法 max_lock")
    print("=" * 72)
    try:
        Network([Edge(0, 1, balance=float("nan"), base_fee=0, ppm=0)])
    except NetworkValidationError as exc:
        print("捕获 NetworkValidationError（NaN 余额）：")
        print(f"  {exc}")
    try:
        solve(pnet, 0, 3, 10, max_lock=True)
    except NetworkValidationError as exc:
        print("捕获 NetworkValidationError（max_lock=True，bool 被拒绝）：")
        print(f"  {exc}")
        print("非法输入被拒绝，未产生任何部分结果。")


if __name__ == "__main__":
    main()

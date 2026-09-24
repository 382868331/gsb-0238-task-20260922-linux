"""固定输入演示：多路径可行分拆（离线真实计算，无预录结果）。

运行：python demo.py

内容：
1. 正常案例：6 节点网络，三条候选路径在末跳 4->5 汇合（共享边），
   求 D=60 的最低源支付分拆，逐跳打印账单并核对每边余额；
2. 同网络 D=100：打印“已证明无解”的不可行瓶颈（可交付上界）；
3. 边界拒绝：构造余额为 NaN 的边，展示可定位的输入校验异常；
4. 锁定增量与到期（本次迭代）：
   4a. 正常结果——max_lock 过滤掉免费但长锁的路径，逐跳打印
       反向累计到期；
   4b. 期限等号边界——lock_total == max_lock 合法（放行），
       再小 1 则全部被过滤，返回 lock_limit_exceeded；
   4c. 非法参数——max_lock=-1 在任何计算前被拒绝。
"""

from mp_split import Edge, Network, solve
from mp_split.errors import NetworkValidationError


def build_network() -> Network:
    # 确定性固定数据（金额均为整数单位）
    #   0->1 b=1 ; 1->4 b=2 ppm=5000   （较贵的路径）
    #   0->2 b=1 ; 2->4 免费
    #   0->3 b=1 ; 3->4 免费
    #   4->5 b=1 ppm=1000，余额 100    （三条路径共享的末跳）
    edges = [
        Edge(0, 1, balance=40, base_fee=1, ppm=0),
        Edge(1, 4, balance=40, base_fee=2, ppm=5_000),
        Edge(0, 2, balance=40, base_fee=1, ppm=0),
        Edge(2, 4, balance=40, base_fee=0, ppm=0),
        Edge(0, 3, balance=40, base_fee=1, ppm=0),
        Edge(3, 4, balance=40, base_fee=0, ppm=0),
        Edge(4, 5, balance=100, base_fee=1, ppm=1_000),
    ]
    return Network(edges, node_count=6)


def build_lock_network() -> Network:
    # 两条 0->5 的三跳路径（节点 0..5）：
    #   A: 0->1->2->5，零费，各边锁定增量 2,3,4 -> 总锁定期 9
    #   B: 0->3->4->5，首跳固定费 4，增量 1,1,1 -> 总锁定期 3
    edges = [
        Edge(0, 1, balance=100, base_fee=0, ppm=0, lock_delta=2),
        Edge(1, 2, balance=100, base_fee=0, ppm=0, lock_delta=3),
        Edge(2, 5, balance=100, base_fee=0, ppm=0, lock_delta=4),
        Edge(0, 3, balance=100, base_fee=4, ppm=0, lock_delta=1),
        Edge(3, 4, balance=100, base_fee=0, ppm=0, lock_delta=1),
        Edge(4, 5, balance=100, base_fee=0, ppm=0, lock_delta=1),
    ]
    return Network(edges, node_count=6)


def print_path_bills(r) -> None:
    for i, pb in enumerate(r.paths):
        print(f"\n路径 {i + 1}: {' -> '.join(map(str, pb.path))}"
              f"  交付 {pb.delivered}，源付 {pb.source_paid}"
              f"，总锁定期 {pb.lock_total}")
        print("  逐跳账单（转发顺序；到期为该跳至末跳的反向累计）：")
        for h in pb.hops:
            print(
                f"    {h.edge.src}->{h.edge.dst}: "
                f"转发 {h.sent:>3} = 交付下游 {h.delivered:>3} + 费用 {h.fee:>2}"
                f"  到期 {h.expiry:>2}"
                f"  (b={h.edge.base_fee}, ppm={h.edge.ppm},"
                f" 增量={h.edge.lock_delta}, 余额={h.edge.balance})"
            )


def main() -> None:
    net = build_network()

    print("=" * 72)
    print("案例 1（正常）：0 -> 5，接收 D = 60")
    print("=" * 72)
    r = solve(net, source=0, destination=5, delivery=60)
    assert r.feasible
    print(f"源总支付 = {r.source_total}（整数单位），使用 {r.used_paths} 条简单路径")
    for i, pb in enumerate(r.paths):
        print(f"\n路径 {i + 1}: {' -> '.join(map(str, pb.path))}"
              f"  交付 {pb.delivered}，源付 {pb.source_paid}")
        print("  逐跳账单（转发顺序）：")
        for h in pb.hops:
            print(
                f"    {h.edge.src}->{h.edge.dst}: "
                f"转发 {h.sent:>3} = 交付下游 {h.delivered:>3} + 费用 {h.fee:>2}"
                f"  (b={h.edge.base_fee}, ppm={h.edge.ppm},"
                f" 余额={h.edge.balance})"
            )

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
    print("案例 2（已证明无解）：0 -> 5，接收 D = 100")
    print("=" * 72)
    r100 = solve(net, source=0, destination=5, delivery=100)
    if not r100.feasible:
        print(f"瓶颈类型：{r100.bottleneck.kind}")
        print(f"说明：{r100.bottleneck.detail}")
        print(f"（完整穷举后的无解结论，不是预算耗尽；评估数 {r100.evaluations}）")
    else:
        print("意外可行，源付", r100.source_total)

    print()
    print("=" * 72)
    print("案例 3（输入拒绝边界）：边余额为 NaN")
    print("=" * 72)
    try:
        Network([Edge(0, 1, balance=float("nan"), base_fee=0, ppm=0)])
    except NetworkValidationError as exc:
        print("捕获 NetworkValidationError：")
        print(f"  {exc}")
        print("非法输入被拒绝，未产生任何部分结果。")

    print()
    print("=" * 72)
    print("案例 4a（正常·期限过滤）：免费长锁路径 vs 收费短锁路径，D = 10")
    print("=" * 72)
    lnet = build_lock_network()
    r_free = solve(lnet, 0, 5, 10)
    assert r_free.feasible
    print("不设 max_lock：选免费路径 A，源付", r_free.source_total,
          "，总锁定期", r_free.paths[0].lock_total)
    print_path_bills(r_free)
    r_lim = solve(lnet, 0, 5, 10, max_lock=8)
    assert r_lim.feasible
    print("\nmax_lock=8：路径 A 总锁定期 9 > 8 被逐路径过滤，改走短锁路径 B：")
    print_path_bills(r_lim)
    assert r_lim.source_total == 14
    assert r_lim.paths[0].lock_total == 3
    assert [h.expiry for h in r_lim.paths[0].hops] == [3, 2, 1]
    print("\n手算核对：首跳固定费 4 -> 源付 14；到期反向累计 [1+1+1, 1+1, 1]"
          " = [3, 2, 1]；源付合计 14。")

    print()
    print("=" * 72)
    print("案例 4b（期限等号边界）：max_lock = 3 放行，= 2 全部过滤")
    print("=" * 72)
    r_eq = solve(lnet, 0, 5, 10, max_lock=3)
    assert r_eq.feasible
    print(f"max_lock=3（== B 的总锁定期，等号允许）：可行，走 "
          f"{' -> '.join(map(str, r_eq.paths[0].path))}")
    r_too_tight = solve(lnet, 0, 5, 10, max_lock=2)
    assert not r_too_tight.feasible
    print("max_lock=2：A(9)、B(3) 均超过期限——")
    print(f"  瓶颈类型：{r_too_tight.bottleneck.kind}")
    print(f"  说明：{r_too_tight.bottleneck.detail}")

    print()
    print("=" * 72)
    print("案例 4c（非法参数）：max_lock = -1")
    print("=" * 72)
    try:
        solve(lnet, 0, 5, 10, max_lock=-1)
    except NetworkValidationError as exc:
        print("捕获 NetworkValidationError：")
        print(f"  {exc}")
        print("非法输入在任何计算前被拒绝，求解无状态、无部分变更。")


if __name__ == "__main__":
    main()

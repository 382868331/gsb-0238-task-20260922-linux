"""循环付款规划演示：离线真实计算，无网络、无密钥、无 sleep、无预录结果。

运行：python demo_iteration.py

内容：
1. 正常案例：5 节点网络，从节点 0 出发规划一笔收回 60 的循环付款，
   首边减余额、末边增余额，逐跳打印实发/转发/费用并核对每边余额；
2. 已证明无解案例：首边手续费导致余额不足，打印可定位瓶颈；
3. 边界拒绝：收回金额超过接口上限，展示输入校验异常。
"""

from mp_split import Edge, Network, solve_cycle
from mp_split.errors import NetworkValidationError


def build_network() -> Network:
    # 确定性固定数据（金额均为整数单位）
    #   0->1 b=1            （候选首边，减余额）
    #   1->3 b=1 ppm=1000
    #   0->2 b=2            （候选首边，固定费更贵）
    #   2->3 免费
    #   3->0 b=1 ppm=2000   （末边，增余额）
    #   3->4 b=0 ; 4->0 b=0 （更长的免费回程）
    edges = [
        Edge(0, 1, balance=80, base_fee=1, ppm=0),
        Edge(1, 3, balance=80, base_fee=1, ppm=1_000),
        Edge(0, 2, balance=79, base_fee=2, ppm=0),
        Edge(2, 3, balance=80, base_fee=0, ppm=0),
        Edge(3, 0, balance=80, base_fee=1, ppm=2_000),
        Edge(3, 4, balance=80, base_fee=0, ppm=0),
        Edge(4, 0, balance=80, base_fee=0, ppm=0),
    ]
    return Network(edges, node_count=5)


def print_hops(result) -> None:
    print(f"环: {' -> '.join(map(str, result.cycle))}（{len(result.hops)} 跳）")
    print("逐跳账单（转发顺序）：")
    for h in result.hops:
        print(
            f"  {h.edge.src}->{h.edge.dst}: "
            f"实发 {h.sent:>3} = 转发 {h.delivered:>3} + 费用 {h.fee:>2}"
            f"  (b={h.edge.base_fee}, ppm={h.edge.ppm}, 余额={h.edge.balance})"
        )


def main() -> None:
    net = build_network()
    balances_before = [e.balance for e in net.edges]

    print("=" * 72)
    print("案例 1（正常）：从节点 0 出发，最终收回 D = 60")
    print("=" * 72)
    r = solve_cycle(net, node=0, amount=60)
    assert r.feasible
    print(f"源端实际支出 = {r.source_total}（首跳实发），最终收回 = {r.amount}")
    print_hops(r)
    print("\n每边余额核对（按各边实际发送额单独检查，不修改网络余额）：")
    sent_on = {}
    for h in r.hops:
        key = (h.edge.src, h.edge.dst)
        sent_on[key] = sent_on.get(key, 0) + h.sent
    all_ok = True
    for e in net.edges:
        key = (net.labels[e.src], net.labels[e.dst])
        used = sent_on.get(key, 0)
        ok = used <= e.balance
        all_ok &= ok
        print(f"  边 {key[0]}->{key[1]}: 实发 {used:>3} / 余额 {e.balance:<3}"
              f" {'OK' if ok else 'VIOLATION'}")
    balances_unchanged = all(e.balance == b for e, b in zip(net.edges, balances_before))
    print(f"余额核对结果：{'全部通过' if all_ok else '存在违约'}；"
          f"网络余额未被修改：{balances_unchanged}")
    print(f"守恒核对：首跳实发 {r.hops[0].sent} - 各跳费用 "
          f"{sum(h.fee for h in r.hops)} = 末跳收回 {r.hops[-1].delivered}")

    print()
    print("=" * 72)
    print("案例 2（已证明无解）：首边手续费导致余额不足，D = 78")
    print("=" * 72)
    r2 = solve_cycle(net, node=0, amount=78)
    if not r2.feasible:
        print(f"瓶颈类型：{r2.bottleneck.kind}")
        print(f"说明：{r2.bottleneck.detail}")
        print(f"（完整枚举全部简单环后的无解结论；评估数 {r2.evaluations}）")
    else:
        print("意外可行，源付", r2.source_total)

    print()
    print("=" * 72)
    print("案例 3（输入拒绝边界）：收回金额超过上限 100")
    print("=" * 72)
    try:
        solve_cycle(net, node=0, amount=101)
    except NetworkValidationError as exc:
        print("捕获 NetworkValidationError：")
        print(f"  {exc}")
        print("非法输入被拒绝，未产生任何部分结果。")


if __name__ == "__main__":
    main()

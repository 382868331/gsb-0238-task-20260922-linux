"""循环付款规划的真实离线演示（固定输入，无预录结果，无网络）。

运行：python demo_iteration.py

内容：
1. 余额不均的 5 节点网络：节点 0 的 0->1 通道余额过高、3->0 通道
   余额过低，规划一笔循环付款（首边减 0->1，末边增 3->0），
   逐跳打印实发/转发/费用并核对每边容量与“余额未被修改”；
2. 同网络收回 60：费用倒推后中间边容量不足，打印“已证明无解”瓶颈；
3. 边界拒绝：首边不从本节点出发，展示可定位的输入校验异常。
"""

from mp_split import Edge, Network, plan_cycle
from mp_split.errors import NetworkValidationError


def build_network() -> Network:
    # 确定性固定数据（金额均为整数单位）
    #   0->1 余额 90（要减）；3->0 余额 10（要增）
    #   中间可选 1->3 直达（贵）或 1->2->3（便宜但多一跳）
    edges = [
        Edge(0, 1, balance=90, base_fee=1, ppm=0),      # 要减余额的通道
        Edge(1, 3, balance=50, base_fee=4, ppm=0),      # 直达但固定费高
        Edge(1, 2, balance=50, base_fee=0, ppm=2_000),
        Edge(2, 3, balance=50, base_fee=1, ppm=0),
        Edge(3, 0, balance=80, base_fee=1, ppm=1_000),  # 要增余额的通道
    ]
    return Network(edges, node_count=5)


def print_plan(r) -> None:
    print(f"环：{' -> '.join(map(str, r.path))}")
    print(f"源端实际支出 = {r.source_paid}，最终收回 = {r.received}，"
          f"环上总费用 = {r.source_paid - r.received}")
    print("逐跳账单（转发顺序）：")
    for h in r.hops:
        print(
            f"  {h.edge.src}->{h.edge.dst}: "
            f"实发 {h.sent:>3} = 转发给下游 {h.delivered:>3} + 费用 {h.fee:>2}"
            f"  (b={h.edge.base_fee}, ppm={h.edge.ppm}, 余额={h.edge.balance})"
        )
    for h in r.hops:
        assert h.sent - h.fee == h.delivered
        assert h.sent <= h.edge.balance
    print("逐跳守恒 sent == delivered + fee 与容量检查：全部通过")


def main() -> None:
    net = build_network()
    snapshot = net.edges

    print("=" * 72)
    print("案例 1（正常）：节点 0 循环付款，首边 0->1（减余额），末边 3->0（增余额），收回 30")
    print("=" * 72)
    r = plan_cycle(net, 0, (0, 1), (3, 0), 30)
    assert r.feasible
    print_plan(r)
    assert net.edges == snapshot
    print("网络余额未被修改：确认")

    print()
    print("=" * 72)
    print("案例 2（已证明无解）：同网络收回 60，费用倒推后环上容量不足")
    print("=" * 72)
    r2 = plan_cycle(net, 0, (0, 1), (3, 0), 60)
    if not r2.feasible:
        print(f"瓶颈类型：{r2.bottleneck.kind}")
        print(f"说明：{r2.bottleneck.detail}")
    else:
        print("意外可行，源付", r2.source_paid)

    print()
    print("=" * 72)
    print("案例 3（输入拒绝边界）：首边不从本节点出发")
    print("=" * 72)
    try:
        plan_cycle(net, 0, (1, 2), (3, 0), 10)
    except NetworkValidationError as exc:
        print("捕获 NetworkValidationError：")
        print(f"  {exc}")
        print("非法输入被拒绝，未产生任何部分结果。")


if __name__ == "__main__":
    main()

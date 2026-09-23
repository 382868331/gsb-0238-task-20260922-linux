"""固定输入演示：真实计算并打印最优多路径分拆、逐跳账单与每边余额核对，
并实际触发一个输入拒绝边界（NaN 金额）。

运行：python demo.py（纯标准库，离线，无需任何参数）。
"""

from __future__ import annotations

import time

from paychan import (
    Edge,
    InfeasibleResult,
    InvalidInputError,
    Network,
    SplitResult,
    split_payment,
)


def build_demo_network() -> Network:
    # 确定性固定数据（非随机、非外部文件）：
    #   S 经 A、B 两条支路到 R，再共享尾边 R->T；
    #   两条支路的 R 前一跳余额都不足以独自承载 D，强制最优解拆成两条路径；
    #   各边余额、固定费 b、ppm 不同，求解器需权衡拆分金额与费用。
    return Network([
        Edge("S", "A", balance=60, base_fee=1, ppm=2000),
        Edge("S", "B", balance=60, base_fee=0, ppm=5000),
        Edge("A", "R", balance=28, base_fee=1, ppm=0),
        Edge("B", "R", balance=30, base_fee=0, ppm=1000),
        Edge("R", "T", balance=100, base_fee=1, ppm=3000),  # 两条路径共享
    ])


def print_feasible(result: SplitResult, network: Network) -> None:
    print("=" * 72)
    print(f"交付需求 D = {result.amount}：{result.source} -> {result.destination}")
    print(f"源总支付 = {result.source_total_paid}（到账 {result.amount_delivered}，"
          f"总费 {result.total_fee}），使用路径数 = {result.paths_used}")
    for i, plan in enumerate(result.plans, 1):
        print(f"\n路径 {i}: {' -> '.join(map(str, plan.nodes))}  "
              f"交付 {plan.deliver}，源付 {plan.source_paid}，费 {plan.total_fee()}")
        print(f"  {'边':<9}{'转发额':>7}{'费(b+ceil)':>12}{'上游送入':>9}{'占用余额':>9}")
        for hop in plan.hops:
            name = f"{hop.src}->{hop.dst}"
            print(f"  {name:<9}{hop.amount:>7}{hop.fee:>12}{hop.inflow:>9}"
                  f"{hop.balance_used:>9}")

    # 每边余额核对：共享边按各路径实际转发额累计。
    print("\n每边余额核对（共享边累计实际发送额）：")
    usage = result.edge_usage()
    print(f"  {'边':<9}{'累计占用':>9}{'余额':>7}{'结果':>6}")
    all_ok = True
    for eid, edge in enumerate(network.edges):
        used = usage.get(eid, 0)
        ok = used <= edge.balance
        all_ok &= ok
        name = f"{edge.src}->{edge.dst}"
        print(f"  {name:<9}{used:>9}{edge.balance:>7}{'OK' if ok else '超限!':>6}")
    assert all_ok, "结果违反边余额约束"
    print("  -> 全部边余额约束满足。")


def print_infeasible(result: InfeasibleResult) -> None:
    print("=" * 72)
    print(f"交付需求 D = {result.amount}：{result.source} -> {result.destination} 不可行")
    print(f"原因：{result.reason}")
    for bn in result.bottlenecks:
        print(f"  瓶颈割边 {bn.src}->{bn.dst}：本边余额 {bn.balance}，"
              f"割总容量 {bn.cut_capacity} < 需求 {bn.required}，缺口 {bn.margin}")


def main() -> None:
    network = build_demo_network()

    t0 = time.perf_counter()
    result = split_payment(network, "S", "T", 50)
    elapsed = time.perf_counter() - t0
    assert isinstance(result, SplitResult)
    print_feasible(result, network)
    print(f"\n（真实计算耗时 {elapsed*1000:.1f} ms，未使用 sleep 或预录结果）")

    # 不可行 + 瓶颈定位：尾边余额只有 40，两条支路再宽也送不过 40。
    tight = Network(list(network.edges[:-1]) +
                    [Edge("R", "T", balance=40, base_fee=1, ppm=3000)])
    bad = split_payment(tight, "S", "T", 60)
    assert isinstance(bad, InfeasibleResult)
    print_infeasible(bad)

    # 实际触发的拒绝边界：NaN 金额必须被拒绝（可定位错误，绝不静默纠正）。
    print("=" * 72)
    print("拒绝边界演示：split_payment(..., amount=float('nan'))")
    try:
        split_payment(network, "S", "T", float("nan"))
    except InvalidInputError as exc:
        print(f"  按预期拒绝：{type(exc).__name__} @ {exc.locator} —— {exc.message}")
    else:  # pragma: no cover
        raise AssertionError("NaN 金额未被拒绝")
    print("=" * 72)


if __name__ == "__main__":
    main()

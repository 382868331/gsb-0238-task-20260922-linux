"""费用数学（纯整数，无浮点）。

题面费用模型：在一条边上向下游转发金额 ``x``（实际发送额）时，

    fee(x) = b + ceil(x * r / 1_000_000)

下游实际收到 ``x - fee(x)``。余额按 **实际发送额** ``x`` 占用。

给定下游要收到 d，源端在该边上的最小实际发送额
``x* = min { x >= 0 : x - fee(x) >= d }`` 有闭式解。推导：

    x - b - ceil(x*r/M) >= d
    ceil(x*r/M) <= x - b - d            （右端为整数时成立）
    x*r/M <= x - b - d
    x*(M - r) >= (b + d) * M

因此当 ``0 <= r < M`` 时

    x* = max(b + d, ceil((b + d) * M / (M - r)))

当 ``r >= M`` 时，对任意 d >= 1 无解（比例费至少吞掉全部增量）。
``r = 0`` 时 x* = b + d。
"""

from __future__ import annotations

PPM_DENOM = 1_000_000


def edge_fee(x: int, base_fee: int, ppm: int) -> int:
    """转发 ``x`` 时收取的费用：``b + ceil(x*r/1e6)``。

    约定 ``x = 0`` 时费用为 0（未使用的边不收费）；x > 0 时固定费
    照常收取。
    """
    if x <= 0:
        return 0
    return base_fee + (x * ppm + PPM_DENOM - 1) // PPM_DENOM


def min_sent_for_delivery(delivered: int, base_fee: int, ppm: int) -> int | None:
    """下游收到 ``delivered`` 时，本边所需的最小实际发送额。

    返回 ``None`` 表示不存在有限解（``ppm >= 1_000_000`` 且
    ``delivered >= 1``）。``delivered == 0`` 返回 0。
    """
    if delivered <= 0:
        return 0
    if ppm >= PPM_DENOM:
        return None
    need = base_fee + delivered
    # ceil(need * M / (M - r))
    scaled = (
        need * PPM_DENOM + (PPM_DENOM - ppm) - 1
    ) // (PPM_DENOM - ppm)
    return max(need, scaled)

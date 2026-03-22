import numpy as np
from typing import List, Tuple
from models import (
    SwingPoint, StructureBreak, LiquidityPool, FairValueGap,
    OrderBlock, Direction, StructureType,
)
from config import Config


def compute_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                period: int = 14) -> np.ndarray:
    """ATR via simple moving average of True Range."""
    n = len(highs)
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    atr = np.empty(n)
    atr[:period] = np.nan
    atr[period] = np.mean(tr[:period])
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


# ---------------------------------------------------------------------------
# Swing Points
# ---------------------------------------------------------------------------

def detect_swing_points(
    highs: np.ndarray, lows: np.ndarray, times: np.ndarray, lookback: int,
) -> Tuple[List[SwingPoint], List[SwingPoint]]:
    """Pivot-based swing detection. Requires `lookback` bars on each side."""
    swing_highs: List[SwingPoint] = []
    swing_lows: List[SwingPoint] = []
    n = len(highs)

    for i in range(lookback, n - lookback):
        # Swing high: high[i] >= all highs in window
        is_sh = True
        for j in range(i - lookback, i + lookback + 1):
            if j != i and highs[j] > highs[i]:
                is_sh = False
                break
        if is_sh:
            swing_highs.append(
                SwingPoint(bar_index=i, price=highs[i], is_high=True,
                           time=int(times[i]))
            )

        # Swing low: low[i] <= all lows in window
        is_sl = True
        for j in range(i - lookback, i + lookback + 1):
            if j != i and lows[j] < lows[i]:
                is_sl = False
                break
        if is_sl:
            swing_lows.append(
                SwingPoint(bar_index=i, price=lows[i], is_high=False,
                           time=int(times[i]))
            )

    return swing_highs, swing_lows


# ---------------------------------------------------------------------------
# Market Structure: BOS / CHoCH
# ---------------------------------------------------------------------------

def detect_structure_breaks(
    closes: np.ndarray, times: np.ndarray,
    swing_highs: List[SwingPoint], swing_lows: List[SwingPoint],
) -> Tuple[List[StructureBreak], str]:
    """Walk forward, detect BOS / CHoCH when close breaks a swing level."""
    breaks: List[StructureBreak] = []
    trend_dir = "none"

    # Build sorted merged events: (bar_index, 'sh'|'sl', SwingPoint)
    events = []
    for sp in swing_highs:
        events.append((sp.bar_index, "sh", sp))
    for sp in swing_lows:
        events.append((sp.bar_index, "sl", sp))
    events.sort(key=lambda e: e[0])

    last_sh: SwingPoint | None = None
    last_sl: SwingPoint | None = None

    ev_idx = 0
    n = len(closes)

    for bar in range(n):
        # Register any swing points confirmed at this bar
        while ev_idx < len(events) and events[ev_idx][0] <= bar:
            _, kind, sp = events[ev_idx]
            if kind == "sh":
                last_sh = sp
            else:
                last_sl = sp
            ev_idx += 1

        # Check break of swing high
        if last_sh is not None and closes[bar] > last_sh.price:
            if trend_dir == "bearish" or trend_dir == "none":
                btype = StructureType.CHOCH
            else:
                btype = StructureType.BOS
            breaks.append(StructureBreak(
                bar_index=bar, price=last_sh.price,
                direction=Direction.BULLISH, break_type=btype,
                time=int(times[bar]),
            ))
            trend_dir = "bullish"
            last_sh = None  # consumed

        # Check break of swing low
        if last_sl is not None and closes[bar] < last_sl.price:
            if trend_dir == "bullish" or trend_dir == "none":
                btype = StructureType.CHOCH
            else:
                btype = StructureType.BOS
            breaks.append(StructureBreak(
                bar_index=bar, price=last_sl.price,
                direction=Direction.BEARISH, break_type=btype,
                time=int(times[bar]),
            ))
            trend_dir = "bearish"
            last_sl = None

    return breaks, trend_dir


# ---------------------------------------------------------------------------
# Liquidity Pools
# ---------------------------------------------------------------------------

def detect_liquidity_pools(
    swing_highs: List[SwingPoint], swing_lows: List[SwingPoint],
    atr: np.ndarray, config: Config,
) -> List[LiquidityPool]:
    """Cluster swing points within ATR * mult into liquidity pools."""
    pools: List[LiquidityPool] = []

    def _cluster(points: List[SwingPoint], is_high_side: bool):
        for sp in points:
            atr_at = atr[sp.bar_index] if not np.isnan(atr[sp.bar_index]) else 0.0
            tolerance = atr_at * config.liq_atr_mult
            merged = False
            for pool in pools:
                if pool.is_high_side == is_high_side and not pool.is_swept:
                    if abs(sp.price - pool.price) <= tolerance:
                        pool.price = (
                            (pool.price * pool.touch_count + sp.price)
                            / (pool.touch_count + 1)
                        )
                        pool.touch_count += 1
                        pool.last_bar = sp.bar_index
                        pool.last_time = sp.time
                        merged = True
                        break
            if not merged:
                pools.append(LiquidityPool(
                    price=sp.price, first_bar=sp.bar_index,
                    last_bar=sp.bar_index, touch_count=1,
                    is_high_side=is_high_side,
                    first_time=sp.time, last_time=sp.time,
                ))

    _cluster(swing_highs, is_high_side=True)
    _cluster(swing_lows, is_high_side=False)

    return [p for p in pools if p.touch_count >= config.liq_min_touches]


# ---------------------------------------------------------------------------
# Liquidity Sweeps
# ---------------------------------------------------------------------------

def detect_sweeps(
    pools: List[LiquidityPool],
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
    times: np.ndarray,
) -> List[LiquidityPool]:
    """Mark pools as swept when wick exceeds but close returns."""
    n = len(highs)
    for pool in pools:
        if pool.is_swept:
            continue
        start = pool.last_bar + 1
        for bar in range(start, n):
            if pool.is_high_side:
                if highs[bar] > pool.price and closes[bar] < pool.price:
                    pool.is_swept = True
                    pool.sweep_bar = bar
                    break
            else:
                if lows[bar] < pool.price and closes[bar] > pool.price:
                    pool.is_swept = True
                    pool.sweep_bar = bar
                    break
    return pools


# ---------------------------------------------------------------------------
# Fair Value Gaps
# ---------------------------------------------------------------------------

def detect_fvgs(
    highs: np.ndarray, lows: np.ndarray, opens: np.ndarray,
    closes: np.ndarray, times: np.ndarray, atr: np.ndarray,
    config: Config,
) -> List[FairValueGap]:
    """3-candle imbalance detection with ATR size filter."""
    fvgs: List[FairValueGap] = []
    n = len(highs)

    for i in range(2, n):
        atr_val = atr[i] if not np.isnan(atr[i]) else 0.0

        # Bullish FVG: gap up — current low > high two bars ago
        if lows[i] > highs[i - 2] and closes[i - 1] > opens[i - 1]:
            size = lows[i] - highs[i - 2]
            if size >= atr_val * config.fvg_min_atr:
                fvgs.append(FairValueGap(
                    bar_index=i - 1, top=lows[i], bottom=highs[i - 2],
                    is_bullish=True, time=int(times[i - 1]),
                ))

        # Bearish FVG: gap down — current high < low two bars ago
        if highs[i] < lows[i - 2] and closes[i - 1] < opens[i - 1]:
            size = lows[i - 2] - highs[i]
            if size >= atr_val * config.fvg_min_atr:
                fvgs.append(FairValueGap(
                    bar_index=i - 1, top=lows[i - 2], bottom=highs[i],
                    is_bullish=False, time=int(times[i - 1]),
                ))

    # Check fills: walk forward from each FVG
    for fvg in fvgs:
        for bar in range(fvg.bar_index + 1, n):
            if fvg.is_bullish and lows[bar] <= fvg.top:
                fvg.is_active = False
                fvg.fill_bar = bar
                break
            if not fvg.is_bullish and highs[bar] >= fvg.bottom:
                fvg.is_active = False
                fvg.fill_bar = bar
                break

    return fvgs


# ---------------------------------------------------------------------------
# Order Blocks
# ---------------------------------------------------------------------------

def detect_order_blocks(
    highs: np.ndarray, lows: np.ndarray, opens: np.ndarray,
    closes: np.ndarray, times: np.ndarray,
    structure_breaks: List[StructureBreak], config: Config,
) -> List[OrderBlock]:
    """Create OBs at the last opposing candle before each structure break."""
    obs: List[OrderBlock] = []

    for sb in structure_breaks:
        bar = sb.bar_index
        search_end = max(0, bar - config.ob_max_lookback)
        caused_choch = sb.break_type == StructureType.CHOCH

        if sb.direction == Direction.BULLISH:
            # Find last bearish candle before the bullish break
            for j in range(bar - 1, search_end - 1, -1):
                if closes[j] < opens[j]:
                    obs.append(OrderBlock(
                        start_bar=j,
                        top=max(opens[j], closes[j]),
                        bottom=min(opens[j], closes[j]),
                        is_bullish=True, bos_bar=bar,
                        caused_choch=caused_choch,
                        time=int(times[j]),
                    ))
                    break
        else:
            # Find last bullish candle before the bearish break
            for j in range(bar - 1, search_end - 1, -1):
                if closes[j] > opens[j]:
                    obs.append(OrderBlock(
                        start_bar=j,
                        top=max(opens[j], closes[j]),
                        bottom=min(opens[j], closes[j]),
                        is_bullish=False, bos_bar=bar,
                        caused_choch=caused_choch,
                        time=int(times[j]),
                    ))
                    break

    # Check invalidation
    n = len(closes)
    for ob in obs:
        for bar in range(ob.start_bar + 1, n):
            if ob.is_bullish and closes[bar] < ob.bottom:
                ob.is_active = False
                ob.invalidation_bar = bar
                break
            if not ob.is_bullish and closes[bar] > ob.top:
                ob.is_active = False
                ob.invalidation_bar = bar
                break

    return obs


# ---------------------------------------------------------------------------
# Master detection
# ---------------------------------------------------------------------------

def run_smc_detection(rates: np.ndarray, config: Config) -> dict:
    """Run all SMC detections on the rates array from MT5.

    Args:
        rates: structured numpy array with fields
               time, open, high, low, close, tick_volume, spread, real_volume
        config: Config instance

    Returns:
        dict with keys: swing_highs, swing_lows, structure_breaks,
        trend_direction, liquidity_pools, fvgs, order_blocks, atr
    """
    times = rates["time"].astype(np.int64)
    opens = rates["open"].astype(np.float64)
    highs = rates["high"].astype(np.float64)
    lows = rates["low"].astype(np.float64)
    closes = rates["close"].astype(np.float64)

    atr = compute_atr(highs, lows, closes, config.atr_period)

    swing_highs, swing_lows = detect_swing_points(
        highs, lows, times, config.swing_lookback,
    )

    structure_breaks, trend_direction = detect_structure_breaks(
        closes, times, swing_highs, swing_lows,
    )

    liquidity_pools = detect_liquidity_pools(
        swing_highs, swing_lows, atr, config,
    )

    liquidity_pools = detect_sweeps(
        liquidity_pools, highs, lows, closes, times,
    )

    fvgs = detect_fvgs(
        highs, lows, opens, closes, times, atr, config,
    )

    order_blocks = detect_order_blocks(
        highs, lows, opens, closes, times, structure_breaks, config,
    )

    return {
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
        "structure_breaks": structure_breaks,
        "trend_direction": trend_direction,
        "liquidity_pools": liquidity_pools,
        "fvgs": fvgs,
        "order_blocks": order_blocks,
        "atr": atr,
    }

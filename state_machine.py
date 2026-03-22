import numpy as np
from dataclasses import dataclass, field
from typing import List, Callable
from models import (
    POI, POIType, Direction, Signal,
    LiquidityPool, FairValueGap, OrderBlock, StructureBreak, StructureType,
)
from config import Config


@dataclass
class StateMachineState:
    """Persistent state across loop iterations."""
    bull_sweep_pending: bool = False
    bull_sweep_bar: int = 0
    bear_sweep_pending: bool = False
    bear_sweep_bar: int = 0

    active_pois: List[POI] = field(default_factory=list)
    all_signals: List[Signal] = field(default_factory=list)
    last_processed_bar: int = -1


class EntryStateMachine:
    def __init__(self, config: Config):
        self.config = config
        self.state = StateMachineState()

    def process(
        self, smc_results: dict, rates: np.ndarray,
        probability_fn: Callable,
    ) -> List[Signal]:
        """Run the state machine for the current bar.

        Flow:
            1. Detect new sweeps → set pending flags
            2. If BOS/CHoCH after pending sweep → tag OBs/FVGs as post-sweep POIs
            3. Check retrace into active POIs
            4. Score retracing POIs with probability_fn (two-barrier P(win))
            5. Emit signals if P(win) > threshold

        Returns:
            List of new signals generated this tick.
        """
        closes = rates["close"].astype(np.float64)
        highs = rates["high"].astype(np.float64)
        lows = rates["low"].astype(np.float64)
        n = len(closes)
        current_bar = n - 1

        if current_bar <= self.state.last_processed_bar:
            return []
        self.state.last_processed_bar = current_bar

        pools: List[LiquidityPool] = smc_results["liquidity_pools"]
        breaks: List[StructureBreak] = smc_results["structure_breaks"]
        fvgs: List[FairValueGap] = smc_results["fvgs"]
        obs: List[OrderBlock] = smc_results["order_blocks"]
        trend_dir: str = smc_results["trend_direction"]
        atr: np.ndarray = smc_results["atr"]
        atr_current = float(atr[-1]) if not np.isnan(atr[-1]) else 0.0

        new_signals: List[Signal] = []

        # --- Step 1: detect new sweeps ---
        for pool in pools:
            if not pool.is_swept or pool.sweep_bar is None:
                continue
            if pool.sweep_bar != current_bar:
                continue
            # New sweep on this bar
            if pool.is_high_side:
                # High-side swept = bearish sweep (price took buy-side liquidity)
                self.state.bear_sweep_pending = True
                self.state.bear_sweep_bar = current_bar
            else:
                # Low-side swept = bullish sweep (price took sell-side liquidity)
                self.state.bull_sweep_pending = True
                self.state.bull_sweep_bar = current_bar

        # --- Step 2: expire stale sweeps ---
        if (self.state.bull_sweep_pending
                and current_bar - self.state.bull_sweep_bar > self.config.sweep_expiry_bars):
            self.state.bull_sweep_pending = False

        if (self.state.bear_sweep_pending
                and current_bar - self.state.bear_sweep_bar > self.config.sweep_expiry_bars):
            self.state.bear_sweep_pending = False

        # --- Step 3: BOS/CHoCH after pending sweep → tag POIs ---
        for sb in breaks:
            if sb.bar_index != current_bar:
                continue

            if sb.direction == Direction.BULLISH and self.state.bull_sweep_pending:
                self.state.bull_sweep_pending = False
                self._tag_post_sweep_pois(obs, fvgs, current_bar,
                                          is_bullish=True)

            if sb.direction == Direction.BEARISH and self.state.bear_sweep_pending:
                self.state.bear_sweep_pending = False
                self._tag_post_sweep_pois(obs, fvgs, current_bar,
                                          is_bullish=False)

        # --- Step 4: invalidation check → retrace check → probability ---
        current_close = closes[-1]
        current_high = highs[-1]
        current_low = lows[-1]

        surviving_pois: List[POI] = []

        for poi in self.state.active_pois:
            if poi.retrace_triggered:
                continue

            # 4a. Invalidation check FIRST — zone is dead, skip everything
            invalidated = False
            if poi.direction == Direction.BULLISH:
                # Bullish zone broken: close below bottom
                if current_close < poi.bottom:
                    invalidated = True
            else:
                # Bearish zone broken: close above top
                if current_close > poi.top:
                    invalidated = True

            if invalidated:
                continue  # POI is dead, don't keep it, don't score it

            # 4b. Minimum POI width filter — skip micro-zones where SL < spread
            poi_width = abs(poi.top - poi.bottom)
            min_width = atr_current * self.config.min_poi_atr_width
            if poi_width < min_width:
                continue  # zone too narrow, SL would be smaller than noise

            # 4c. Retrace check — did price enter the (still-valid) zone?
            entered = False
            if poi.direction == Direction.BULLISH:
                if current_low <= poi.top and current_close >= poi.bottom:
                    entered = True
            else:
                if current_high >= poi.bottom and current_close <= poi.top:
                    entered = True

            if not entered:
                surviving_pois.append(poi)  # zone still valid, keep waiting
                continue

            # 4d. Score this POI: calculate P(win) via two-barrier FPT
            # Pass current_close as actual entry price (not POI edge)
            poi = probability_fn(
                poi, closes, trend_dir, atr_current, self.config,
                current_close,
            )
            poi.retrace_triggered = True

            if poi.posterior >= self.config.signal_probability_threshold:
                sig = Signal(
                    direction=poi.direction,
                    poi=poi,
                    probability=poi.posterior,
                    bar_index=current_bar,
                    price=current_close,
                    time=int(rates["time"][-1]),
                )
                new_signals.append(sig)
                self.state.all_signals.append(sig)
            # POI was triggered (scored), don't keep it regardless of result

        self.state.active_pois = surviving_pois

        return new_signals

    def _tag_post_sweep_pois(
        self, obs: List[OrderBlock], fvgs: List[FairValueGap],
        current_bar: int, is_bullish: bool,
    ):
        """Tag active OBs and FVGs as post-sweep and add them to active_pois.

        After a sweep -> BOS/CHoCH sequence, ALL active zones with the correct
        direction become post-sweep POI candidates. No timing filter — an OB
        created 50 bars ago is still valid if it hasn't been invalidated.
        """
        # Existing POI keys to avoid duplicates
        existing = {(p.poi_type, p.bar_index, p.direction) for p in self.state.active_pois}

        direction = Direction.BULLISH if is_bullish else Direction.BEARISH

        # Tag OBs
        for ob in obs:
            if ob.is_active and ob.is_bullish == is_bullish:
                key = (POIType.ORDER_BLOCK, ob.start_bar, direction)
                if key not in existing:
                    ob.post_sweep = True
                    self.state.active_pois.append(POI(
                        poi_type=POIType.ORDER_BLOCK,
                        direction=direction,
                        top=ob.top, bottom=ob.bottom,
                        bar_index=ob.start_bar, post_sweep=True,
                        time=ob.time, caused_choch=ob.caused_choch,
                    ))

        # Tag FVGs
        for fvg in fvgs:
            if fvg.is_active and fvg.is_bullish == is_bullish:
                key = (POIType.FAIR_VALUE_GAP, fvg.bar_index, direction)
                if key not in existing:
                    fvg.post_sweep = True
                    self.state.active_pois.append(POI(
                        poi_type=POIType.FAIR_VALUE_GAP,
                        direction=direction,
                        top=fvg.top, bottom=fvg.bottom,
                        bar_index=fvg.bar_index, post_sweep=True,
                        time=fvg.time,
                    ))

import os
import json
import time
from datetime import datetime
from typing import List, Optional

import MetaTrader5 as mt5
import numpy as np

from models import POI, Signal, Direction, POIType
from config import Config


class MT5Interface:
    def __init__(self, config: Config):
        self.config = config
        self.data_path = ""
        self.signal_file = ""

    def connect(self) -> bool:
        """Initialize MT5 connection and set up signal file path."""
        if not mt5.initialize():
            print(f"MT5 initialization failed: {mt5.last_error()}")
            return False

        symbol_info = mt5.symbol_info(self.config.symbol)
        if symbol_info is None:
            print(f"Symbol {self.config.symbol} not found")
            mt5.shutdown()
            return False
        if not symbol_info.visible:
            mt5.symbol_select(self.config.symbol, True)

        terminal_info = mt5.terminal_info()
        self.data_path = terminal_info.data_path
        self.signal_file = os.path.join(
            self.data_path, "MQL5", "Files", "ssp_ea_signals.json",
        )
        self.config.signal_file_path = self.signal_file

        print(f"Connected to MT5 | {self.config.symbol} {self.config.timeframe}")
        print(f"Signal file: {self.signal_file}")
        return True

    def get_rates(self) -> Optional[np.ndarray]:
        """Fetch OHLC bars from MT5."""
        tf = Config.timeframe_to_mt5(self.config.timeframe)
        rates = mt5.copy_rates_from_pos(
            self.config.symbol, tf, 0, self.config.num_bars,
        )
        if rates is None or len(rates) == 0:
            print(f"Failed to get rates: {mt5.last_error()}")
            return None
        return rates

    def get_current_bar_time(self) -> int:
        """Return timestamp of the most recent bar for new-bar detection."""
        tf = Config.timeframe_to_mt5(self.config.timeframe)
        rates = mt5.copy_rates_from_pos(self.config.symbol, tf, 0, 1)
        if rates is not None and len(rates) > 0:
            return int(rates[-1]["time"])
        return 0

    # ------------------------------------------------------------------
    # Signal file output (for MQL5 overlay)
    # ------------------------------------------------------------------

    def write_signals(
        self, smc_results: dict, signals: List[Signal],
        active_pois: List[POI], trend_direction: str,
    ):
        """Write JSON signal file for the MQL5 overlay indicator.

        Uses atomic write (temp file + os.replace) to prevent
        the MQL5 side from reading a half-written file.

        Format includes full SMC state for both overlay visualization and EA execution.
        """
        data = {
            "timestamp": int(time.time()),
            "symbol": self.config.symbol,
            "timeframe": self.config.timeframe,
            "trend": trend_direction,
            "signals": [self._signal_to_dict(s) for s in signals],
            "active_pois": [self._poi_to_dict(p) for p in active_pois],
            "fvgs": self._serialize_fvgs(smc_results["fvgs"]),
            "order_blocks": self._serialize_obs(smc_results["order_blocks"]),
            "liquidity_pools": self._serialize_pools(smc_results["liquidity_pools"]),
            "structure_breaks": self._serialize_breaks(smc_results["structure_breaks"]),
        }

        tmp_file = self.signal_file + ".tmp"
        os.makedirs(os.path.dirname(self.signal_file), exist_ok=True)
        with open(tmp_file, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_file, self.signal_file)

    def _signal_to_dict(self, s: Signal) -> dict:
        # Calculate SL/TP from ACTUAL entry price to POI edge
        # This must match probability.py entry price fix:
        #   - SL at POI invalidation edge (bottom for bullish, top for bearish)
        #   - Distance = entry_price to edge, NOT full POI height
        if s.direction == Direction.BULLISH:
            sl_price = s.poi.bottom  # SL at zone bottom
            sl_distance = s.price - sl_price  # Entry to SL edge
            tp_distance = sl_distance * self.config.tp_rr_ratio
            tp_price = s.price + tp_distance
        else:  # BEARISH
            sl_price = s.poi.top  # SL at zone top
            sl_distance = sl_price - s.price  # Entry to SL edge
            tp_distance = sl_distance * self.config.tp_rr_ratio
            tp_price = s.price - tp_distance

        # Guard against invalid distances
        if sl_distance <= 0:
            sl_distance = 0.01
            tp_distance = sl_distance * self.config.tp_rr_ratio

        return {
            "time": s.time,
            "direction": s.direction.value,
            "price": round(s.price, 5),
            "sl_price": round(sl_price, 5),
            "tp_price": round(tp_price, 5),
            "probability": round(s.probability, 4),
            "poi_type": s.poi.poi_type.value,
            "is_oos": 1,  # For live trading, all signals are executed (is_oos=1)
            # Extra fields for indicator display and analysis (not parsed by EA, but useful for overlay)
            "poi_top": round(s.poi.top, 5),
            "poi_bottom": round(s.poi.bottom, 5),
            "sl_distance": round(abs(sl_distance), 5),
            "tp_distance": round(tp_distance, 5),
            "tp_rr_ratio": self.config.tp_rr_ratio,
        }

    def _poi_to_dict(self, p: POI) -> dict:
        return {
            "type": p.poi_type.value,
            "direction": p.direction.value,
            "top": p.top,
            "bottom": p.bottom,
            "time": p.time,
            "post_sweep": p.post_sweep,
            "hold_prob": round(p.hold_probability, 4),
            "prior": round(p.prior, 4),
            "fpt_break": round(p.fpt_probability, 4),
            "posterior": round(p.posterior, 4),
        }

    def _serialize_fvgs(self, fvgs) -> list:
        return [
            {"top": f.top, "bottom": f.bottom, "bullish": f.is_bullish,
             "active": f.is_active, "post_sweep": f.post_sweep,
             "time": f.time, "bar": f.bar_index}
            for f in fvgs if f.is_active
        ]

    def _serialize_obs(self, obs) -> list:
        return [
            {"top": o.top, "bottom": o.bottom, "bullish": o.is_bullish,
             "active": o.is_active, "post_sweep": o.post_sweep,
             "time": o.time, "bar": o.start_bar}
            for o in obs if o.is_active
        ]

    def _serialize_pools(self, pools) -> list:
        return [
            {"price": p.price, "high_side": p.is_high_side,
             "touches": p.touch_count, "swept": p.is_swept,
             "first_time": p.first_time, "last_time": p.last_time}
            for p in pools
        ]

    def _serialize_breaks(self, breaks) -> list:
        return [
            {"price": b.price, "direction": b.direction.value,
             "type": b.break_type.value, "time": b.time, "bar": b.bar_index}
            for b in breaks[-20:]  # last 20 only
        ]

    # ------------------------------------------------------------------
    # Console dashboard
    # ------------------------------------------------------------------

    def print_dashboard(
        self, smc_results: dict, signals: List[Signal],
        active_pois: List[POI], mu: float, sigma: float,
    ):
        """Print formatted console dashboard."""
        os.system("cls" if os.name == "nt" else "clear")

        trend = smc_results["trend_direction"]
        breaks = smc_results["structure_breaks"]
        pools = smc_results["liquidity_pools"]
        fvgs = smc_results["fvgs"]
        obs = smc_results["order_blocks"]
        atr = smc_results["atr"]
        atr_val = float(atr[-1]) if not np.isnan(atr[-1]) else 0.0

        last_break_type = breaks[-1].break_type.value if breaks else "-"
        active_fvgs = sum(1 for f in fvgs if f.is_active)
        active_obs = sum(1 for o in obs if o.is_active)
        swept_pools = sum(1 for p in pools if p.is_swept)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        print("=" * 60)
        print(f"  INDICATOR 5 | SMC + Stochastic Probability")
        print(f"  {self.config.symbol} {self.config.timeframe} | {now}")
        print("=" * 60)
        print(f"  Trend         : {trend.upper()}")
        print(f"  Last Break    : {last_break_type}")
        print(f"  ATR           : {atr_val:.5f}")
        print(f"  Drift (mu)    : {mu:.8f}")
        print(f"  Volatility    : {sigma:.8f}")
        print("-" * 60)
        print(f"  Active FVGs   : {active_fvgs}")
        print(f"  Active OBs    : {active_obs}")
        print(f"  Liq. Pools    : {len(pools)} ({swept_pools} swept)")
        print(f"  Pending POIs  : {len(active_pois)}")
        print("-" * 60)

        if active_pois:
            print("  SCORED POIs:")
            for p in active_pois:
                arrow = "^" if p.direction == Direction.BULLISH else "v"
                ps = "POST-SWEEP" if p.post_sweep else ""
                print(
                    f"    {arrow} {p.poi_type.value:3s} "
                    f"[{p.bottom:.5f} - {p.top:.5f}] "
                    f"P(win)={p.posterior:.2f} {ps}"
                )

        if signals:
            print("-" * 60)
            print("  >>> SIGNALS <<<")
            for s in signals:
                arrow = "BUY" if s.direction == Direction.BULLISH else "SELL"
                print(
                    f"    {arrow} @ {s.price:.5f} | "
                    f"P(win)={s.probability:.2f} | "
                    f"{s.poi.poi_type.value} zone"
                )

        print("=" * 60)
        print(f"  Threshold: {self.config.signal_probability_threshold:.0%}")
        print(f"  Press Ctrl+C to stop")
        print("=" * 60)

    def disconnect(self):
        """Clean shutdown."""
        mt5.shutdown()

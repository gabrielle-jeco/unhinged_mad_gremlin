"""Quick diagnostic: verify probability spread and buy/sell detection."""
import sys
import MetaTrader5 as mt5
import numpy as np
from config import Config
from smc import run_smc_detection
from probability import score_poi, estimate_drift, estimate_volatility
from models import POI, POIType, Direction


def main():
    symbol = "EURUSDm"
    timeframe = "H1"

    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        sys.exit(1)
    mt5.symbol_select(symbol, True)

    tf = Config.timeframe_to_mt5(timeframe)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, 500)
    mt5.shutdown()

    if rates is None:
        print("No data")
        sys.exit(1)

    config = Config(symbol=symbol, timeframe=timeframe)

    # --- 1. Verify config values ---
    print("=" * 50)
    print("CONFIG CHECK:")
    print(f"  TP/RR Ratio          = {config.tp_rr_ratio:.1f}")
    print(f"  Signal Threshold     = {config.signal_probability_threshold:.0%}")
    print(f"  Drift Window         = {config.drift_window} bars")
    print(f"  Volatility Window    = {config.vol_window} bars")
    print(f"  Min POI Width (ATR)  = {config.min_poi_atr_width:.1f}x")
    print(f"  Sweep Expiry         = {config.sweep_expiry_bar} bars")
    print()

    # --- 2. Verify probability math ---
    closes = rates["close"].astype(float)
    mu = estimate_drift(closes, config.drift_window)
    sigma = estimate_volatility(closes, config.vol_window)
    current = closes[-1]

    print("MARKET STATE:")
    print(f"  Current price = {current:.5f}")
    print(f"  Drift (mu)    = {mu:.10f}")
    print(f"  Volatility    = {sigma:.10f}")
    print()

    # Get SMC data to obtain actual trend direction
    smc = run_smc_detection(rates, config)
    trend_direction = smc["trend_direction"]
    print(f"  Trend         = {trend_direction.upper()}")
    print()

    # Simulate different POI scenarios
    print("PROBABILITY SCENARIOS:")
    test_cases = [
        ("Bearish OB post-sweep CHoCH", Direction.BEARISH, True, True,
         current + 0.0010, current + 0.0030),  # barrier = top
        ("Bearish OB post-sweep BOS", Direction.BEARISH, True, False,
         current + 0.0010, current + 0.0030),
        ("Bearish OB no-sweep", Direction.BEARISH, False, False,
         current + 0.0010, current + 0.0030),
        ("Bullish OB post-sweep CHoCH", Direction.BULLISH, True, True,
         current - 0.0030, current - 0.0010),  # barrier = bottom
        ("Bullish OB post-sweep BOS", Direction.BULLISH, True, False,
         current - 0.0030, current - 0.0010),
    ]

    for name, direction, post_sweep, choch, bottom, top in test_cases:
        poi = POI(
            poi_type=POIType.ORDER_BLOCK, direction=direction,
            top=top, bottom=bottom, bar_index=0,
            post_sweep=post_sweep, caused_choch=choch,
        )
        # Use score_poi (full pipeline including drift attenuation)
        # entry_price = current (actual close where retrace happens)
        # trend_direction = actual market trend (for counter-trend attenuation)
        poi = score_poi(poi, closes, trend_direction, sigma * 100, config, current)

        barrier = bottom if direction == Direction.BULLISH else top
        print(f"  {name}")
        print(f"    barrier={barrier:.5f} d={abs(barrier-current):.5f}")
        print(f"    P(win)={poi.posterior:.4f} | FPT={poi.fpt_probability:.4f}")
        passes = "SIGNAL" if poi.posterior >= config.signal_probability_threshold else "FILTERED"
        print(f"    >> {passes}")
        print()

    # --- 3. SMC detection stats ---
    print("=" * 50)
    print("SMC DETECTION STATS:")
    print(f"  Swing highs      : {len(smc['swing_highs'])}")
    print(f"  Swing lows       : {len(smc['swing_lows'])}")
    print(f"  Structure breaks : {len(smc['structure_breaks'])}")
    print(f"  Trend direction  : {smc['trend_direction']}")

    pools = smc["liquidity_pools"]
    high_pools = [p for p in pools if p.is_high_side]
    low_pools = [p for p in pools if not p.is_high_side]
    high_swept = [p for p in high_pools if p.is_swept]
    low_swept = [p for p in low_pools if not p.is_high_side and p.is_swept]

    print(f"  Liq pools (high) : {len(high_pools)} ({len(high_swept)} swept)")
    print(f"  Liq pools (low)  : {len(low_pools)} ({len(low_swept)} swept)")
    print(f"  FVGs active      : {sum(1 for f in smc['fvgs'] if f.is_active)}")
    print(f"  OBs active       : {sum(1 for o in smc['order_blocks'] if o.is_active)}")

    # BOS/CHoCH breakdown
    bullish_breaks = [b for b in smc["structure_breaks"]
                      if b.direction == Direction.BULLISH]
    bearish_breaks = [b for b in smc["structure_breaks"]
                      if b.direction == Direction.BEARISH]
    print(f"  Bullish breaks   : {len(bullish_breaks)}")
    print(f"  Bearish breaks   : {len(bearish_breaks)}")

    # OB breakdown
    bull_obs = [o for o in smc["order_blocks"] if o.is_bullish]
    bear_obs = [o for o in smc["order_blocks"] if not o.is_bullish]
    print(f"  Bullish OBs      : {len(bull_obs)} "
          f"({sum(1 for o in bull_obs if o.is_active)} active)")
    print(f"  Bearish OBs      : {len(bear_obs)} "
          f"({sum(1 for o in bear_obs if o.is_active)} active)")
    print("=" * 50)


if __name__ == "__main__":
    main()

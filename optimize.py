"""
Parameter sweep optimizer for indicator5.

Strategy: pre-compute SMC detection once on full data (O(n)), then re-run
the lightweight state machine + scoring for each parameter combo using
per-bar snapshots (O(n * structures) per combo instead of O(n^2)).

Usage:
    python optimize.py
    python optimize.py --symbol EURUSDm --timeframe H1 --bars 5000
"""

import argparse
import sys
import time
from copy import copy
from datetime import datetime, UTC
from itertools import product

import MetaTrader5 as mt5
import numpy as np

from config import Config
from smc import run_smc_detection
from probability import score_poi
from state_machine import EntryStateMachine
from models import Direction


# ── Parameter grid ──────────────────────────────────────────────
# Edit these to control what gets swept.
PARAM_GRID = {
    "signal_probability_threshold": [0.30, 0.50, 0.60, 0.70],
    "fpt_horizon_bars":            [10, 30, 50],
    "sweep_expiry_bars":           [10, 20, 30],
    "tp_rr_ratio":                 [1.5, 2.0, 3.0],
    "min_poi_atr_width":           [0.3, 0.5, 1.0],
}
# ────────────────────────────────────────────────────────────────


def load_data(symbol: str, timeframe: str, num_bars: int):
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        sys.exit(1)
    info = mt5.symbol_info(symbol)
    if info is None:
        print(f"Symbol {symbol} not found")
        mt5.shutdown()
        sys.exit(1)
    if not info.visible:
        mt5.symbol_select(symbol, True)

    tf = Config.timeframe_to_mt5(timeframe)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, num_bars)
    mt5.shutdown()

    if rates is None or len(rates) == 0:
        print("Failed to load data")
        sys.exit(1)

    start = datetime.fromtimestamp(rates[0]["time"], UTC).strftime("%Y-%m-%d %H:%M")
    end = datetime.fromtimestamp(rates[-1]["time"], UTC).strftime("%Y-%m-%d %H:%M")
    print(f"Loaded {len(rates)} bars of {symbol} {timeframe}")
    print(f"Period: {start} >> {end}\n")
    return rates


def precompute_smc(rates, config: Config):
    """Run SMC detection once on full data. Returns full_smc + trend_per_bar."""
    n = len(rates)
    print("Pre-computing SMC on full dataset...", end="", flush=True)
    t0 = time.time()

    full_smc = run_smc_detection(rates, config)

    # Build per-bar trend direction from structure breaks
    trend_per_bar = ["none"] * n
    sorted_breaks = sorted(full_smc["structure_breaks"], key=lambda x: x.bar_index)
    current_trend = "none"
    break_idx = 0
    for bar in range(n):
        while break_idx < len(sorted_breaks) and sorted_breaks[break_idx].bar_index <= bar:
            sb = sorted_breaks[break_idx]
            current_trend = "bullish" if sb.direction == Direction.BULLISH else "bearish"
            break_idx += 1
        trend_per_bar[bar] = current_trend

    elapsed = time.time() - t0
    n_obs = len(full_smc["order_blocks"])
    n_fvgs = len(full_smc["fvgs"])
    print(f" done ({elapsed:.1f}s, {n_obs} OBs, {n_fvgs} FVGs)")
    return full_smc, trend_per_bar


def _build_smc_snapshot(full_smc, bar, trend_per_bar):
    """Build SMC results for a specific bar from pre-computed data."""
    obs_snapshot = []
    for ob in full_smc["order_blocks"]:
        if ob.start_bar > bar:
            continue
        active_now = (ob.invalidation_bar is None or ob.invalidation_bar > bar)
        if active_now != ob.is_active:
            ob_copy = copy(ob)
            ob_copy.is_active = active_now
            obs_snapshot.append(ob_copy)
        else:
            obs_snapshot.append(ob)

    fvg_snapshot = []
    for fvg in full_smc["fvgs"]:
        if fvg.bar_index > bar:
            continue
        active_now = (fvg.fill_bar is None or fvg.fill_bar > bar)
        if active_now != fvg.is_active:
            fvg_copy = copy(fvg)
            fvg_copy.is_active = active_now
            fvg_snapshot.append(fvg_copy)
        else:
            fvg_snapshot.append(fvg)

    return {
        "liquidity_pools": full_smc["liquidity_pools"],
        "structure_breaks": full_smc["structure_breaks"],
        "order_blocks": obs_snapshot,
        "fvgs": fvg_snapshot,
        "atr": full_smc["atr"][:bar + 1],
        "trend_direction": trend_per_bar[bar],
    }


def run_single(rates, full_smc, trend_per_bar, config: Config,
               warmup: int, eval_bars: int):
    """Run state machine + scoring using pre-computed SMC snapshots."""
    sm = EntryStateMachine(config)
    signals = []
    n = len(rates)

    for bar in range(warmup, n):
        smc_snapshot = _build_smc_snapshot(full_smc, bar, trend_per_bar)
        new_signals = sm.process(smc_snapshot, rates[:bar + 1], score_poi)

        for sig in new_signals:
            signals.append({
                "bar_index": bar,
                "direction": sig.direction,
                "entry_price": sig.price,
                "probability": sig.probability,
                "poi_top": sig.poi.top,
                "poi_bottom": sig.poi.bottom,
            })

    # ── Evaluate ──
    wins = losses = neutral = 0
    total_mfe = total_mae = 0.0
    rr = config.tp_rr_ratio

    for sig in signals:
        bar = sig["bar_index"]
        if bar + eval_bars >= n:
            continue

        future = rates[bar + 1: bar + 1 + eval_bars]
        entry = sig["entry_price"]
        is_buy = sig["direction"] == Direction.BULLISH

        fh = future["high"].astype(float)
        fl = future["low"].astype(float)
        fc = future["close"].astype(float)

        if is_buy:
            mfe = float(np.max(fh) - entry)
            mae = float(entry - np.min(fl))
        else:
            mfe = float(entry - np.min(fl))
            mae = float(np.max(fh) - entry)

        poi_h = abs(sig["poi_top"] - sig["poi_bottom"])
        sl = poi_h if poi_h > 0 else mae
        tp = sl * rr

        hit_tp = mfe >= tp
        hit_sl = mae >= sl

        outcome = "neutral"
        if hit_tp and hit_sl:
            for i in range(len(future)):
                if is_buy:
                    if fl[i] <= entry - sl:
                        outcome = "loss"; break
                    if fh[i] >= entry + tp:
                        outcome = "win"; break
                else:
                    if fh[i] >= entry + sl:
                        outcome = "loss"; break
                    if fl[i] <= entry - tp:
                        outcome = "win"; break
        elif hit_tp:
            outcome = "win"
        elif hit_sl:
            outcome = "loss"

        if outcome == "win":
            wins += 1
        elif outcome == "loss":
            losses += 1
        else:
            neutral += 1

        total_mfe += mfe
        total_mae += mae

    total = wins + losses + neutral
    wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
    avg_mfe = total_mfe / total if total > 0 else 0
    avg_mae = total_mae / total if total > 0 else 0
    avg_prob = np.mean([s["probability"] for s in signals]) * 100 if signals else 0

    # Expectancy: (WR * RR - (1-WR) * 1R) per trade
    wr_dec = wr / 100
    expectancy = wr_dec * rr - (1 - wr_dec) * 1 if total > 0 else 0

    return {
        "signals": len(signals),
        "evaluated": total,
        "wins": wins,
        "losses": losses,
        "neutral": neutral,
        "win_rate": wr,
        "avg_prob": avg_prob,
        "avg_mfe": avg_mfe,
        "avg_mae": avg_mae,
        "mfe_mae": avg_mfe / avg_mae if avg_mae > 0 else 0,
        "expectancy_r": expectancy,
    }


def main():
    parser = argparse.ArgumentParser(description="Indicator5 Parameter Optimizer")
    parser.add_argument("--symbol", default="XAUUSDm")
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--bars", type=int, default=5000)
    parser.add_argument("--eval-bars", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=100)
    args = parser.parse_args()

    rates = load_data(args.symbol, args.timeframe, args.bars)

    # Base config (SMC params stay fixed during sweep)
    base_config = Config(symbol=args.symbol, timeframe=args.timeframe)

    # Pre-compute SMC once
    full_smc, trend_per_bar = precompute_smc(rates, base_config)

    # Generate all combinations
    keys = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    combos = list(product(*values))
    print(f"\nSweeping {len(combos)} parameter combinations...\n")

    results = []
    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))

        config = Config(
            symbol=args.symbol,
            timeframe=args.timeframe,
            signal_probability_threshold=params.get(
                "signal_probability_threshold",
                base_config.signal_probability_threshold,
            ),
            fpt_horizon_bars=params.get(
                "fpt_horizon_bars", base_config.fpt_horizon_bars
            ),
            sweep_expiry_bars=params.get(
                "sweep_expiry_bars", base_config.sweep_expiry_bars
            ),
            tp_rr_ratio=params.get("tp_rr_ratio", base_config.tp_rr_ratio),
            min_poi_atr_width=params.get(
                "min_poi_atr_width", base_config.min_poi_atr_width
            ),
        )

        metrics = run_single(rates, full_smc, trend_per_bar, config,
                             args.warmup, args.eval_bars)
        results.append({**params, **metrics})

        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(combos)} done...")

    # ── Sort by expectancy (descending), then win rate ──
    results.sort(key=lambda r: (r["expectancy_r"], r["win_rate"]), reverse=True)

    # ── Print top results ──
    print("\n" + "=" * 105)
    print("  PARAMETER SWEEP RESULTS (sorted by expectancy)")
    print("=" * 105)
    print(
        f"  {'Thresh':>6} {'FPT_H':>5} {'Sweep':>5} {'RR':>5} {'MinW':>5}"
        f" | {'Sigs':>4} {'W':>3} {'L':>3} {'N':>3}"
        f" | {'WR%':>6} {'Exp(R)':>7} {'MFE/MAE':>7} {'AvgP':>6}"
    )
    print("-" * 110)

    for r in results[:30]:
        print(
            f"  {r['signal_probability_threshold']:>6.2f}"
            f" {r['fpt_horizon_bars']:>5}"
            f" {r['sweep_expiry_bars']:>5}"
            f" {r['tp_rr_ratio']:>5.1f}"
            f" {r.get('min_poi_atr_width', 0.3):>5.2f}"
            f" | {r['signals']:>4} {r['wins']:>3} {r['losses']:>3} {r['neutral']:>3}"
            f" | {r['win_rate']:>5.1f}% {r['expectancy_r']:>+7.3f}"
            f" {r['mfe_mae']:>7.2f} {r['avg_prob']:>5.1f}%"
        )

    # ── Best combo summary ──
    if results:
        best = results[0]
        print("\n" + "-" * 105)
        print("  BEST COMBO:")
        for k in keys:
            print(f"    {k} = {best[k]}")
        print(
            f"    >> {best['signals']} signals, {best['win_rate']:.1f}% WR, "
            f"expectancy {best['expectancy_r']:+.3f}R"
        )

    # ── Also show combos with best WR (min 5 signals) ──
    valid = [r for r in results if r["signals"] >= 5]
    if valid:
        valid.sort(key=lambda r: r["win_rate"], reverse=True)
        top_wr = valid[0]
        print(f"\n  BEST WR (min 5 signals):")
        for k in keys:
            print(f"    {k} = {top_wr[k]}")
        print(
            f"    >> {top_wr['signals']} signals, {top_wr['win_rate']:.1f}% WR, "
            f"expectancy {top_wr['expectancy_r']:+.3f}R"
        )

    print("=" * 105)


if __name__ == "__main__":
    main()

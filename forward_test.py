"""
Forward test (walk-forward OOS validation) for indicator5.

Splits historical data into in-sample (IS) and out-of-sample (OOS) periods.
State machine warms up on IS data to build full context, then signals are
collected only from the OOS period -- mimicking real forward testing.

Compares IS vs OOS metrics to validate that backtest expectations hold
on unseen data (overfitting detection).

Usage:
    python forward_test.py --symbol BTCUSDm --timeframe M15 --bars 10000
    python forward_test.py --symbol XAUUSDm --timeframe M15 --bars 10000 --oos-pct 30
    python forward_test.py --symbol XAUUSDm --timeframe M15 --bars 10000 --oos-bars 3000
    python forward_test.py --symbol BTCUSDm --timeframe M15 --bars 10000 --balance 100 --risk-pct 2 --leverage 100
"""

import argparse
import sys
import time
import os
import glob
import shutil
from copy import copy
from datetime import datetime, UTC

import numpy as np

from config import Config
from smc import run_smc_detection
from probability import score_poi
from state_machine import EntryStateMachine
from models import Direction
from backtest import (
    load_historical_data,
    run_backtest,
    print_results,
    _build_smc_snapshot,
    _build_trend_per_bar,
)


# ---------------------------------------------------------------------------
# OOS backtest engine
# ---------------------------------------------------------------------------

def run_oos_backtest(rates, config: Config, warmup: int, split_bar: int,
                     eval_bars: int = 20, slippage: float = 0.0, max_wait: int = 0):
    """Walk-forward OOS backtest.

    State machine processes bars from warmup to end (building full context),
    but signals are only collected from split_bar onward.
    This mimics real forward testing where historical context exists.

    If max_wait > 0, simulates pending order fill:
    - Signal at bar N generates ref_price = close[N]
    - Order tries to fill at ref_price from bar N+1 to N+max_wait
    - First bar where low <= ref_price <= high → fill at ref_price
    - If no fill within max_wait bars → signal skipped
    """
    sm = EntryStateMachine(config)
    signals = []
    n = len(rates)

    # Phase 1: Pre-compute SMC on full data
    t0 = time.time()
    print("Pre-computing SMC structures on full dataset...", end="", flush=True)
    full_smc = run_smc_detection(rates, config)
    trend_per_bar = _build_trend_per_bar(full_smc["structure_breaks"], n)
    elapsed = time.time() - t0
    print(f" done ({elapsed:.1f}s)")

    n_obs = len(full_smc["order_blocks"])
    n_fvgs = len(full_smc["fvgs"])
    n_breaks = len(full_smc["structure_breaks"])
    n_pools = len(full_smc["liquidity_pools"])
    print(f"  Structures: {n_obs} OBs, {n_fvgs} FVGs, {n_breaks} breaks, {n_pools} pools")

    # Phase 2: Walk forward -- warm up on IS, collect signals on OOS
    print(f"\nRunning OOS walk-forward... (warmup={warmup}, split={split_bar}, eval={eval_bars})")
    print(f"  IS warm-up : bars {warmup}-{split_bar - 1} (state machine builds context)")
    print(f"  OOS collect: bars {split_bar}-{n - 1} (signals collected here)\n")

    for bar in range(warmup, n):
        smc_snapshot = _build_smc_snapshot(full_smc, bar, trend_per_bar)
        slice_rates = rates[:bar + 1]
        new_signals = sm.process(smc_snapshot, slice_rates, score_poi)

        # Only collect signals from OOS period
        if bar >= split_bar:
            for sig in new_signals:
                signals.append({
                    "bar_index": bar,
                    "time": int(rates[bar]["time"]),
                    "direction": sig.direction,
                    "entry_price": sig.price,
                    "probability": sig.probability,
                    "poi_type": sig.poi.poi_type.value,
                    "poi_top": sig.poi.top,
                    "poi_bottom": sig.poi.bottom,
                })

        if (bar - warmup) % 500 == 0 and bar > warmup:
            phase = "IS warm-up" if bar < split_bar else "OOS"
            pct = (bar - warmup) / (n - warmup) * 100
            print(f"  Progress: {pct:.0f}% [{phase}] ({len(signals)} OOS signals)")

    print(f"\nOOS walk-forward complete. OOS signals: {len(signals)}\n")

    # Phase 3: Evaluate each signal
    results = []
    skipped_no_fill = 0
    n = len(rates)

    for sig in signals:
        bar = sig["bar_index"]
        ref_entry = sig["entry_price"]  # Original close price (ref for limit order)
        is_buy = sig["direction"] == Direction.BULLISH

        # --- Pending Order Fill Simulation ---
        if max_wait > 0:
            # Try to fill at ref_entry price from bar+1 to bar+max_wait
            fill_bar = None
            fill_search_end = min(bar + 1 + max_wait, n)

            for i in range(bar + 1, fill_search_end):
                candle_low = float(rates[i]["low"])
                candle_high = float(rates[i]["high"])

                # Order fills if price passes through ref_entry
                if candle_low <= ref_entry <= candle_high:
                    fill_bar = i
                    break

            if fill_bar is None:
                skipped_no_fill += 1
                continue  # Order never filled, skip signal

            # Evaluation starts from fill_bar + 1
            eval_start = fill_bar + 1
            entry = ref_entry  # Limit order fills at exact price
        else:
            # Original behavior: instant fill at bar close (unrealistic but backward compatible)
            eval_start = bar + 1
            # Apply slippage (unfavorable direction for trader)
            if slippage > 0:
                slip_direction = 1 if is_buy else -1
                entry = ref_entry + (slippage * slip_direction)
            else:
                entry = ref_entry

        # Check if enough bars for evaluation
        if eval_start + eval_bars > n:
            continue

        future = rates[eval_start: eval_start + eval_bars]

        future_highs = future["high"].astype(float)
        future_lows = future["low"].astype(float)
        future_closes = future["close"].astype(float)

        if is_buy:
            max_favorable = float(np.max(future_highs) - entry)
            max_adverse = float(entry - np.min(future_lows))
            final_pnl = float(future_closes[-1] - entry)
        else:
            max_favorable = float(entry - np.min(future_lows))
            max_adverse = float(np.max(future_highs) - entry)
            final_pnl = float(entry - future_closes[-1])

        # SL distance = POI height (fixed, matches EA logic)
        poi_height = abs(sig["poi_top"] - sig["poi_bottom"])
        sl_distance = poi_height if poi_height > 0 else 0.01  # Matches mt5_interface.py fallback
        tp_distance = sl_distance * config.tp_rr_ratio

        hit_tp = max_favorable >= tp_distance
        hit_sl = max_adverse >= sl_distance

        outcome = "neutral"
        if hit_tp and hit_sl:
            for i in range(len(future)):
                if is_buy:
                    if future_lows[i] <= entry - sl_distance:
                        outcome = "loss"
                        break
                    if future_highs[i] >= entry + tp_distance:
                        outcome = "win"
                        break
                else:
                    if future_highs[i] >= entry + sl_distance:
                        outcome = "loss"
                        break
                    if future_lows[i] <= entry - tp_distance:
                        outcome = "win"
                        break
        elif hit_tp:
            outcome = "win"
        elif hit_sl:
            outcome = "loss"

        if outcome == "win":
            pnl_r = config.tp_rr_ratio
        elif outcome == "loss":
            pnl_r = -1.0
        else:
            pnl_r = final_pnl / sl_distance if sl_distance > 0 else 0.0

        dt = datetime.fromtimestamp(sig["time"], UTC).strftime("%Y-%m-%d %H:%M")
        results.append({
            **sig,
            "entry_price": entry,  # Use actual fill price
            "ref_entry": ref_entry,  # Keep original for reference
            "fill_bar": fill_bar if max_wait > 0 else bar,  # Bar where order filled
            "datetime": dt,
            "max_favorable": max_favorable,
            "max_adverse": max_adverse,
            "final_pnl": final_pnl,
            "pnl_r": pnl_r,
            "outcome_1to2": outcome,
        })

    if max_wait > 0 and skipped_no_fill > 0:
        print(f"  Pending order simulation: {skipped_no_fill} signals skipped (no fill within {max_wait} bars)")

    return results


# ---------------------------------------------------------------------------
# Metrics extraction
# ---------------------------------------------------------------------------

def compute_metrics(results, config):
    """Extract performance metrics from results into a dict for comparison."""
    if not results:
        return {
            "total": 0, "wins": 0, "losses": 0, "neutral": 0,
            "win_rate": 0, "avg_prob": 0, "net_profit": 0,
            "gross_profit": 0, "gross_loss": 0,
            "expectancy": 0, "profit_factor": 0, "sharpe": 0,
            "max_dd_r": 0, "recovery_factor": 0, "payoff_ratio": 0,
            "avg_mfe": 0, "avg_mae": 0, "mfe_mae": 0,
        }

    total = len(results)
    wins = sum(1 for r in results if r["outcome_1to2"] == "win")
    losses = sum(1 for r in results if r["outcome_1to2"] == "loss")
    neutral = sum(1 for r in results if r["outcome_1to2"] == "neutral")
    decisive = wins + losses
    win_rate = wins / decisive * 100 if decisive > 0 else 0

    all_r = [r["pnl_r"] for r in results]
    win_r = [r["pnl_r"] for r in results if r["outcome_1to2"] == "win"]
    loss_r = [r["pnl_r"] for r in results if r["outcome_1to2"] == "loss"]

    gross_profit = sum(win_r) if win_r else 0.0
    gross_loss = abs(sum(loss_r)) if loss_r else 0.0
    net_profit = sum(all_r)
    avg_win = float(np.mean(win_r)) if win_r else 0.0
    avg_loss = float(abs(np.mean(loss_r))) if loss_r else 0.0

    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    payoff_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")
    expectancy = net_profit / decisive if decisive > 0 else 0.0

    sharpe = (float(np.mean(all_r) / np.std(all_r, ddof=1))
              if len(all_r) > 1 and np.std(all_r, ddof=1) > 0 else 0.0)

    cum_r = np.cumsum(all_r)
    peak = np.maximum.accumulate(cum_r)
    drawdowns = peak - cum_r
    max_dd_r = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

    recovery_factor = net_profit / max_dd_r if max_dd_r > 0 else float("inf")

    avg_prob = float(np.mean([r["probability"] for r in results]))
    avg_mfe = float(np.mean([r["max_favorable"] for r in results]))
    avg_mae = float(np.mean([r["max_adverse"] for r in results]))
    mfe_mae = avg_mfe / avg_mae if avg_mae > 0 else float("inf")

    return {
        "total": total, "wins": wins, "losses": losses, "neutral": neutral,
        "win_rate": win_rate, "avg_prob": avg_prob,
        "net_profit": net_profit, "gross_profit": gross_profit,
        "gross_loss": gross_loss, "expectancy": expectancy,
        "profit_factor": profit_factor, "sharpe": sharpe,
        "max_dd_r": max_dd_r, "recovery_factor": recovery_factor,
        "payoff_ratio": payoff_ratio,
        "avg_mfe": avg_mfe, "avg_mae": avg_mae, "mfe_mae": mfe_mae,
    }


# ---------------------------------------------------------------------------
# Comparison report
# ---------------------------------------------------------------------------

def print_comparison(is_metrics, oos_metrics, is_period, oos_period, config):
    """Print IS vs OOS metrics comparison table with verdict."""
    rr_label = f"1:{config.tp_rr_ratio:.1f}"

    print()
    print("=" * 70)
    print("  FORWARD TEST COMPARISON | In-Sample vs Out-of-Sample")
    print(f"  Risk:Reward: {rr_label}")
    print("=" * 70)
    print(f"  IS  Period: {is_period}")
    print(f"  OOS Period: {oos_period}")
    print("-" * 70)

    rows = [
        ("Signals",         "total",            "{:.0f}",    False),
        ("  Wins",          "wins",             "{:.0f}",    False),
        ("  Losses",        "losses",           "{:.0f}",    False),
        ("Win Rate",        "win_rate",         "{:.1f}%",   True),
        ("Avg P(win)",      "avg_prob",         "{:.2%}",    True),
        ("Net Profit",      "net_profit",       "{:+.2f}R",  False),
        ("Expectancy",      "expectancy",       "{:+.3f}R",  True),
        ("Profit Factor",   "profit_factor",    "{:.2f}",    True),
        ("Sharpe Ratio",    "sharpe",           "{:.2f}",    True),
        ("Max Drawdown",    "max_dd_r",         "{:.2f}R",   True),
        ("Recovery Factor", "recovery_factor",  "{:.2f}",    True),
        ("Payoff Ratio",    "payoff_ratio",     "{:.2f}",    True),
        ("MFE/MAE",         "mfe_mae",          "{:.2f}",    True),
    ]

    print(f"  {'Metric':<18} {'In-Sample':>14} {'Out-of-Sample':>14} {'Diff':>10}")
    print(f"  {'':->18} {'':->14} {'':->14} {'':->10}")

    for label, key, fmt, show_diff in rows:
        is_val = is_metrics[key]
        oos_val = oos_metrics[key]

        is_str = fmt.format(is_val) if is_val != float("inf") else "inf"
        oos_str = fmt.format(oos_val) if oos_val != float("inf") else "inf"

        if show_diff and is_val != float("inf") and oos_val != float("inf"):
            diff = oos_val - is_val
            if "%" in fmt and ":" not in fmt:
                diff_str = f"{diff:+.1f}"
            else:
                diff_str = f"{diff:+.2f}"
        else:
            diff_str = ""

        print(f"  {label:<18} {is_str:>14} {oos_str:>14} {diff_str:>10}")

    # --- Verdict ---
    print("-" * 70)
    verdicts = []

    # 1. OOS profitable?
    if oos_metrics["total"] == 0:
        verdicts.append(("OOS has signals", False))
    else:
        verdicts.append(("OOS Profitable", oos_metrics["net_profit"] > 0))

    # 2. Win rate consistency (within 10 percentage points)
    if is_metrics["win_rate"] > 0 and oos_metrics["total"] > 0:
        wr_diff = abs(oos_metrics["win_rate"] - is_metrics["win_rate"])
        verdicts.append((f"WR diff {wr_diff:.1f}pp (< 10pp)", wr_diff < 10))

    # 3. Profit factor consistency (OOS PF > 0.7x IS PF)
    if (is_metrics["profit_factor"] > 0
            and is_metrics["profit_factor"] != float("inf")
            and oos_metrics["total"] > 0):
        pf_ratio = (oos_metrics["profit_factor"] / is_metrics["profit_factor"]
                     if is_metrics["profit_factor"] > 0 else 0)
        verdicts.append((f"PF ratio {pf_ratio:.2f}x (> 0.70x)", pf_ratio > 0.7))

    # 4. OOS expectancy positive?
    if is_metrics["expectancy"] > 0 and oos_metrics["total"] > 0:
        verdicts.append(("OOS Expectancy > 0", oos_metrics["expectancy"] > 0))

    all_pass = all(v[1] for v in verdicts) if verdicts else False

    for desc, passed in verdicts:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {desc}")

    print("-" * 70)
    if not verdicts:
        print("  VERDICT: NO DATA -- cannot evaluate")
    elif all_pass:
        print("  VERDICT: OOS VALIDATES IN-SAMPLE -- forward test passed")
    elif sum(v[1] for v in verdicts) >= len(verdicts) / 2:
        print("  VERDICT: PARTIAL VALIDATION -- some metrics diverge, review carefully")
    else:
        print("  VERDICT: OOS DOES NOT VALIDATE -- possible overfitting or regime change")
    print("=" * 70)


def export_signals_json(oos_results, is_results, config: Config, symbol: str,
                        split_bar: int, filename: str = None):
    """Export signals to JSON for MT5 Strategy Tester replay.

    Includes both IS and OOS signals with is_oos flag:
    - is_oos=0 for IS signals (Strategy Tester will skip)
    - is_oos=1 for OOS signals (Strategy Tester will execute)

    JSON format matches Indicator5_EA.mq5 signal parsing.
    """
    import json
    import os
    import time

    all_results = []
    # Add IS signals with is_oos=0
    for r in is_results:
        r["is_oos"] = 0
        all_results.append(r)
    # Add OOS signals with is_oos=1
    for r in oos_results:
        r["is_oos"] = 1
        all_results.append(r)

    # Sort by bar_index
    all_results.sort(key=lambda x: x["bar_index"])

    if not all_results:
        print("No signals to export.")
        return

    if filename is None:
        filename = f"forward_test_signals_{symbol}.json"

    # Get MT5 data path for output
    try:
        import MetaTrader5 as mt5
        if mt5.initialize():
            terminal_info = mt5.terminal_info()
            output_dir = os.path.join(terminal_info.data_path, "MQL5", "Files")
            mt5.shutdown()
        else:
            output_dir = "."
    except:
        output_dir = "."

    filepath = os.path.join(output_dir, filename)

    signals_list = []
    for r in all_results:
        is_buy = r["direction"] == Direction.BULLISH
        entry = r["entry_price"]

        # Calculate SL/TP using entry-to-edge logic (matches mt5_interface.py)
        if is_buy:
            sl_price = r["poi_bottom"]  # SL at zone bottom
            sl_distance = entry - sl_price
        else:
            sl_price = r["poi_top"]  # SL at zone top
            sl_distance = sl_price - entry

        # Guard against invalid distances
        if sl_distance <= 0:
            sl_distance = 0.01

        tp_distance = sl_distance * config.tp_rr_ratio

        if is_buy:
            tp_price = entry + tp_distance
        else:
            tp_price = entry - tp_distance

        signals_list.append({
            "time": r["time"],  # Unix timestamp
            "direction": "bullish" if is_buy else "bearish",
            "price": round(entry, 5),
            "sl_price": round(sl_price, 5),
            "tp_price": round(tp_price, 5),
            "probability": round(r["probability"], 4),
            "poi_type": r["poi_type"],
            "is_oos": r["is_oos"],
            # Extra fields for consistency with mt5_interface.py (Replay EA can use these)
            "poi_top": round(r["poi_top"], 5),
            "poi_bottom": round(r["poi_bottom"], 5),
            "sl_distance": round(abs(sl_distance), 5),
            "tp_distance": round(tp_distance, 5),
            "tp_rr_ratio": config.tp_rr_ratio,
        })

    # Write JSON with same structure as live signal file
    data = {
        "timestamp": int(time.time()),
        "symbol": symbol,
        "split_bar": split_bar,
        "signals": signals_list,
    }

    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

    is_count = sum(1 for s in signals_list if s["is_oos"] == 0)
    oos_count = sum(1 for s in signals_list if s["is_oos"] == 1)

    print(f"\n[JSON EXPORT] {len(signals_list)} signals exported to:")
    print(f"  {filepath}")
    print(f"  IS signals: {is_count} (is_oos=0, will be skipped)")
    print(f"  OOS signals: {oos_count} (is_oos=1, will be executed)")
    print(f"  Ready for MT5 Strategy Tester replay.")

    # Copy to Strategy Tester sandbox folder for Replay EA to find
    _copy_to_tester_sandbox(filepath, filename)


# ---------------------------------------------------------------------------
# Copy JSON to Strategy Tester sandbox (for Replay EA)
# ---------------------------------------------------------------------------

def _copy_to_tester_sandbox(source_filepath, filename):
    """Copy JSON file to Strategy Tester sandbox folder for Replay EA.

    MT5 Strategy Tester runs in isolated sandbox:
    C:\Program Files\MetaTrader\Tester\Agent-127.0.0.1-XXXX\MQL5\Files\

    Python exports to main terminal folder:
    C:\Program Files\MetaTrader\MQL5\Files\

    We need to copy to sandbox for FileOpen() in MQL5 to find it.
    """
    try:
        # Find Tester sandbox paths (multiple agents may be running)
        tester_base = r"C:\Program Files\MetaTrader\Tester"
        agent_pattern = os.path.join(tester_base, "Agent-*", "MQL5", "Files")

        tester_paths = glob.glob(agent_pattern)

        if not tester_paths:
            print(f"  [INFO] Strategy Tester sandbox not found at {agent_pattern}")
            return

        # Copy to each Tester instance found
        for tester_path in tester_paths:
            os.makedirs(tester_path, exist_ok=True)
            dest_filepath = os.path.join(tester_path, filename)
            shutil.copy2(source_filepath, dest_filepath)
            print(f"  [COPY] Also copied to Tester sandbox: {dest_filepath}")

    except Exception as e:
        print(f"  [WARN] Could not copy to Tester sandbox: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Indicator5 Forward Test (Walk-Forward OOS Validation)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python forward_test.py --symbol BTCUSDm --timeframe M15 --bars 10000
  python forward_test.py --symbol XAUUSDm --timeframe M15 --bars 10000 --oos-pct 30
  python forward_test.py --symbol XAUUSDm --timeframe M15 --bars 10000 --oos-bars 3000
  python forward_test.py --symbol BTCUSDm --timeframe M15 --bars 10000 --balance 100 --risk-pct 2 --leverage 100
""")

    # Same parameters as backtest.py
    parser.add_argument("--symbol", default="EURUSDm")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--bars", type=int, default=5000)
    parser.add_argument("--eval-bars", type=int, default=20)
    parser.add_argument("--threshold", type=float, default=0.3)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--balance", type=float, default=0,
                        help="Starting balance ($). 0 = skip balance simulation.")
    parser.add_argument("--risk-pct", type=float, default=1.0,
                        help="Risk per trade as %% of current balance (default 1%%)")
    parser.add_argument("--leverage", type=int, default=0,
                        help="Leverage ratio (e.g. 100 for 1:100). 0 = don't show lots/margin.")
    parser.add_argument("--lot", type=float, default=0,
                        help="Fixed lot size (e.g. 0.01). Overrides --risk-pct for position sizing.")
    parser.add_argument("--min-lot", type=float, default=0.01,
                        help="Minimum lot size enforced by broker (default 0.01).")

    # Forward test specific
    parser.add_argument("--oos-pct", type=float, default=30,
                        help="OOS period as %% of total bars (default 30%%)")
    parser.add_argument("--oos-bars", type=int, default=0,
                        help="Explicit OOS bar count. Overrides --oos-pct.")
    parser.add_argument("--slippage", type=float, default=0.0,
                        help="Slippage in price units (e.g. 0.5 for XAU, 50 for BTC). Simulates unfavorable entry.")
    parser.add_argument("--max-wait", type=int, default=0,
                        help="Max bars to wait for pending order fill (0 = instant fill at close, realistic: 3-5)")
    parser.add_argument("--export-json", action="store_true",
                        help="Export signals to JSON for MT5 Strategy Tester replay")

    args = parser.parse_args()

    config = Config(
        symbol=args.symbol,
        timeframe=args.timeframe,
        num_bars=args.bars,
        signal_probability_threshold=args.threshold,
    )

    # Load all data
    rates = load_historical_data(args.symbol, args.timeframe, args.bars)
    n = len(rates)

    # Calculate split point
    if args.oos_bars > 0:
        oos_count = min(args.oos_bars, n - args.warmup - 50)
    else:
        oos_count = int(n * args.oos_pct / 100)
    split = n - oos_count

    if split <= args.warmup:
        print(f"Error: split bar ({split}) must be > warmup ({args.warmup}). "
              "Reduce --oos-pct or --oos-bars.")
        sys.exit(1)

    is_start = datetime.fromtimestamp(rates[0]["time"], UTC).strftime("%Y-%m-%d %H:%M")
    is_end = datetime.fromtimestamp(rates[split - 1]["time"], UTC).strftime("%Y-%m-%d %H:%M")
    oos_start = datetime.fromtimestamp(rates[split]["time"], UTC).strftime("%Y-%m-%d %H:%M")
    oos_end = datetime.fromtimestamp(rates[-1]["time"], UTC).strftime("%Y-%m-%d %H:%M")

    is_period = f"{is_start} >> {is_end} ({split} bars)"
    oos_period = f"{oos_start} >> {oos_end} ({oos_count} bars)"

    print(f"\n{'=' * 70}")
    print(f"  FORWARD TEST | {config.symbol} {config.timeframe}")
    print(f"{'=' * 70}")
    print(f"  Total Bars    : {n}")
    print(f"  IS  Period    : {is_period}")
    print(f"  OOS Period    : {oos_period}")
    print(f"  OOS Split     : {args.oos_pct:.0f}% ({oos_count} bars)")
    print(f"  Eval Horizon  : {args.eval_bars} bars")
    print(f"  Threshold     : {config.signal_probability_threshold:.0%}")
    print(f"{'=' * 70}\n")

    # --- Phase 1: In-Sample Backtest ---
    print("=" * 70)
    print("  PHASE 1: IN-SAMPLE BACKTEST")
    print("=" * 70)
    is_results = run_backtest(rates[:split], config,
                              warmup=args.warmup, eval_bars=args.eval_bars,
                              slippage=args.slippage, max_wait=args.max_wait)
    is_metrics = compute_metrics(is_results, config)

    # Print IS summary (compact -- full detail is optional, focus is on OOS)
    if is_metrics["total"] > 0:
        print(f"  IS Results: {is_metrics['total']} signals, "
              f"WR {is_metrics['win_rate']:.1f}%, "
              f"Net {is_metrics['net_profit']:+.2f}R, "
              f"PF {is_metrics['profit_factor']:.2f}, "
              f"Sharpe {is_metrics['sharpe']:.2f}")
    else:
        print("  IS Results: No signals generated in IS period.")
    print()

    # --- Phase 2: Out-of-Sample Forward Test ---
    print("=" * 70)
    print("  PHASE 2: OUT-OF-SAMPLE FORWARD TEST")
    print("=" * 70)
    oos_results = run_oos_backtest(rates, config,
                                   warmup=args.warmup, split_bar=split,
                                   eval_bars=args.eval_bars, slippage=args.slippage,
                                   max_wait=args.max_wait)

    # Print full OOS results (same format as backtest -- all features)
    print_results(oos_results, config,
                  starting_balance=args.balance, risk_pct=args.risk_pct,
                  leverage=args.leverage, fixed_lot=args.lot,
                  min_lot=args.min_lot, title="OUT-OF-SAMPLE RESULTS")

    # --- Phase 3: Comparison ---
    oos_metrics = compute_metrics(oos_results, config)
    print_comparison(is_metrics, oos_metrics, is_period, oos_period, config)

    # Export to JSON for MT5 Strategy Tester if requested
    if args.export_json:
        export_signals_json(oos_results, is_results, config, args.symbol, split)



if __name__ == "__main__":
    main()

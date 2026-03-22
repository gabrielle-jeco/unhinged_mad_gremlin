"""
Backtest script for indicator5.
Simulates bar-by-bar walk-forward on historical data.

Performance: SMC is pre-computed once on full data, then per-bar snapshots
are built by filtering structures — O(n * structures) instead of O(n^2).

Usage:
    python backtest.py
    python backtest.py --symbol XAUUSDm --timeframe M15 --bars 5000 --eval-bars 30
"""

import argparse
import sys
import time
import os
import glob
import shutil
from copy import copy
from datetime import datetime, UTC

import MetaTrader5 as mt5
import numpy as np

from config import Config
from smc import run_smc_detection
from probability import score_poi
from state_machine import EntryStateMachine
from models import Direction


def load_historical_data(symbol: str, timeframe: str, num_bars: int):
    """Load historical bars from MT5."""
    if not mt5.initialize():
        print(f"MT5 initialization failed: {mt5.last_error()}")
        sys.exit(1)

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        print(f"Symbol {symbol} not found")
        mt5.shutdown()
        sys.exit(1)
    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)

    tf = Config.timeframe_to_mt5(timeframe)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, num_bars)
    mt5.shutdown()

    if rates is None or len(rates) == 0:
        print("Failed to load data")
        sys.exit(1)

    print(f"Loaded {len(rates)} bars of {symbol} {timeframe}")
    start = datetime.fromtimestamp(rates[0]["time"], UTC).strftime("%Y-%m-%d %H:%M")
    end = datetime.fromtimestamp(rates[-1]["time"], UTC).strftime("%Y-%m-%d %H:%M")
    print(f"Period: {start} >> {end}\n")
    return rates


# ---------------------------------------------------------------------------
# Snapshot builder: O(structures) per bar instead of O(n) per bar
# ---------------------------------------------------------------------------

def _build_smc_snapshot(full_smc, bar, trend_per_bar):
    """Build SMC results for a specific bar from pre-computed full data."""
    # OBs: include if created at/before this bar, adjust is_active by lifecycle
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

    # FVGs: include if created at/before this bar, adjust is_active
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
        # Pools & breaks: state machine filters by sweep_bar/bar_index internally
        "liquidity_pools": full_smc["liquidity_pools"],
        "structure_breaks": full_smc["structure_breaks"],
        "order_blocks": obs_snapshot,
        "fvgs": fvg_snapshot,
        "atr": full_smc["atr"][:bar + 1],
        "trend_direction": trend_per_bar[bar],
    }


def _build_trend_per_bar(structure_breaks, n):
    """Pre-compute trend direction at each bar from structure break history."""
    trend_per_bar = ["none"] * n
    sorted_breaks = sorted(structure_breaks, key=lambda x: x.bar_index)

    current_trend = "none"
    break_idx = 0
    for bar in range(n):
        while break_idx < len(sorted_breaks) and sorted_breaks[break_idx].bar_index <= bar:
            sb = sorted_breaks[break_idx]
            current_trend = "bullish" if sb.direction == Direction.BULLISH else "bearish"
            break_idx += 1
        trend_per_bar[bar] = current_trend

    return trend_per_bar


# ---------------------------------------------------------------------------
# Walk-forward backtest
# ---------------------------------------------------------------------------

def run_backtest(rates, config: Config, warmup: int = 100, eval_bars: int = 20,
                 slippage: float = 0.0, max_wait: int = 0):
    """Walk-forward backtest with O(n) SMC pre-computation.

    If max_wait > 0, simulates pending order fill:
    - Signal at bar N generates ref_price = close[N]
    - Order tries to fill at ref_price from bar N+1 to N+max_wait
    - First bar where low <= ref_price <= high → fill at ref_price
    - If no fill within max_wait bars → signal skipped
    """
    sm = EntryStateMachine(config)
    signals = []
    n = len(rates)

    # --- Phase 1: Pre-compute SMC once on full data ---
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

    # --- Phase 2: Walk-forward with snapshots ---
    print(f"\nRunning backtest... (warmup={warmup}, eval_horizon={eval_bars} bars)")
    print(f"P(win) threshold: {config.signal_probability_threshold:.0%}\n")

    for bar in range(warmup, n):
        smc_snapshot = _build_smc_snapshot(full_smc, bar, trend_per_bar)
        slice_rates = rates[:bar + 1]

        new_signals = sm.process(smc_snapshot, slice_rates, score_poi)

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
            pct = (bar - warmup) / (n - warmup) * 100
            print(f"  Progress: {pct:.0f}% ({len(signals)} signals so far)")

    print(f"\nWalk-forward complete. Total raw signals: {len(signals)}\n")

    # --- Evaluate each signal ---
    results = []
    skipped_no_fill = 0

    for sig in signals:
        bar = sig["bar_index"]
        ref_entry = sig["entry_price"]  # Original close price (ref for limit order)
        is_buy = sig["direction"] == Direction.BULLISH

        # --- Pending Order Fill Simulation ---
        if max_wait > 0:
            # Try to fill at ref_entry price from bar+1 to bar+max_wait
            fill_bar = None
            fill_search_end = min(bar + 1 + max_wait, len(rates))

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
        if eval_start + eval_bars > len(rates):
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

        # Win/loss with configurable RR benchmark
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

        # R-based PnL: win = +RR, loss = -1R, neutral = fractional R
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


def print_results(results, config: Config, starting_balance: float = 0,
                  risk_pct: float = 1.0, leverage: int = 0,
                  fixed_lot: float = 0, min_lot: float = 0.01,
                  title: str = "BACKTEST RESULTS"):
    """Print backtest results summary with comprehensive performance metrics."""
    if not results:
        print("No signals with enough evaluation data.")
        return

    total = len(results)
    wins = sum(1 for r in results if r["outcome_1to2"] == "win")
    losses = sum(1 for r in results if r["outcome_1to2"] == "loss")
    neutral = sum(1 for r in results if r["outcome_1to2"] == "neutral")
    decisive = wins + losses
    win_rate = wins / decisive * 100 if decisive > 0 else 0

    buys = sum(1 for r in results if r["direction"] == Direction.BULLISH)
    sells = total - buys

    avg_mfe = np.mean([r["max_favorable"] for r in results])
    avg_mae = np.mean([r["max_adverse"] for r in results])
    avg_prob = np.mean([r["probability"] for r in results])

    # --- R-based metrics ---
    all_r = [r["pnl_r"] for r in results]
    win_r = [r["pnl_r"] for r in results if r["outcome_1to2"] == "win"]
    loss_r = [r["pnl_r"] for r in results if r["outcome_1to2"] == "loss"]

    gross_profit = sum(win_r) if win_r else 0.0
    gross_loss = abs(sum(loss_r)) if loss_r else 0.0
    net_profit = sum(all_r)
    avg_win = np.mean(win_r) if win_r else 0.0
    avg_loss = abs(np.mean(loss_r)) if loss_r else 0.0

    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    payoff_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")
    expectancy = net_profit / decisive if decisive > 0 else 0.0

    # Sharpe ratio (R-returns, no risk-free adjustment)
    sharpe = float(np.mean(all_r) / np.std(all_r, ddof=1)) if len(all_r) > 1 and np.std(all_r, ddof=1) > 0 else 0.0

    # Max drawdown from cumulative R equity curve
    cum_r = np.cumsum(all_r)
    peak = np.maximum.accumulate(cum_r)
    drawdowns = peak - cum_r
    max_dd_r = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
    max_dd_pct = (max_dd_r / float(np.max(peak)) * 100) if float(np.max(peak)) > 0 else 0.0

    recovery_factor = net_profit / max_dd_r if max_dd_r > 0 else float("inf")

    # --- Print ---
    rr_label = f"1:{config.tp_rr_ratio:.1f}"

    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)
    print(f"  Total Signals     : {total} ({buys} buy, {sells} sell)")
    print(f"  Risk:Reward       : {rr_label}")
    print(f"  Avg P(win)        : {avg_prob:.2%}")
    print()

    # Trade outcomes
    print("  TRADE OUTCOMES:")
    print(f"    Wins            : {wins}")
    print(f"    Losses          : {losses}")
    print(f"    Neutral         : {neutral}")
    print(f"    Win Rate        : {win_rate:.1f}%")
    print()

    # R-based performance
    print("  PERFORMANCE (R-based):")
    print(f"    Gross Profit    : +{gross_profit:.2f}R")
    print(f"    Gross Loss      : -{gross_loss:.2f}R")
    print(f"    Net Profit      : {'+' if net_profit >= 0 else ''}{net_profit:.2f}R")
    print(f"    Avg Win         : +{avg_win:.2f}R")
    print(f"    Avg Loss        : -{avg_loss:.2f}R")
    print(f"    Payoff Ratio    : {payoff_ratio:.2f}")
    print(f"    Expectancy      : {'+' if expectancy >= 0 else ''}{expectancy:.3f}R/trade")
    print()

    # Risk metrics
    print("  RISK METRICS:")
    print(f"    Profit Factor   : {profit_factor:.2f}")
    print(f"    Sharpe Ratio    : {sharpe:.2f}")
    print(f"    Max Drawdown    : {max_dd_r:.2f}R ({max_dd_pct:.1f}%)")
    print(f"    Recovery Factor : {recovery_factor:.2f}")
    print()

    # MFE/MAE
    print("  EFFICIENCY:")
    print(f"    Avg MFE         : {avg_mfe:.5f}")
    print(f"    Avg MAE         : {avg_mae:.5f}")
    if avg_mae > 0:
        print(f"    MFE/MAE Ratio   : {avg_mfe / avg_mae:.2f}")
    print("-" * 70)

    # Breakdown by probability bands
    bands = [(0.20, 0.30), (0.30, 0.35), (0.35, 0.40), (0.40, 1.0)]
    print("  BREAKDOWN BY P(win):")
    print(f"  {'Band':<12} {'Count':>6} {'Wins':>6} {'Losses':>6} {'WR%':>8} {'PnL(R)':>9}")
    for lo, hi in bands:
        band_r = [r for r in results if lo <= r["probability"] < hi]
        if not band_r:
            continue
        b_wins = sum(1 for r in band_r if r["outcome_1to2"] == "win")
        b_losses = sum(1 for r in band_r if r["outcome_1to2"] == "loss")
        b_wr = b_wins / (b_wins + b_losses) * 100 if (b_wins + b_losses) > 0 else 0
        b_pnl = sum(r["pnl_r"] for r in band_r)
        print(f"  {lo:.0%}-{hi:.0%}      {len(band_r):>6} {b_wins:>6} {b_losses:>6} {b_wr:>7.1f}% {b_pnl:>+8.2f}R")

    # Equity curve (mini text chart)
    print("-" * 70)
    print("  EQUITY CURVE (R):")
    cum_list = list(cum_r)
    if cum_list:
        min_eq = min(cum_list)
        max_eq = max(cum_list)
        chart_width = 40
        for i, eq in enumerate(cum_list):
            r = results[i]
            d = "B" if r["direction"] == Direction.BULLISH else "S"
            marker = "W" if r["outcome_1to2"] == "win" else ("L" if r["outcome_1to2"] == "loss" else "-")
            # Scale position in chart
            if max_eq - min_eq > 0:
                pos = int((eq - min_eq) / (max_eq - min_eq) * (chart_width - 1))
            else:
                pos = chart_width // 2
            bar_str = "." * pos + marker + "." * (chart_width - 1 - pos)
            print(f"  {i+1:>3} {d}{marker} {eq:>+7.2f}R |{bar_str}|")

    # --- Balance Simulation ---
    if starting_balance > 0:
        # Auto-detect contract size from symbol
        sym = config.symbol.upper().replace("M", "").replace(".", "")
        if "XAU" in sym or "GOLD" in sym:
            contract_size = 100        # 1 lot = 100 oz
        elif "BTC" in sym:
            contract_size = 1          # 1 lot = 1 BTC
        elif "ETH" in sym:
            contract_size = 1
        else:
            contract_size = 100_000    # 1 lot = 100k units (forex)

        use_fixed_lot = fixed_lot > 0
        show_leverage = leverage > 0 or use_fixed_lot

        print("=" * 70)
        if use_fixed_lot:
            header = f"  BALANCE SIMULATION (start: ${starting_balance:,.2f}, fixed lot: {fixed_lot}"
        else:
            header = f"  BALANCE SIMULATION (start: ${starting_balance:,.2f}, risk: {risk_pct}%/trade"
        if leverage > 0:
            header += f", leverage: 1:{leverage}"
        header += ")"
        print(header)
        print("-" * 70)

        balance = starting_balance
        peak_balance = starting_balance
        max_dd_dollar = 0.0
        max_dd_pct_bal = 0.0

        if show_leverage:
            print(f"  {'#':>3} {'Date':<16} {'Dir':<5} {'Lots':>7} {'Margin':>9}"
                  f" {'Risk($)':>9} {'PnL($)':>10} {'Balance':>12} {'DD%':>6}")
        else:
            print(f"  {'#':>3} {'Date':<18} {'Dir':<5} {'Risk($)':>9} {'PnL($)':>10}"
                  f" {'Balance':>12} {'DD%':>7}")

        for i, r in enumerate(results):
            sl_dist = abs(r["poi_top"] - r["poi_bottom"])

            if use_fixed_lot:
                lot_size = fixed_lot
                risk_amount = lot_size * sl_dist * contract_size if sl_dist > 0 else 0
            else:
                risk_amount = balance * (risk_pct / 100.0)
                if sl_dist > 0:
                    lot_size = risk_amount / (sl_dist * contract_size)
                else:
                    lot_size = 0.0

            # Enforce minimum lot (broker requirement)
            lot_size_actual = max(lot_size, min_lot)
            if lot_size_actual != lot_size:
                # Recalculate actual risk based on rounded lot
                risk_amount = lot_size_actual * sl_dist * contract_size if sl_dist > 0 else 0

            pnl_dollar = r["pnl_r"] * risk_amount
            balance += pnl_dollar

            if balance > peak_balance:
                peak_balance = balance
            dd_dollar = peak_balance - balance
            dd_pct = (dd_dollar / peak_balance * 100) if peak_balance > 0 else 0
            if dd_dollar > max_dd_dollar:
                max_dd_dollar = dd_dollar
                max_dd_pct_bal = dd_pct

            d = "BUY" if r["direction"] == Direction.BULLISH else "SELL"
            sign = "+" if pnl_dollar >= 0 else ""

            if show_leverage:
                position_value = lot_size_actual * r["entry_price"] * contract_size
                margin_req = position_value / leverage if leverage > 0 else position_value
                if lot_size_actual >= 0.1:
                    lot_str = f"{lot_size_actual:>7.2f}"
                else:
                    lot_str = f"{lot_size_actual:>7.3f}"
                print(
                    f"  {i+1:>3} {r['datetime']:<16} {d:<5}{lot_str}"
                    f" ${margin_req:>8,.2f} ${risk_amount:>8,.2f}"
                    f" {sign}${pnl_dollar:>8,.2f} ${balance:>11,.2f} {dd_pct:>5.1f}%"
                )
            else:
                print(
                    f"  {i+1:>3} {r['datetime']:<18} {d:<5} "
                    f"${risk_amount:>8,.2f} {sign}${pnl_dollar:>8,.2f}"
                    f" ${balance:>11,.2f} {dd_pct:>6.1f}%"
                )

        total_return = ((balance - starting_balance) / starting_balance) * 100
        print("-" * 70)
        print(f"  Starting Balance  : ${starting_balance:>12,.2f}")
        print(f"  Final Balance     : ${balance:>12,.2f}")
        net_pnl = balance - starting_balance
        print(f"  Net P&L           : {'+'if net_pnl >= 0 else ''}${net_pnl:>11,.2f}")
        print(f"  Total Return      : {'+' if total_return >= 0 else ''}{total_return:.2f}%")
        print(f"  Peak Balance      : ${peak_balance:>12,.2f}")
        print(f"  Max Drawdown      : ${max_dd_dollar:>12,.2f} ({max_dd_pct_bal:.1f}%)")

    print("=" * 70)

    # Individual signals
    print("  SIGNAL LOG:")
    print(f"  {'#':>3} {'Date':<18} {'Dir':<5} {'Type':<4} {'P(win)':<8} {'PnL(R)':>8} {'Result':<8}")
    for i, r in enumerate(results):
        d = "BUY" if r["direction"] == Direction.BULLISH else "SELL"
        print(
            f"  {i+1:>3} {r['datetime']:<18} {d:<5} {r['poi_type']:<4} "
            f"{r['probability']:<8.2%} {r['pnl_r']:>+7.2f}R {r['outcome_1to2']:<8}"
        )
    print("=" * 70)


def export_signals_json(results, config: Config, symbol: str, filename: str = None):
    """Export signals to JSON for MT5 Strategy Tester replay.

    JSON format matches Indicator5_EA.mq5 signal parsing.
    """
    import json
    import os

    if not results:
        print("No signals to export.")
        return

    if filename is None:
        filename = f"backtest_signals_{symbol}.json"

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
    for r in results:
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
            "is_oos": 1,  # For backtest, all signals are "executable" (is_oos=1)
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
        "signals": signals_list,
    }

    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

    print(f"\n[JSON EXPORT] {len(results)} signals exported to:")
    print(f"  {filepath}")
    print(f"  Ready for MT5 Strategy Tester replay.")

    # Copy to Strategy Tester sandbox folder for Replay EA to find
    _copy_to_tester_sandbox(filepath, filename)




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


def main():
    parser = argparse.ArgumentParser(description="Indicator5 Backtest")
    parser.add_argument("--symbol", default="EURUSDm")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--bars", type=int, default=2000)
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

    rates = load_historical_data(args.symbol, args.timeframe, args.bars)

    results = run_backtest(rates, config, warmup=args.warmup,
                           eval_bars=args.eval_bars, slippage=args.slippage,
                           max_wait=args.max_wait)

    print_results(results, config,
                  starting_balance=args.balance, risk_pct=args.risk_pct,
                  leverage=args.leverage, fixed_lot=args.lot, min_lot=args.min_lot)

    # Export to JSON for MT5 Strategy Tester if requested
    if args.export_json:
        export_signals_json(results, config, args.symbol)


if __name__ == "__main__":
    main()

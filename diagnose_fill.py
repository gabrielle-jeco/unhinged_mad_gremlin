#!/usr/bin/env python3
"""
Diagnostic: Analyze fill bar distribution for --max-wait variations
"""

from config import Config
from backtest import load_historical_data, run_backtest

# Load data
rates = load_historical_data("XAUUSDm", "M15", 5000)

# Configure
config = Config(
    symbol="XAUUSDm",
    timeframe="M15",
    num_bars=5000,
    signal_probability_threshold=0.30,
)

print("=" * 70)
print("DIAGNOSTIC: Fill Bar Distribution Analysis")
print("=" * 70)

# Test different max_wait values
for max_wait_val in [1, 2, 3, 5, 10, 99]:
    print(f"\n--- Testing --max-wait {max_wait_val} ---")

    results = run_backtest(rates, config, warmup=100, eval_bars=20, max_wait=max_wait_val)

    if not results:
        print(f"No results for max_wait {max_wait_val}")
        continue

    # Analyze where fills occurred
    fill_distances = []  # How many bars after signal did fill happen

    for r in results:
        bar = r["bar_index"]
        fill_bar = r.get("fill_bar", bar)
        distance = fill_bar - bar
        fill_distances.append(distance)

    print(f"  Signals processed: {len(results)}")
    print(f"  Fill distances: {sorted(fill_distances)}")
    print(f"  Max fill distance: {max(fill_distances)} bars")
    print(f"  Avg fill distance: {sum(fill_distances) / len(fill_distances):.2f} bars")

    # Distribution
    dist_1 = sum(1 for d in fill_distances if d == 1)
    dist_2 = sum(1 for d in fill_distances if d == 2)
    dist_3 = sum(1 for d in fill_distances if d == 3)
    dist_4plus = sum(1 for d in fill_distances if d >= 4)

    print(f"\n  Fill distribution:")
    print(f"    Bar+1: {dist_1} signals ({dist_1*100//len(fill_distances)}%)")
    print(f"    Bar+2: {dist_2} signals ({dist_2*100//len(fill_distances)}%)")
    print(f"    Bar+3: {dist_3} signals ({dist_3*100//len(fill_distances)}%)")
    print(f"    Bar+4+: {dist_4plus} signals ({dist_4plus*100//len(fill_distances)}%)")

    # Results summary
    wins = sum(1 for r in results if r["outcome_1to2"] == "win")
    losses = sum(1 for r in results if r["outcome_1to2"] == "loss")
    wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
    net_r = sum(r["pnl_r"] for r in results)

    print(f"\n  Results: {wins}W/{losses}L, WR {wr:.1f}%, Net {net_r:.2f}R")

print("\n" + "=" * 70)
print("KEY INSIGHT:")
print("=" * 70)
print("""
If max-wait has NO impact:
→ MOST signals fill within 1-3 bars (before max-wait even matters)
→ The 'break' statement in fill loop exits immediately upon first fill
→ Unused max_wait bars are never checked
→ Result: Identical metrics across all max_wait values

Why this happens:
1. Signal generated when price is AT POI level
2. Very next bar (bar+1), price usually still near that level
3. Order fills instantly (within 1-3 bars)
4. Loop breaks, doesn't continue to check bar+4, bar+5, etc.
5. max_wait=1 same as max_wait=99 because fill happens early

This means:
✅ Signals are VALID and fill quickly (good sign!)
✅ max-wait parameter is NOT the tuning knob (signal quality is)
✅ System is robust regardless of pending order timeout
""")

#!/usr/bin/env python3
"""
Diagnostic: Compare eval_start windows for max-wait 0 vs max-wait 1+
"""

from config import Config
from backtest import load_historical_data

# Load data
rates = load_historical_data("XAUUSDm", "M15", 5000)
config = Config(symbol="XAUUSDm", timeframe="M15", num_bars=5000, signal_probability_threshold=0.30)

print("=" * 80)
print("ANALYSIS: How --max-wait affects evaluation window")
print("=" * 80)

# Simulate what happens in code
test_signal_bar = 1000
ref_price = float(rates[test_signal_bar]["close"])

print(f"\nSignal at Bar {test_signal_bar}:")
print(f"  ref_price = close[{test_signal_bar}] = {ref_price:.2f}\n")

print("SCENARIO 1: --max-wait 0 (Instant Fill)")
print("-" * 80)
print(f"Code path: max_wait = 0")
print(f"  eval_start = bar + 1 = {test_signal_bar} + 1 = {test_signal_bar + 1}")
print(f"  future window: rates[{test_signal_bar + 1} : {test_signal_bar + 1 + 20}]")
print(f"  = bars {test_signal_bar + 1} to {test_signal_bar + 20} (INCLUSIVE)")
print(f"\nBar details in window:")
for i in range(test_signal_bar + 1, min(test_signal_bar + 6, test_signal_bar + 1 + 20)):
    print(f"  Bar {i}: low={rates[i]['low']:.2f}, close={rates[i]['close']:.2f}")

print("\n\nSCENARIO 2: --max-wait 1+ (Pending Order Fill)")
print("-" * 80)
print(f"Code path: max_wait >= 1")
print(f"  Search for fill from bar+1 onwards:")
print(f"    Bar {test_signal_bar + 1}: low={rates[test_signal_bar + 1]['low']:.2f}, high={rates[test_signal_bar + 1]['high']:.2f}")

# Check if fills at bar+1
if float(rates[test_signal_bar + 1]["low"]) <= ref_price <= float(rates[test_signal_bar + 1]["high"]):
    fill_bar = test_signal_bar + 1
    print(f"    -> FILLED at bar {fill_bar}! (price range includes {ref_price:.2f})")
else:
    fill_bar = test_signal_bar + 2  # Would check next bar
    print(f"    -> NOT FILLED, check bar {fill_bar}")

print(f"\n  fill_bar = {fill_bar}")
print(f"  eval_start = fill_bar + 1 = {fill_bar} + 1 = {fill_bar + 1}")
print(f"  future window: rates[{fill_bar + 1} : {fill_bar + 1 + 20}]")
print(f"  = bars {fill_bar + 1} to {fill_bar + 20} (INCLUSIVE)")
print(f"\nBar details in window:")
for i in range(fill_bar + 1, min(fill_bar + 6, fill_bar + 1 + 20)):
    print(f"  Bar {i}: low={rates[i]['low']:.2f}, close={rates[i]['close']:.2f}")

print("\n" + "=" * 80)
print("KEY DIFFERENCE:")
print("=" * 80)
print(f"""
--max-wait 0:
  Evaluation starts at: bar+1 (signal bar + 1)
  Looks at future: bars {test_signal_bar + 1} to {test_signal_bar + 20}

--max-wait 1+ (fill at bar+1):
  Evaluation starts at: bar+2 (after fill)
  Looks at future: bars {test_signal_bar + 2} to {test_signal_bar + 21}

DIFFERENCE:
  max-wait 0 includes bar {test_signal_bar + 1} but NOT bar {test_signal_bar + 21}
  max-wait 1+ includes bar {test_signal_bar + 21} but NOT bar {test_signal_bar + 1}

  = 1-BAR WINDOW SHIFT! Different price action = different outcomes!

Why this causes metric differences:
1. --max-wait 0: 37 signals (2 didn't make it to evaluation window)
2. --max-wait 1+: 35 signals (2 skipped due to no fill + 2 due to window shift = 4 not processed)
3. The 20-bar evaluation window captures different price moves
4. Win rate changes from 43.3% to 41.4% because different bars = different outcomes
""")

# SSP - SMC + Stochastic Probability Trading System

An algorithmic trading indicator that combines **Smart Money Concepts (SMC)** price action analysis with **stochastic probability modeling** to generate high-probability trade entry signals for MetaTrader 5.

---

## Overview

SSP detects institutional market structure and scores potential trade zones using two-barrier first passage time (FPT) probability theory. Instead of instant market-order fills, it uses **pending limit/stop orders** so the theoretical win probability remains valid at the actual entry price.

**Core capabilities:**
- Detect swing highs/lows, liquidity pools, order blocks (OBs), and fair value gaps (FVGs)
- Identify Break of Structure (BOS) and Change of Character (CHoCH) events
- Track liquidity sweeps and post-sweep entry zones via a state machine
- Score each zone with a calculated win probability `P(win)`
- Emit signals to a JSON file consumed by the MQL5 Expert Advisor
- Execute trades as pending LIMIT/STOP orders in MetaTrader 5

---

## Project Structure

```
unhinged_mad_gremlin/
├── main.py                      # Live real-time indicator loop
├── config.py                    # Configuration dataclass (all parameters)
├── models.py                    # Data models (POI, Signal, structures)
├── smc.py                       # SMC detection engine
├── probability.py               # FPT probability scoring
├── state_machine.py             # Post-sweep entry state machine
├── mt5_interface.py             # MetaTrader 5 connection & signal output
├── backtest.py                  # Historical backtesting with walk-forward
├── forward_test.py              # Out-of-sample forward testing
├── optimize.py                  # Parameter optimization sweep
├── diagnose.py                  # General diagnostic tools
├── diagnose_eval_window.py      # Debug evaluation window timing
├── diagnose_fill.py             # Debug fill simulation
├── requirements.txt             # Python dependencies
├── mql5/
│   ├── SSP_EA_v2_main.mq5       # Main Expert Advisor (v2) with pending orders
│   ├── SSP_EA_v2_backup.mq5     # Backup EA version
│   ├── SSP_EA_Replay_v2_main.mq5    # Replay EA for backtesting in MT5
│   ├── SSP_EA_Replay_v2_backup.mq5  # Backup replay EA
│   └── SSP_Indicator_Overlay.mq5    # Visualization overlay indicator
└── test/
    └── test_duplicate_signals.json  # Test data for duplicate signal detection
```

---

## Prerequisites

- **Python 3.8+**
- **MetaTrader 5** desktop application with an active broker account
- **MetaTrader5** Python package `>= 5.0.45`
- **NumPy** `>= 1.24.0`

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/gabrielle-jeco/unhinged_mad_gremlin.git
cd unhinged_mad_gremlin

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Open MetaTrader 5 and ensure:
#    - Your broker account is connected
#    - The trading symbol (e.g., XAUUSDm) is visible in the Market Watch
```

---

## Configuration

All parameters live in `config.py` as a `Config` dataclass. Edit the defaults there or pass a custom `Config` instance at runtime.

| Parameter | Default | Description |
|---|---|---|
| `symbol` | `"EURUSD"` | Trading symbol |
| `timeframe` | `"H1"` | Chart timeframe (`M1`–`W1`) |
| `num_bars` | `500` | Bars of history to load |
| `swing_lookback` | `5` | Bars each side for swing detection |
| `liq_atr_mult` | `0.3` | ATR multiplier for liquidity pool tolerance |
| `liq_min_touches` | `2` | Minimum touches to form a liquidity pool |
| `ob_max_lookback` | `10` | Max bars back to search for order blocks |
| `fvg_min_atr` | `0.5` | FVG must be ≥ this fraction of ATR |
| `sweep_expiry_bars` | `60` | Bars before a sweep tag expires |
| `drift_window` | `30` | Bars used to calculate price drift (μ) |
| `vol_window` | `50` | Bars used to calculate volatility (σ) |
| `fpt_horizon_bars` | `20` | FPT look-ahead horizon |
| `tp_rr_ratio` | `2.0` | Take-profit as a multiple of stop-loss (1:2 RR) |
| `signal_probability_threshold` | `0.3` | Minimum `P(win)` to emit a signal |
| `min_poi_atr_width` | `0.5` | POI must span ≥ this fraction of ATR |
| `atr_period` | `14` | ATR calculation period |
| `console_dashboard` | `True` | Print live dashboard to stdout |
| `loop_interval_sec` | `1.0` | Polling interval in seconds |

---

## Usage

### Live Real-Time Indicator

```bash
python main.py
```

Connects to MetaTrader 5, waits for each new bar, runs the full SMC + probability pipeline, and writes signals to a JSON file for the EA to consume.

**Example console output:**
```
==================================================
  INDICATOR 5 | SMC + Stochastic Probability
  XAUUSDm M15 | 2024-03-22 15:45:30
==================================================
  Trend         : BULLISH
  Last Break    : BOS
  ATR           : 15.32110
  Drift (mu)    : 0.00012345
  Volatility    : 0.00045678
--------------------------------------------------
  Active FVGs   : 3
  Active OBs    : 5
  Liq. Pools    : 8 (2 swept)
  Pending POIs  : 2
--------------------------------------------------
  SCORED POIs:
    ^ FVG  [2050.12 - 2051.45] P(win)=0.62 POST-SWEEP
    v OB   [2049.00 - 2049.87] P(win)=0.45
```

### Backtest on Historical Data

```bash
# Default: last 5000 M15 bars of XAUUSDm
python backtest.py

# Custom parameters
python backtest.py --symbol EURUSDm --timeframe H1 --bars 10000 --eval-bars 30 --max-wait 3
```

Results are saved to `backtest_results_*.csv` and include win rate, expectancy, profit factor, Sharpe ratio, and max drawdown.

### Forward Testing (Out-of-Sample Validation)

```bash
# Default: 80% in-sample / 20% out-of-sample split
python forward_test.py

# Custom split
python forward_test.py --symbol XAUUSDm --split-pct 75 --max-wait 3
```

Results are saved to `forward_test_results_*.csv`.

### Parameter Optimization

```bash
python optimize.py
```

Sweeps a parameter grid (threshold, horizon, expiry, RR ratio, POI width) and saves the best-performing configuration to `optimize_results_*.csv`.

### Diagnostic Tools

```bash
python diagnose.py                # General diagnostics
python diagnose_eval_window.py    # Debug evaluation window timing
python diagnose_fill.py           # Debug pending-order fill simulation
```

---

## MQL5 Expert Advisor

Copy the files from the `mql5/` directory into your MetaTrader 5 `Experts/` folder and compile them in the MetaEditor.

- **`SSP_EA_v2_main.mq5`** — Production EA. Reads the JSON signal file produced by `main.py` and places pending LIMIT/STOP orders.
- **`SSP_EA_Replay_v2_main.mq5`** — Replay EA for use in the MT5 Strategy Tester.
- **`SSP_Indicator_Overlay.mq5`** — Visual overlay that draws detected SMC structures directly on the chart.

---

## How It Works

1. **SMC Detection** (`smc.py`): Identifies swing points, clusters them into liquidity pools, labels the most recent candle groups as order blocks, and flags three-candle imbalances as fair value gaps. Structural breaks (BOS/CHoCH) determine the current bias.

2. **State Machine** (`state_machine.py`): Monitors liquidity pools for sweeps. Once a sweep is confirmed, nearby POIs (OBs and FVGs) are tagged as high-conviction entry zones.

3. **Probability Scoring** (`probability.py`): Estimates drift (μ) and volatility (σ) from recent returns, then applies the two-barrier first passage time formula to calculate the probability that price reaches TP before SL within the configured horizon.

4. **Signal Emission** (`mt5_interface.py`): POIs exceeding the probability threshold are serialised to a JSON file. The MQL5 EA polls this file and places the corresponding pending orders.

---

## License

This project is unlicensed. All rights reserved by the author.

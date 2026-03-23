# Critical Bugs Found: Python vs MT5 Discrepancy

## Summary
Found **4 critical issues** causing P&L discrepancy between Python and MT5:
- Bugs #1-3: SL Distance Calculation → **FIXED** (2026-03-23)
- Bug #4: Contract Size Mismatch → **NEEDS VERIFICATION**

---

## Bug #1: SL Distance - Forward Test Evaluation ✅ FIXED

**File:** `forward_test.py` | **Lines:** 171-177
**Status:** ✅ FIXED

```python
# Fixed code:
if is_buy:
    sl_distance = entry - sig["poi_bottom"]
else:
    sl_distance = sig["poi_top"] - entry
sl_distance = abs(sl_distance) if sl_distance > 0 else 0.01
tp_distance = sl_distance * config.tp_rr_ratio
```

---

## Bug #2: SL Distance - Backtest Evaluation ✅ FIXED

**File:** `backtest.py` | **Lines:** 241-248
**Status:** ✅ FIXED

Same fix applied - entry-to-POI.edge instead of POI height.

---

## Bug #3: SL Distance - Balance Simulation ✅ FIXED

**File:** `backtest.py` | **Lines:** 472-478
**Status:** ✅ FIXED

```python
# Fixed code in balance simulation:
if r["direction"] == Direction.BULLISH:
    sl_dist = r["entry_price"] - r["poi_bottom"]
else:
    sl_dist = r["poi_top"] - r["entry_price"]
sl_dist = abs(sl_dist) if sl_dist > 0 else 0.01
```

---

## Bug #4: Contract Size Mismatch ⚠️ NEEDS VERIFICATION

**File:** `backtest.py` | **Line:** 438
**Severity:** CRITICAL - Causes 10x P&L discrepancy
**Status:** ⚠️ UNDER INVESTIGATION

### Problem
Python hardcodes `contract_size = 100` for XAU, but MT5 actual P&L shows **10x higher**.

### Evidence from MT5 Log
```
Entry: 3998.936
Exit (SL): 3991.354
Move: 7.582 points
Lot: 0.01
EA calculated risk: $7.54
ACTUAL LOSS: $75.82 (10x more!)
```

### Reverse Calculation
```
PnL = lots × move × contract_size
$75.82 = 0.01 × 7.582 × contract_size
contract_size = 75.82 / 0.07582 = 1000 oz (not 100 oz!)
```

### Root Cause (Suspected)
Either:
1. **Exness XAUUSDm uses 1000 oz per lot** (non-standard contract)
2. **MT5 tick_value misreported**: Shows 0.10 but actual is 1.00

### Current Code
```python
# backtest.py line 438
if "XAU" in sym or "GOLD" in sym:
    contract_size = 100        # 1 lot = 100 oz ← POSSIBLY WRONG
```

### Proposed Fix
```python
# Option A: Hardcode for Exness XAUUSDm
if "XAU" in sym or "GOLD" in sym:
    contract_size = 1000       # 1 lot = 1000 oz (Exness XAUUSDm)

# Option B: Make configurable
parser.add_argument("--contract-size", type=int, default=0,
                    help="Override contract size (0=auto-detect)")
```

### Verification Needed
Run in MT5 terminal to check actual contract spec:
```cpp
PrintFormat("CONTRACT_SIZE = %.2f", SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE));
PrintFormat("TICK_VALUE = %.5f", SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE));
PrintFormat("TICK_SIZE = %.5f", SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE));
```

---

## P&L Calculation Comparison

| Component | Python (current) | MT5 Actual |
|-----------|------------------|------------|
| contract_size | 100 oz | 1000 oz (?) |
| SL distance | 7.535 pts | 7.535 pts |
| Risk @ 0.01 lot | $7.54 | $75.35 |
| 10x ratio | ✓ Matches! | |

---

## Status Summary

| Bug | File | Issue | Status |
|-----|------|-------|--------|
| #1 | forward_test.py | SL distance eval | ✅ FIXED |
| #2 | backtest.py | SL distance eval | ✅ FIXED |
| #3 | backtest.py | SL distance balance sim | ✅ FIXED |
| #4 | backtest.py | Contract size | ⚠️ VERIFY |

---

## Next Steps
1. ✅ Re-export JSON with fixed SL distance
2. ⚠️ Verify Exness XAUUSDm contract size via MT5 terminal
3. ⚠️ Update Python contract_size if 1000 oz confirmed
4. Re-run MT5 Strategy Tester to validate fixes


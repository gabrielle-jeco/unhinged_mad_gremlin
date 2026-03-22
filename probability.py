import math
import numpy as np
from models import POI, Direction, POIType
from config import Config


# ---------------------------------------------------------------------------
# Normal CDF (no scipy dependency)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Standard normal CDF using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Drift & Volatility estimation
# ---------------------------------------------------------------------------

def estimate_drift(closes: np.ndarray, window: int) -> float:
    """Rolling mean of log returns over the last `window` bars.
    Positive = uptrend, negative = downtrend."""
    if len(closes) < window + 1:
        return 0.0
    log_ret = np.diff(np.log(closes[-(window + 1):]))
    return float(np.mean(log_ret))


def estimate_volatility(closes: np.ndarray, window: int) -> float:
    """Realized volatility: std of log returns over `window` bars."""
    if len(closes) < window + 1:
        return 1e-10
    log_ret = np.diff(np.log(closes[-(window + 1):]))
    vol = float(np.std(log_ret, ddof=1))
    return max(vol, 1e-10)


# ---------------------------------------------------------------------------
# Two-Barrier Exit Probability (replaces single-barrier FPT + Bayesian)
# ---------------------------------------------------------------------------

def two_barrier_win_probability(
    mu: float, sigma: float,
    sl_distance: float, tp_distance: float,
) -> float:
    """Probability of hitting TP before SL for Brownian motion with drift.

    Exact formula derived from scale function of BM with drift:
        X(t) = mu*t + sigma*W(t), starting at X(0) = 0
        SL at -L (below start), TP at +G (above start)

    For drift mu != 0:
        P(win) = [exp(2*mu*L/s2) - 1] / [exp(2*mu*L/s2) - exp(-2*mu*G/s2)]

    For drift mu -> 0 (random walk):
        P(win) = L / (L + G)

    Where:
        L = SL distance in log-price space (always positive)
        G = TP distance in log-price space (always positive)
        mu = drift per bar toward TP (positive = favorable)
        sigma = volatility per bar (log returns)

    Args:
        mu: drift per bar TOWARD TP (positive = trend-aligned, favorable)
        sigma: volatility per bar (from estimate_volatility)
        sl_distance: SL distance in log-price space (positive)
        tp_distance: TP distance in log-price space (positive)

    Returns:
        P(win) in [0, 1] — probability that TP is hit before SL
    """
    if sigma <= 1e-10:
        return 0.0

    L = abs(sl_distance)
    G = abs(tp_distance)

    if L < 1e-12 or G < 1e-12:
        return 0.0

    # Random walk case: mu effectively zero
    if abs(mu) < 1e-12:
        return L / (L + G)

    # General case with drift
    s2 = sigma ** 2

    exp_sl = 2.0 * mu * L / s2   # exponent for SL term
    exp_tp = -2.0 * mu * G / s2  # exponent for TP term

    # Clamp to prevent overflow
    exp_sl = max(-500.0, min(500.0, exp_sl))
    exp_tp = max(-500.0, min(500.0, exp_tp))

    numerator = math.exp(exp_sl) - 1.0
    denominator = math.exp(exp_sl) - math.exp(exp_tp)

    if abs(denominator) < 1e-15:
        return L / (L + G)  # fallback to random walk

    p_win = numerator / denominator
    return max(0.0, min(1.0, p_win))


# ---------------------------------------------------------------------------
# Master scoring function
# ---------------------------------------------------------------------------

def score_poi(
    poi: POI, closes: np.ndarray, trend_direction: str,
    atr_current: float, config: Config,
    entry_price: float,
) -> POI:
    """Score a single POI: estimate drift/vol, compute P(win) via two-barrier.

    SL = distance from actual entry_price to POI invalidation edge
    TP = SL * tp_rr_ratio (default 2.0 for 1:2 RR)

    Args:
        entry_price: Actual entry price (current_close from retrace candle)

    Drift handling:
        Counter-trend POIs (e.g., bullish POI in bearish drift) get mu=0
        because a sweep + CHoCH signals a reversal — historical drift is
        becoming invalid. Using mu=0 (random walk) is intellectually honest:
        at a reversal point, future drift is unknown.
    """
    if entry_price <= 0:
        poi.posterior = 0.0
        return poi

    mu = estimate_drift(closes, config.drift_window)
    sigma = estimate_volatility(closes, config.vol_window)

    # Convert drift to "toward TP" convention:
    # estimate_drift returns positive = uptrend, negative = downtrend
    # For bullish trade: TP is above → positive mu is favorable (keep sign)
    # For bearish trade: TP is below → negative mu is favorable (negate sign)
    if poi.direction == Direction.BEARISH:
        mu = -mu

    # Counter-trend handling: if mu < 0 after sign adjustment, it means
    # drift pushes AWAY from TP (toward SL). But sweep + CHoCH signals
    # a reversal, so historical drift is becoming invalid. Set mu=0
    # (random walk) — intellectually honest at a reversal point.
    if mu < 0:
        mu = 0.0

    # SL and TP distances in log-price space
    # Using ACTUAL entry_price from retrace candle (not POI edge)
    poi_height = abs(poi.top - poi.bottom)
    if poi_height <= 0 or entry_price <= 0:
        poi.posterior = 0.0
        return poi

    # SL = distance from actual entry to invalidation edge (log space)
    # For bullish: SL at poi.bottom (zone break) → sl = log(entry/bottom)
    # For bearish: SL at poi.top (zone break) → sl = log(top/entry)
    if poi.direction == Direction.BULLISH:
        sl_price = poi.bottom
        sl_log = math.log(entry_price / sl_price)
    else:
        sl_price = poi.top
        sl_log = math.log(sl_price / entry_price)

    tp_log = sl_log * config.tp_rr_ratio

    if sl_log < 1e-12:
        poi.posterior = 0.0
        return poi

    p_win = two_barrier_win_probability(mu, sigma, sl_log, tp_log)

    # Store as posterior for backward compat with state_machine.py
    poi.posterior = p_win
    poi.fpt_probability = p_win
    poi.prior = 0.0  # no more Bayesian prior
    poi.hold_probability = p_win
    poi.break_probability = 1.0 - p_win

    return poi

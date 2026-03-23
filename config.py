from dataclasses import dataclass


@dataclass
class Config:
    # -- MT5 Connection --
    symbol: str = "EURUSD"
    timeframe: str = "H1"
    num_bars: int = 500

    # -- SMC: Swing Points --
    swing_lookback: int = 5

    # -- SMC: Liquidity Pools --
    liq_atr_mult: float = 0.3
    liq_min_touches: int = 2

    # -- SMC: Order Blocks --
    ob_max_lookback: int = 10
    max_active_obs: int = 8

    # -- SMC: Fair Value Gaps --
    fvg_min_atr: float = 0.5
    max_active_fvgs: int = 10

    # -- State Machine --
    sweep_expiry_bars: int = 25

    # -- Probability: Drift & Volatility --
    drift_window: int = 30
    vol_window: int = 50
    fpt_horizon_bars: int = 10

    # -- Probability: Two-Barrier --
    tp_rr_ratio: float = 2.0                     # TP = SL * ratio (1:2 RR)

    # -- Signal Threshold --
    signal_probability_threshold: float = 0.3   # low - let SMC structure do the filtering
    min_poi_atr_width: float = 0.5               # POI must be >= 0.5 * ATR — key filter

    # -- ATR --
    atr_period: int = 14

    # -- Output --
    signal_file_path: str = ""
    console_dashboard: bool = True
    loop_interval_sec: float = 1.0

    @staticmethod
    def timeframe_to_mt5(tf_str: str):
        import MetaTrader5 as mt5
        mapping = {
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1,
        }
        return mapping.get(tf_str, mt5.TIMEFRAME_H1)

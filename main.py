import time
import signal
import sys

from config import Config
from mt5_interface import MT5Interface
from smc import run_smc_detection
from probability import score_poi, estimate_drift, estimate_volatility
from state_machine import EntryStateMachine


def main():
    config = Config(
        symbol="XAUUSDm",
        timeframe="M15",
        signal_probability_threshold=0.3,
    )

    interface = MT5Interface(config)
    if not interface.connect():
        sys.exit(1)

    sm = EntryStateMachine(config)
    last_bar_time = 0

    running = True

    def on_sigint(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, on_sigint)

    print(f"\nStarting indicator5 on {config.symbol} {config.timeframe}")
    print(f"Probability threshold: {config.signal_probability_threshold:.0%}")
    print("Waiting for new bars...\n")

    try:
        while running:
            # 1. Wait for a new bar
            current_bar_time = interface.get_current_bar_time()
            if current_bar_time == last_bar_time:
                time.sleep(config.loop_interval_sec)
                continue
            last_bar_time = current_bar_time

            # 2. Fetch OHLC data
            rates = interface.get_rates()
            if rates is None:
                time.sleep(config.loop_interval_sec)
                continue

            # 3. Layer 1: SMC detection
            smc_results = run_smc_detection(rates, config)

            # 4. State machine: orchestrate Layer 1 → Layer 2 → signals
            new_signals = sm.process(smc_results, rates, score_poi)

            # 5. Write signal file for MQL5 overlay
            interface.write_signals(
                smc_results, new_signals,
                sm.state.active_pois,
                smc_results["trend_direction"],
            )

            # 6. Console dashboard
            if config.console_dashboard:
                closes = rates["close"].astype(float)
                mu = estimate_drift(closes, config.drift_window)
                sigma = estimate_volatility(closes, config.vol_window)
                interface.print_dashboard(
                    smc_results, new_signals,
                    sm.state.active_pois, mu, sigma,
                )

            time.sleep(config.loop_interval_sec)

    except Exception as e:
        print(f"\nError: {e}")
        raise
    finally:
        interface.disconnect()
        print("\nShutdown complete.")


if __name__ == "__main__":
    main()

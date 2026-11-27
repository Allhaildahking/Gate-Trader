# Gate-Trader

Gate.io USDT perpetual futures research bot. **Default mode is dry-run. Real-money trading is not recommended.**

## Current components

- `gate_futures_bot.py`: existing trend-breakout bot and per-symbol historical check.
- `portfolio_backtest.py`: shared-account portfolio simulator. Enforces a single balance, maximum concurrent positions, margin cap, cooldown, drawdown guard, entry/exit fees and configurable slippage. If stop and target are both touched in one 15m candle, it assumes the stop happened first. Open trades are marked to market at the end rather than silently ignored.
- `tests/test_portfolio_backtest.py`: basic deterministic tests.

## Run

```bash
python gate_futures_bot.py backtest
python portfolio_backtest.py --days 20 --pairs 59 --balance 1000
python -m unittest discover -s tests -v
```

The portfolio runner currently caps the lookback at 20 days because it uses the current one-request candle downloader and Gate's 2,000-candle request limit. It does **not** yet claim 60–90 day coverage. A paginated historical downloader is required before extending the lookback.

## Important limitations

- Historical results are research, not a promise of future returns.
- The simulator is OHLC-bar based; it cannot know the exact intrabar path. It uses conservative stop-first ordering when both stop and target are touched.
- Funding payments, exchange-specific liquidation mechanics, tick/step rounding, and market impact beyond the configured slippage estimate are not modelled yet.
- The live bot's local stop monitoring is not equivalent to exchange-hosted protection. Keep `DRY_RUN=true`; do not set live mode until exchange-side bracket placement, fill reconciliation, restart recovery and protective-order cleanup have been tested on Gate's test environment or with tiny non-production exposure.
- Never commit API keys. Use a restricted API key with withdrawals disabled.

## Strategy

The current strategy is a 1h EMA trend filter plus a 15m 20-candle breakout with volume and RSI filters. Default stop is 1.5 ATR and target 3 ATR. Backtests must be evaluated on unseen periods and across multiple market regimes before any deployment decision.

# Polymarket BTC 5m trade price distribution

This script pulls the last N days of Polymarket BTC 5-minute Up/Down trades and plots the trade-price probability distribution.

## Install

```bash
pip install requests pandas numpy matplotlib tqdm
```

## Run

```bash
python polymarket_btc5m_trade_distribution.py --days 30 --bin-size 0.02 --outdir out_btc5m
```

Output files:

- `out_btc5m/btc5m_trades.csv`: raw trade rows
- `out_btc5m/btc5m_trade_price_distribution.csv`: bin counts and probabilities
- `out_btc5m/btc5m_trade_price_distribution.png`: histogram plot
- `out_btc5m/market_refs.csv`: resolved slugs and condition IDs

## Notes

Default mode counts taker-side fills only (`takerOnly=true`) so each matched trade/fill is counted once.

If you want a unified x-axis of implied Up probability instead of each outcome's own traded price:

```bash
python polymarket_btc5m_trade_distribution.py --days 30 --bin-size 0.02 --canonical-up-prob
```

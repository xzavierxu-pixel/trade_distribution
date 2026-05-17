# Polymarket BTC 5m 7-day winner-only touch strategy

This version pulls the last 7 complete calendar days by default, computes full-range touch probability plots, and optimizes the strategy with:

- Total budget <= `10 USDC`
- `mos = 5`
- `mts = 0.01`
- Strategy price range default: `0.01` to `0.50`
- Plot price range default: `0.01` to `0.99`
- Per-price max notional default: `20%` of total budget, so `2 USDC` when budget is `10`

## Install

```bash
pip install requests pandas numpy matplotlib tqdm
```

## Run

```bash
python polymarket_btc5m_7d_touch_strategy.py \
  --outdir out_btc5m_7d
```

## Important options

Use 30 days:

```bash
python polymarket_btc5m_7d_touch_strategy.py \
  --days 30 \
  --outdir out_btc5m_30d
```

Use a fixed end date in Singapore timezone:

```bash
python polymarket_btc5m_7d_touch_strategy.py \
  --days 7 \
  --end-date 2026-05-13 \
  --timezone Asia/Singapore \
  --outdir out_btc5m_7d_20260513
```

Change per-price cap:

```bash
python polymarket_btc5m_7d_touch_strategy.py \
  --budget 10 \
  --max-notional-per-price-frac 0.20 \
  --outdir out_btc5m_7d
```

Conservative run:

```bash
python polymarket_btc5m_7d_touch_strategy.py \
  --a-haircut 0.7 \
  --min-touched 5 \
  --outdir out_btc5m_7d_conservative
```

## Outputs

- `touch_probability_up_full.png`
- `touch_probability_down_full.png`
- `ev_curve_up_full.png`
- `ev_curve_down_full.png`
- `ladder_up.png`
- `ladder_down.png`
- `selected_chunks_ev_by_k_up.png`
- `selected_chunks_ev_by_k_down.png`
- `touch_probability_curve_full_plot_range.csv`
- `touch_probability_curve_strategy_range.csv`
- `ev_curve_full_plot_range.csv`
- `ev_curve_strategy_range.csv`
- `selected_chunks_up.csv`
- `selected_chunks_down.csv`
- `ladder_up.csv`
- `ladder_down.csv`
- `strategy_summary.txt`

## Core formula

```text
EV_s(p,k) = q_s * a_s(p,k) * mos * (1-p) - (1-q_s) * mos * p
```

where `b=1` is already included in the loss term.

Use `ladder_up.csv` if your model predicts Up.  
Use `ladder_down.csv` if your model predicts Down.

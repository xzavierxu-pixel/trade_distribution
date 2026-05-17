# BTC 5m Trade Distribution Project

This project analyzes Polymarket BTC 5-minute Up/Down markets from a local
`trades_raw.csv`. Data fetching and data analysis are separated.

## Current Data

Primary raw data:

```text
out_btc5m_7d_full_strategy/trades_raw.csv
out_btc5m_7d_full_strategy/market_refs_resolved.csv
```

Do not edit these files. All analysis outputs are reproducible from them.

## Main Scripts

Fetch data only:

```bash
python reach_distribution/fetch_btc5m_data.py --days 7 --outdir data/btc5m_7d
```

Run clean analysis only:

```bash
python reach_distribution/analyze_btc5m_clean.py \
  --trades-csv out_btc5m_7d_full_strategy/trades_raw.csv \
  --refs-csv out_btc5m_7d_full_strategy/market_refs_resolved.csv \
  --outdir analysis_clean/btc5m_7d \
  --max-minute 4 \
  --q 0.60
```

Optional minute distribution heatmaps:

```bash
python reach_distribution/btc5m_minute_price_distribution.py \
  --csv out_btc5m_7d_full_strategy/trades_raw.csv \
  --outdir out_btc5m_7d_full_strategy/minute_price_dist_m0_4_sell \
  --max-minute 4 \
  --side-filter sell
```

## Clean Analysis Outputs

The canonical compact output is:

```text
analysis_clean/btc5m_7d/
  ANALYSIS_REPORT.md
  dataset_quality.json
  minute_summary_by_side.csv
  price_band_realized_roi_by_side.csv
  price_band_minute_realized_roi_by_side.csv
  strategy_ev_q60_passive_buy_sell_fills.csv
  strategy_ev_3d_up.html
  strategy_ev_3d_down.html
  a_win_market_fill_3d_up.html
  a_win_market_fill_3d_down.html
```

Large enriched trade-level CSVs are intentionally not kept because they can be
regenerated and mostly duplicate `trades_raw.csv`.

### File Guide

`ANALYSIS_REPORT.md`

Human-readable summary of the clean analysis. It includes dataset quality,
realized ROI by passive-buy price band, q=0.60 model-conditioned EV candidates,
and the recommended trading policy.

`dataset_quality.json`

Basic audit metadata for the input data and analysis assumptions:

- raw file paths
- `max_minute`
- model `q`
- market counts by final outcome
- total trade rows
- strict duplicate-row count
- side / token outcome / final outcome counts

Use this first to confirm the analysis was run on the expected data.

`minute_summary_by_side.csv`

Minute-level realized PnL summary split by taker side (`buy` / `sell`).
For passive limit-buy research, focus on `side=sell`, because taker sells are
the trades that would hit a passive bid.

Important columns:

- `side`: taker side
- `minute_from_start`: market minute, with `0` as the first minute
- `rows`: number of trade rows
- `size_sum`: total token shares traded
- `cost`: sum of `price * size`
- `payout`: sum of final settlement payout, equal to winning-token shares
- `profit`: `payout - cost`
- `avg_price`: size-weighted average traded price
- `win_rate_size`: size-weighted winner share
- `roi_on_cost`: realized ROI if every row in that group was bought

`price_band_realized_roi_by_side.csv`

Price-band realized ROI summary split by taker side. This is the quickest file
for checking whether a price range was historically profitable.

For the current q=0.60 passive-buy strategy, the key rows are `side=sell`.
In the current 7-day sample, low-price bands below `0.50` are poor, while
`0.60-0.90` has the strongest historical realized ROI.

`price_band_minute_realized_roi_by_side.csv`

Same metrics as `price_band_realized_roi_by_side.csv`, but split by both
`minute_from_start` and `price_band`. Use this to decide when a price band is
useful. For example, a band can be profitable in minute 2-3 but weaker in
minute 4.

`strategy_ev_q60_passive_buy_sell_fills.csv`

Model-conditioned EV table for the passive limit-buy strategy at `q=0.60`.
This uses the conservative `b=1` loss assumption:

```text
EV = q * a_win_market_fill * (1 - price_mid)
   - (1 - q) * price_mid
```

Only the winning side is discounted by observed taker SELL fill probability. If
the model is wrong, the passive bid is assumed to eventually fill and settle to
zero.

Important columns:

- `prediction_side`: model prediction, `up` or `down`
- `minute_from_start`: minute where the fill opportunity appears
- `price_band`: candidate limit-buy price band
- `price_mid`: midpoint of `price_band`, used as the EV limit price
- `q`: assumed directional accuracy
- `a_win_market_fill`: `win_fill_markets / final_markets_for_prediction_side`
- `a_lose_market_fill`: diagnostic only; wrong-side observed taker SELL fill
  probability in this early window, not used in conservative EV
- `win_fill_markets`: count of correct-outcome markets with taker SELL fills in
  this band
- `lose_fill_markets`: count of wrong-outcome markets with taker SELL fills in
  this band
- `ev_per_share_opportunity`: conservative expected value per share opportunity

Sort this file by `ev_per_share_opportunity` descending. Positive rows are
candidate strategies; negative rows should be avoided unless the model accuracy
or live fill assumptions change materially.

`strategy_ev_3d_up.html` and `strategy_ev_3d_down.html`

Interactive Plotly 3D bar charts for conservative EV by minute and price band
midpoint. Open these in a browser to inspect the EV surface for each prediction
direction.

`a_win_market_fill_3d_up.html` and `a_win_market_fill_3d_down.html`

Interactive Plotly 3D bar charts for `a_win_market_fill` by minute and price
band midpoint. These show how often the winning token historically gave a
taker-SELL fill opportunity at each entry zone.

## Strategy Interpretation

For passive limit buys, use taker `SELL` rows to estimate fills. A taker sell is
the observed action that would hit a passive bid.

The clean strategy report uses:

```text
minute_from_start <= 4
side = SELL for fill assumptions
q = 0.60 model directional accuracy
```

Legacy exploratory scripts were moved to:

```text
reach_distribution/legacy/
```

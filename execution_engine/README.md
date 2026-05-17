# execution_engine Deployment

This directory contains the paper/live execution loop for the BTC 5m Polymarket
maker strategy.

## Current Contract

- Strategy: maker limit buy only.
- Timing: submit immediately after a valid model decision.
- Holding: keep the order live until market end.
- Fill table lookup: `decision_time_regime + current_price_bucket`.
- Action: `limit_price_anchor`.
- Default planner: two maker limit buys at `min(best_bid, 0.80)` and
  `min(best_bid, 0.80) - 0.10`, 5 shares each.
- Kelly planner remains available as an online fallback, but
  `kelly_fallback_enabled` defaults to `false`.
- Kelly fallback inputs: Wilson lower bounds `q_market_LCB` and `a_win_LCB`,
  not the raw small-sample rates.
- Removed from runtime lookup: `prediction_side`, `submit_second_anchor`,
  `order_delay_seconds`.
- Default `polymarket.signature_type`: `1`.

The canonical product and strategy document is:

```text
docs/polymarket_btc5m_hold_to_end_prd.md
```

The previous maker fill surface docs are obsolete and should not be used for
runtime behavior.

## Build Artifact

Model files are grouped under `models/early_trade_label_v1/` by generation
stage. The exporter searches the model directory recursively, so the grouped
layout is supported directly.

```powershell
python -m execution_engine.artifact_export `
  --model-dir models\early_trade_label_v1 `
  --deploy-dir execution_engine\deploy `
  --model-version early_trade_label_v1 `
  --maker-fill-table models\early_trade_label_v1\fill_surface\maker_fill_table.csv `
  --bundle-out dist\fortune_bot_early_trade_label_v1.tar.gz
```

## Verify

```powershell
python -m execution_engine.verify_prd `
  --artifact-dir execution_engine\deploy\early_trade_label_v1 `
  --bundle dist\fortune_bot_early_trade_label_v1.tar.gz

python -m execution_engine.self_test `
  --artifact-dir execution_engine\deploy\early_trade_label_v1 `
  --bundle dist\fortune_bot_early_trade_label_v1.tar.gz `
  --config config.example.yaml
```

The verifier checks the Kelly hold-to-end table contract, including `q_market`,
`q_market_LCB`, `a_win_LCB`, `q_required`, `q_margin`, `edge_market_q`,
`f_kelly`, and `is_valid_maker_candidate`. Deploy exports are grouped by
generation stage instead of being flattened into the version directory.

## Deploy

```powershell
.\deploy\version3_deploy.ps1 `
  -Bundle dist\fortune_bot_early_trade_label_v1.tar.gz `
  -HostName version3 `
  -Target "~/fortune_bot"
```

The deploy script verifies the local bundle, stops the service/timer, clears
`~/fortune_bot`, extracts the new bundle, installs dependencies, runs local
server checks, and installs the systemd user timer. It does not create
`~/fortune_bot_backups/fortune_bot_<timestamp>.tar.gz`.

## Live Guard

Live mode still requires:

```text
orders.enabled=true
runtime.mode=live
artifact_manifest.live_eligible=true
required Polymarket credentials present in the environment
polymarket.signature_type=1
polymarket.funder_env=POLYMARKET_FUNDER
```

If `orders.enabled` is false or the artifact is not live eligible, `run_once`
refuses live order submission.

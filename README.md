# BTC 5m Trade Distribution Project

This repository analyzes BTC 5-minute Polymarket Up/Down markets and packages
the `early_trade_label_v1` model for the execution engine.

## Current Execution Strategy

The active execution strategy is defined in:

```text
docs/polymarket_btc5m_hold_to_end_prd.md
```

Runtime behavior:

- model chooses `up` or `down`
- planner reads the predicted token's current price
- planner looks up the merged hold-to-end maker fill table by
  `decision_time_regime + current_price_bucket`
- default planner submits a two-level best-bid ladder:
  `min(best_bid, 0.80)` and `min(best_bid, 0.80) - 0.10`, 5 shares each
- Kelly planner remains available as an explicit fallback, but is disabled by
  default
- orders are submitted immediately and held until market end

The old submit-delay fill surface method is obsolete.

## Model Artifact Layout

`models/early_trade_label_v1/` is grouped by generation stage:

```text
model/          model.pkl, feature columns, feature importance
evaluation/     evaluation and model-selection reports
tuning/         thresholds, tuning candidates, probability references
predictions/    train/validation/holdout predictions
fill_surface/   hold-to-end maker fill table and side diagnostics
features/       generated feature parquet files
diagnostics/    regime and false-slice reports
state/          local run state/log files
```

The exporter searches this directory recursively.

## Build Deployment Bundle

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

Runtime uses the target-token `q_market_LCB` from the fill table by default. A
model lower-bound probability is only used when it clears the configured alpha
margin over `q_market_LCB`.

Deploy artifacts are exported by generation stage under:

```text
execution_engine/deploy/<model_version>/model/
execution_engine/deploy/<model_version>/evaluation/
execution_engine/deploy/<model_version>/tuning/
execution_engine/deploy/<model_version>/predictions/
execution_engine/deploy/<model_version>/features/
execution_engine/deploy/<model_version>/fill_surface/
execution_engine/deploy/<model_version>/diagnostics/
```

## Polymarket Config

`polymarket.signature_type` defaults to:

```yaml
polymarket:
  signature_type: 1
```

Paper mode remains the default and orders are disabled by default.

## Deploy

```powershell
.\deploy\version3_deploy.ps1 `
  -Bundle dist\fortune_bot_early_trade_label_v1.tar.gz `
  -HostName version3 `
  -Target "~/fortune_bot"
```

The deploy script no longer creates a backup tarball under
`~/fortune_bot_backups/`.

## Legacy Analysis

Older exploratory distribution scripts remain under:

```text
reach_distribution/
reach_distribution/legacy/
```

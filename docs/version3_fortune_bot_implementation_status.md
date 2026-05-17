# version3 fortune_bot Implementation Status

Generated for the current local artifact:

```text
execution_engine/deploy/early_trade_label_v1
dist/fortune_bot_early_trade_label_v1.tar.gz
```

## Current Result

The engineering path is implemented for paper deployment, artifact export,
maker EV planning, idempotency, guarded live submission, and deployment
packaging. Live deployment remains blocked by the model metric gate.

`version3` is deployed in paper-only mode at `/home/ec2-user/fortune_bot`.
The user systemd timer is enabled and confirmed firing after enabling linger
for `ec2-user`. It produced scheduled paper summaries at
`2026-05-16T16:20:26Z`, `2026-05-16T16:25:44Z`, and
`2026-05-16T16:30:44Z`.

Current manifest evidence:

```text
model_version: early_trade_label_v1
artifact_hash: cac4cb43ea2e6d6e38cbf17b9020b9948fc12a3d56cd0cb7c4820b3174ab6dea
validation coverage: 0.7045850261172374
validation accepted_sample_accuracy: 0.7817133443163097
holdout coverage: 0.7363530778164924
holdout accepted_sample_accuracy: 0.7854889589905363
live_eligible: false
deployment_status: paper_only_blocked
blocked_reasons: accepted_sample_accuracy_gt_0_80, holdout_accepted_sample_accuracy_gt_0_80
```

The PRD requires:

```text
validation coverage >= 0.70
validation accepted_sample_accuracy > 0.80
```

Only the coverage gate is currently met.

## Verification Commands

Paper/package verifier:

```bash
python -m execution_engine.verify_prd \
  --artifact-dir execution_engine/deploy/early_trade_label_v1 \
  --bundle dist/fortune_bot_early_trade_label_v1.tar.gz
```

Expected current result: `ok=true`, with a warning for
`accepted_sample_accuracy > 0.80`.

Live eligibility verifier:

```bash
python -m execution_engine.verify_prd \
  --artifact-dir execution_engine/deploy/early_trade_label_v1 \
  --bundle dist/fortune_bot_early_trade_label_v1.tar.gz \
  --require-live
```

Expected current result: non-zero exit, because
`accepted_sample_accuracy > 0.80` is not satisfied.

Paper smoke:

```bash
python -m execution_engine.prewarm --config config.example.yaml --print-json
python -m execution_engine.run_once --config config.example.yaml --mode paper --print-json
```

Runtime self-test:

```bash
python -m execution_engine.self_test \
  --artifact-dir execution_engine/deploy/early_trade_label_v1 \
  --bundle dist/fortune_bot_early_trade_label_v1.tar.gz \
  --config config.example.yaml
```

Expected current result: `ok=true`; the self-test also confirms that
`--require-live` verification blocks the current artifact.

Preflight:

```bash
python -m execution_engine.preflight --config config.example.yaml --mode paper --print-json
python -m execution_engine.preflight --config config.example.yaml --mode live --print-json
```

Expected current result: paper preflight passes; live preflight fails because
orders are disabled, credentials are not present, the manifest is not live
eligible, and live deposit-wallet checks require POLY_1271/signature type 3
with `DEPOSIT_WALLET_ADDRESS`.

Paper observation:

```bash
python -m execution_engine.observe_paper --config config.example.yaml --cycles 3 --sleep-seconds 300 --print-json
```

Expected server use: run three paper cycles before any manual live review. Local
self-test exercises one no-sleep cycle.

## Requirement Checklist

| PRD requirement | Current evidence | Status |
|---|---|---|
| Rewrite maker method document | `docs/polymarket_btc5m_maker_method_summary.md` | Done |
| Generate offline maker fill table | `execution_engine/maker_fill_table.py`; `maker_fill_table.csv` | Done |
| Implement maker EV order planner | `execution_engine/maker_order_plan.py`; planner smoke selected 2 orders with total budget 7.0 | Done |
| Enforce maker-only limit buys | planner emits `order_type=limit_buy`, `maker_only=true`; live adapter uses `post_only=True` | Done |
| Enforce total budget <= 7 USDC | `PlannerLimits.max_total_budget_usdc=7.0`; verifier checks manifest trading contract | Done |
| Enforce per-order budget <= 4 USDC | `PlannerLimits.max_order_budget_usdc=4.0` | Done |
| Enforce minimum 5 shares | planner requires `min_budget = min_shares * price` | Done |
| Only positive edge orders | planner filters `edge > 0` before allocation | Done |
| Tick-size price rounding | `floor_to_tick` rounds prices down | Done |
| Duplicate order prevention | planner dedupes candidate order keys before allocation; `execution_engine/idempotency.py` and `run_once` filter already-seen selected order keys | Done |
| Artifact manifest | `artifact_manifest.json`; verifier checks hashes, schema, `deployment_status`, `blocked_reasons`, and model gates | Done |
| Evaluation export metadata | `evaluation.json` and `metrics.json` record `model_version` and deploy `artifact_hash`; verifier checks both against manifest | Done |
| Bundle export | `execution_engine/artifact_export.py`; `dist/fortune_bot_early_trade_label_v1.tar.gz` | Done |
| Bundle excludes secrets/cache | verifier checks no `secrets.env`, `__pycache__`, runtime artifacts, or sensitive env assignments | Done |
| Safe paper defaults | verifier checks `config.example.yaml` paper mode, `orders.enabled=false`, and systemd `--mode paper` | Done |
| Paper prewarm | `execution_engine/prewarm.py` | Done |
| Paper `run_once` | `execution_engine/run_once.py` | Done, stdout summary verified |
| Runtime self-test | `execution_engine/self_test.py` | Done |
| Paper/live preflight | `execution_engine/preflight.py`; paper passes, live blocks current artifact | Done |
| Paper observation workflow | `execution_engine/observe_paper.py`; deploy script ran a one-cycle smoke on `version3`; timer produced scheduled paper summaries at `16:20`, `16:25`, and `16:30 UTC` on `2026-05-16` | Done for scheduled paper runtime |
| Runtime feature source config | `execution_engine/feature_source.py`; `config.example.yaml` supports `validation_snapshot`, `latest_feature_file`, `trades_csv_snapshot`, and `polymarket_live`; `config.polymarket_live.example.yaml` smoke fetched live Gamma/Data API market data | Implemented for Polymarket; Binance feature feed not used by current model |
| JSONL audit event | `run_once` appends compact audit JSONL; `self_test` verifies event shape and secret-safe fields | Done |
| Offline tuning audit | `models/early_trade_label_v1/tuning_report.json`; LightGBM/CatBoost included in full audit; current deploy model is `random_forest_sqrt` with validation `0.7817133443163097` and holdout `0.7854889589905363` accepted accuracy | Done, blocked |
| Holdout review | `evaluation.json` includes `holdout_window` and `holdout_metrics`; `artifact_manifest.json` and verifier include holdout coverage/accuracy gates | Done, blocked |
| q source priority | `maker_order_plan.choose_q` uses calibrated `p_side` only when a calibrator is present, otherwise matches `probability_reference.json` buckets, then falls back to validation `accepted_sample_accuracy`; `self_test` covers bucket and fallback paths | Done |
| Summary JSON file | configured path is written in normal environments; local Windows sandbox denies writes after pandas/sklearn import | Weakly verified locally |
| Live default off | `config.example.yaml` has `runtime.mode=paper`, `orders.enabled=false` | Done |
| Live requires explicit gates | `run_once` requires `orders.enabled=true` and `live_eligible=true` | Done |
| Deposit-wallet live preflight | `execution_engine/preflight.py` requires `signature_type=3`, `DEPOSIT_WALLET_ADDRESS`, valid wallet-address shape, and Polymarket client packages before live | Implemented; real credential smoke not run |
| Polymarket live adapter | `execution_engine/polymarket_adapter.py` | Implemented but not live-tested |
| version3 deployment script | `deploy/version3_deploy.ps1` | Executed paper-only; timer later enabled manually after smoke |
| version3 rollback script | `deploy/version3_rollback.ps1` | Implemented but not executed |
| version3 dry-run path check | `deploy/version3_deploy.ps1 -DryRun` reached `version3` and resolved `/home/ec2-user/fortune_bot` | Done |
| systemd timer/service | `deploy/fortune-bot.timer`, `deploy/fortune-bot.service` | Installed on `version3`; `ec2-user` linger enabled; timer enabled and confirmed fired through `2026-05-16T16:30:43Z` |
| Model coverage >= 0.70 | manifest shows validation `0.7045850261172374` and holdout `0.7363530778164924` | Done |
| Model accepted accuracy > 0.80 | manifest shows validation `0.7817133443163097` and holdout `0.7854889589905363` | Blocked |
| version3 backup/clear/deploy | `version3` deployed to `/home/ec2-user/fortune_bot`; two backups exist in `/home/ec2-user/fortune_bot_backups`; timer enabled after smoke; newest local artifact `cac4cb43...` has not been redeployed | Paper-only done; redeploy pending |
| Observe at least 3 paper cycles on server | scheduled paper summaries: `summary_20260516T162026Z.json`, `summary_20260516T162544Z.json`, `summary_20260516T163044Z.json` | Done for runtime scheduling; still uses validation snapshot features |

## Live Block Rationale

The current artifact must remain paper-only. The manifest and verifier both
compute `live_eligible=false` because validation and holdout accepted sample
accuracy are below the required threshold.

The reproducible tuning audit is stored in
`models/early_trade_label_v1/tuning_report.json` and copied into the deploy
artifact. The latest full audit selected `random_forest_sqrt`; it improved
validation accepted accuracy to `0.7817133443163097` but holdout fell to
`0.7854889589905363`, so live remains blocked.

Additional split-grid experiments are summarized locally in
`models/early_trade_label_v1_audit/split_grid_summary.csv`. The closest
validation result was `0.798680` at `train_fraction=0.75` and
`holdout_fraction=0.15`, but it failed accepted_count and holdout accuracy. A
separate 7d full-trade supplemental audit was materially worse, with best
validation accepted accuracy `0.7173144876325088`.

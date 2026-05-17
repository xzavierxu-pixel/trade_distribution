# Early Trade Label Pipeline

Builds market-level features from the first 120 seconds of Polymarket BTC 5m trade data and predicts final `up/down` resolution.

Default full pipeline:

```bash
python -m early_trade_label.cli run-all --config configs/early_trade_label_v1.yaml
```

This writes `models/early_trade_label_v1/evaluation.json` plus predictions, threshold search, feature importance, and diagnostics.

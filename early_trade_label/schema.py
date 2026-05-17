from __future__ import annotations

TRADE_RENAME = {
    "conditionId": "condition_id",
    "_condition_id": "condition_id",
    "slug": "slug",
    "_slug": "slug",
    "_market_start_ts": "market_start_ts",
    "_final_outcome": "final_outcome",
    "_outcome_norm": "outcome_norm",
    "transactionHash": "transaction_hash",
}

REQUIRED_TRADE_COLUMNS = {
    "condition_id",
    "market_start_ts",
    "timestamp",
    "outcome_norm",
    "price",
    "size",
}

LABEL_VALUES = {"up", "down"}


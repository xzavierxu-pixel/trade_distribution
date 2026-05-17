from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from early_trade_label.features import build_market_features
from early_trade_label.schema import LABEL_VALUES, TRADE_RENAME
from execution_engine.config import EngineConfig


def load_feature_row(cfg: EngineConfig, manifest: dict[str, Any], feature_columns: list[str]) -> tuple[pd.Series, dict[str, Any]]:
    source = cfg.features.source
    if source == "validation_snapshot":
        return _load_validation_snapshot(cfg, manifest)
    if source == "latest_feature_file":
        if cfg.features.path is None:
            raise ValueError("features.path is required for latest_feature_file")
        return _load_latest_feature_file(cfg.features.path)
    if source == "trades_csv_snapshot":
        if cfg.features.path is None:
            raise ValueError("features.path is required for trades_csv_snapshot")
        return _load_trades_csv_snapshot(cfg.features.path, cfg.features.feature_window_seconds, cfg.features.source_is_sell_only, feature_columns)
    raise ValueError(f"unsupported features.source: {source}")


def _load_validation_snapshot(cfg: EngineConfig, manifest: dict[str, Any]) -> tuple[pd.Series, dict[str, Any]]:
    features_path = cfg.baseline.artifact_dir / "features_validation.parquet"
    if not features_path.exists():
        features_path = Path("models") / manifest["model_version"] / "features_validation.parquet"
    features = pd.read_parquet(features_path)
    row = features.sort_values("market_start_ts").tail(1).iloc[0]
    return row, {
        "source": "validation_snapshot",
        "path": str(features_path),
        "feature_count": int(len(features.columns)),
    }


def _load_latest_feature_file(path: Path) -> tuple[pd.Series, dict[str, Any]]:
    table = pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)
    if table.empty:
        raise ValueError(f"feature file is empty: {path}")
    if "market_start_ts" in table.columns:
        table = table.sort_values("market_start_ts")
    row = table.tail(1).iloc[0]
    return row, {
        "source": "latest_feature_file",
        "path": str(path),
        "feature_count": int(len(table.columns)),
    }


def _load_trades_csv_snapshot(path: Path, feature_window_seconds: int, source_is_sell_only: bool, feature_columns: list[str]) -> tuple[pd.Series, dict[str, Any]]:
    trades = _normalize_runtime_trades(path)
    valid = trades[
        trades["condition_id"].notna()
        & trades["market_start_ts"].notna()
        & trades["timestamp"].notna()
        & trades["price"].between(0, 1)
        & (trades["size"] > 0)
        & trades["outcome_norm"].isin(LABEL_VALUES)
    ].copy()
    if valid.empty:
        raise ValueError(f"no valid runtime trades in {path}")
    valid["second_from_start"] = valid["timestamp"] - valid["market_start_ts"]
    latest_market_start = valid["market_start_ts"].max()
    market = valid[valid["market_start_ts"].eq(latest_market_start)].copy()
    condition_id = str(market["condition_id"].iloc[0])
    early = market[(market["second_from_start"] >= 0) & (market["second_from_start"] < feature_window_seconds)].copy()
    row = build_market_features(early, int(latest_market_start), source_is_sell_only)
    row["condition_id"] = condition_id
    for token_col in ["up_token_id", "down_token_id", "_up_token_id", "_down_token_id", "slug"]:
        if token_col in market.columns:
            value = market[token_col].dropna()
            if not value.empty:
                row[token_col] = value.iloc[-1]
    for col in feature_columns:
        row.setdefault(col, 0.0)
    return pd.Series(row), {
        "source": "trades_csv_snapshot",
        "path": str(path),
        "feature_count": int(len(feature_columns)),
        "raw_trade_rows": int(len(trades)),
        "valid_trade_rows": int(len(valid)),
        "condition_id": condition_id,
    }


def _normalize_runtime_trades(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    df = df.rename(columns={k: v for k, v in TRADE_RENAME.items() if k in df.columns})
    df = _coalesce_duplicate_columns(df)
    missing = {"condition_id", "market_start_ts", "timestamp", "outcome_norm", "price", "size"} - set(df.columns)
    if missing:
        raise ValueError(f"runtime trades missing required columns: {sorted(missing)}")
    for col in ["timestamp", "market_start_ts", "price", "size"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["condition_id", "outcome_norm", "side"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.lower().str.strip()
    return df


def _coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if not df.columns.has_duplicates:
        return df
    out = pd.DataFrame(index=df.index)
    for col in dict.fromkeys(df.columns):
        same = df.loc[:, df.columns == col]
        out[col] = same.bfill(axis=1).iloc[:, 0] if same.shape[1] > 1 else same.iloc[:, 0]
    return out

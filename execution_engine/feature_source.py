from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import requests

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
    if source == "polymarket_live":
        return _load_polymarket_live_snapshot(cfg, feature_columns)
    raise ValueError(f"unsupported features.source: {source}")


def _load_validation_snapshot(cfg: EngineConfig, manifest: dict[str, Any]) -> tuple[pd.Series, dict[str, Any]]:
    features_path = cfg.baseline.artifact_dir / "features/features_validation.parquet"
    if not features_path.exists():
        features_path = Path("models") / manifest["model_version"] / "features" / "features_validation.parquet"
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


def _load_polymarket_live_snapshot(cfg: EngineConfig, feature_columns: list[str]) -> tuple[pd.Series, dict[str, Any]]:
    now_ts = int(pd.Timestamp.now(tz="UTC").timestamp())
    market_start_ts = now_ts - (now_ts % 300)
    slug = f"btc-updown-5m-{market_start_ts}"
    market = _discover_live_market(cfg.polymarket.gamma_base_url, slug)
    condition_id = str(market["condition_id"])
    trades = _fetch_live_trades(cfg.polymarket.data_api_url, condition_id, market)
    if not trades.empty:
        trades["second_from_start"] = trades["timestamp"] - market_start_ts
        early = trades[
            (trades["second_from_start"] >= 0)
            & (trades["second_from_start"] < cfg.features.feature_window_seconds)
            & trades["outcome_norm"].isin(LABEL_VALUES)
        ].copy()
    else:
        early = pd.DataFrame(columns=["second_from_start", "price", "size", "outcome_norm", "side"])
    row = build_market_features(early, market_start_ts, source_is_sell_only=False)
    row["condition_id"] = condition_id
    row["slug"] = slug
    row["up_token_id"] = market.get("up_token_id")
    row["down_token_id"] = market.get("down_token_id")
    for col in feature_columns:
        row.setdefault(col, 0.0)
    return pd.Series(row), {
        "source": "polymarket_live",
        "path": None,
        "feature_count": int(len(feature_columns)),
        "slug": slug,
        "condition_id": condition_id,
        "raw_trade_rows": int(len(trades)),
        "early_trade_rows": int(len(early)),
    }


def _discover_live_market(gamma_base_url: str, slug: str) -> dict[str, Any]:
    response = requests.get(f"{gamma_base_url.rstrip('/')}/events", params={"slug": slug, "limit": 1}, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Gamma did not return event for slug {slug}")
    event = data[0]
    markets = _parse_maybe_json(event.get("markets") or [])
    if not isinstance(markets, list) or not markets:
        raise RuntimeError(f"Gamma event has no markets for slug {slug}")
    market = markets[0]
    condition_id = market.get("conditionId") or market.get("condition_id")
    if not condition_id:
        raise RuntimeError(f"Gamma market missing condition id for slug {slug}")
    up_token_id, down_token_id = _extract_token_ids(market)
    return {
        "condition_id": condition_id,
        "up_token_id": up_token_id,
        "down_token_id": down_token_id,
    }


def _fetch_live_trades(data_api_url: str, condition_id: str, market: dict[str, Any]) -> pd.DataFrame:
    response = requests.get(
        f"{data_api_url.rstrip('/')}/trades",
        params={"market": condition_id, "limit": 10000, "offset": 0, "takerOnly": "true"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list) or not data:
        return pd.DataFrame(columns=["condition_id", "timestamp", "price", "size", "outcome_norm", "side"])
    rows = []
    for trade in data:
        row = dict(trade)
        row["condition_id"] = condition_id
        row["price"] = pd.to_numeric(row.get("price"), errors="coerce")
        row["size"] = pd.to_numeric(row.get("size"), errors="coerce")
        row["timestamp"] = pd.to_numeric(row.get("timestamp"), errors="coerce")
        row["side"] = str(row.get("side") or "").lower()
        asset = str(row.get("asset") or row.get("token") or row.get("tokenId") or "")
        outcome = _norm_outcome(row.get("outcome"))
        if outcome is None and asset == str(market.get("up_token_id")):
            outcome = "up"
        if outcome is None and asset == str(market.get("down_token_id")):
            outcome = "down"
        row["outcome_norm"] = outcome
        rows.append(row)
    df = pd.DataFrame(rows)
    return df.dropna(subset=["timestamp", "price", "size"])


def _parse_maybe_json(value: Any) -> Any:
    if isinstance(value, (list, dict)) or value is None:
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") or text.startswith("{"):
            import json

            try:
                return json.loads(text)
            except Exception:
                return value
    return value


def _extract_token_ids(market: dict[str, Any]) -> tuple[str | None, str | None]:
    outcomes = _parse_maybe_json(market.get("outcomes"))
    token_ids = _parse_maybe_json(
        market.get("clobTokenIds")
        or market.get("clob_token_ids")
        or market.get("outcomeTokenIds")
        or market.get("tokens")
    )
    if isinstance(token_ids, list) and token_ids and isinstance(token_ids[0], dict):
        if not outcomes:
            outcomes = [t.get("outcome") or t.get("name") for t in token_ids]
        token_ids = [t.get("token_id") or t.get("tokenId") or t.get("id") or t.get("asset_id") for t in token_ids]
    up = down = None
    if isinstance(outcomes, list) and isinstance(token_ids, list):
        for outcome, token_id in zip(outcomes, token_ids):
            normalized = _norm_outcome(outcome)
            if normalized == "up":
                up = str(token_id)
            elif normalized == "down":
                down = str(token_id)
    return up, down


def _norm_outcome(value: Any) -> str | None:
    text = str(value).strip().lower() if value is not None else ""
    if text in {"up", "yes", "higher", "above"}:
        return "up"
    if text in {"down", "no", "lower", "below"}:
        return "down"
    return None


def _coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if not df.columns.has_duplicates:
        return df
    out = pd.DataFrame(index=df.index)
    for col in dict.fromkeys(df.columns):
        same = df.loc[:, df.columns == col]
        out[col] = same.bfill(axis=1).iloc[:, 0] if same.shape[1] > 1 else same.iloc[:, 0]
    return out

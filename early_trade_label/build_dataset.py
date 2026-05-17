from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from early_trade_label.features import build_market_features
from early_trade_label.schema import LABEL_VALUES, TRADE_RENAME


def normalize_trades(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    df = df.rename(columns={k: v for k, v in TRADE_RENAME.items() if k in df.columns})
    df = coalesce_duplicate_columns(df)
    missing = {"condition_id", "market_start_ts", "timestamp", "outcome_norm", "price", "size"} - set(df.columns)
    if missing:
        raise ValueError(f"Missing required trade columns after normalization: {sorted(missing)}")
    for col in ["timestamp", "market_start_ts", "price", "size"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["condition_id"] = df["condition_id"].astype(str)
    df["outcome_norm"] = df["outcome_norm"].astype(str).str.lower().str.strip()
    if "final_outcome" in df.columns:
        df["final_outcome"] = df["final_outcome"].astype(str).str.lower().str.strip()
    if "side" in df.columns:
        df["side"] = df["side"].astype(str).str.lower().str.strip()
    return df


def coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if not df.columns.has_duplicates:
        return df
    out = pd.DataFrame(index=df.index)
    for col in dict.fromkeys(df.columns):
        same = df.loc[:, df.columns == col]
        if same.shape[1] == 1:
            out[col] = same.iloc[:, 0]
        else:
            out[col] = same.bfill(axis=1).iloc[:, 0]
    return out


def load_labels(refs_csv: Path, trades: pd.DataFrame) -> pd.DataFrame:
    refs = pd.read_csv(refs_csv, dtype=str).rename(columns={"_condition_id": "condition_id", "_final_outcome": "final_outcome"})
    if "condition_id" not in refs.columns or "final_outcome" not in refs.columns:
        raise ValueError("refs csv must contain condition_id/final_outcome columns")
    refs["condition_id"] = refs["condition_id"].astype(str)
    refs["final_outcome"] = refs["final_outcome"].astype(str).str.lower().str.strip()
    refs = refs[refs["final_outcome"].isin(LABEL_VALUES)][["condition_id", "final_outcome"]].drop_duplicates()
    conflicts = refs.groupby("condition_id")["final_outcome"].nunique()
    refs = refs[~refs["condition_id"].isin(conflicts[conflicts > 1].index)]
    if not refs.empty:
        return refs
    return trades[["condition_id", "final_outcome"]].dropna().drop_duplicates()


def build_dataset(trades_csv: Path, refs_csv: Path, outdir: Path, feature_window_seconds: int) -> pd.DataFrame:
    outdir.mkdir(parents=True, exist_ok=True)
    trades = normalize_trades(trades_csv)
    labels = load_labels(refs_csv, trades)
    source_is_sell_only = set(trades.get("side", pd.Series(dtype=str)).dropna().str.lower().unique()) <= {"sell"}

    invalid_reasons = {
        "missing_condition_id": int(trades["condition_id"].isna().sum()),
        "missing_market_start_ts": int(trades["market_start_ts"].isna().sum()),
        "missing_timestamp": int(trades["timestamp"].isna().sum()),
        "invalid_price": int((~trades["price"].between(0, 1)).fillna(True).sum()),
        "invalid_size": int((~(trades["size"] > 0)).fillna(True).sum()),
        "invalid_outcome_norm": int((~trades["outcome_norm"].isin(LABEL_VALUES)).sum()),
    }
    valid = trades[
        trades["condition_id"].notna()
        & trades["market_start_ts"].notna()
        & trades["timestamp"].notna()
        & trades["price"].between(0, 1)
        & (trades["size"] > 0)
        & trades["outcome_norm"].isin(LABEL_VALUES)
    ].copy()
    valid["second_from_start"] = valid["timestamp"] - valid["market_start_ts"]
    early = valid[(valid["second_from_start"] >= 0) & (valid["second_from_start"] < feature_window_seconds)].copy()

    starts = valid.groupby("condition_id")["market_start_ts"].min().reset_index()
    base = labels.merge(starts, on="condition_id", how="inner")
    early_groups = {str(k): g for k, g in early.groupby("condition_id", sort=False)}
    rows = []
    for item in base.itertuples(index=False):
        ev = early_groups.get(str(item.condition_id), early.iloc[0:0])
        feats = build_market_features(ev, int(item.market_start_ts), source_is_sell_only)
        feats["condition_id"] = item.condition_id
        feats["final_outcome"] = item.final_outcome
        feats["label"] = 1 if item.final_outcome == "up" else 0
        rows.append(feats)
    dataset = pd.DataFrame(rows).sort_values("market_start_ts").reset_index(drop=True)
    dataset_tmp = outdir / "market_dataset.tmp.parquet"
    dataset_path = outdir / "market_dataset.parquet"
    dataset.to_parquet(dataset_tmp, index=False)
    dataset_tmp.replace(dataset_path)
    quality = {
        "trades_csv": str(trades_csv),
        "refs_csv": str(refs_csv),
        "feature_window_seconds": feature_window_seconds,
        "raw_trade_rows": int(len(trades)),
        "valid_trade_rows": int(len(valid)),
        "early_trade_rows": int(len(early)),
        "market_rows": int(len(dataset)),
        "empty_early_markets": int((dataset["early_has_trade"] == 0).sum()) if not dataset.empty else 0,
        "label_counts": dataset["final_outcome"].value_counts().to_dict() if not dataset.empty else {},
        "source_is_sell_only": bool(source_is_sell_only),
        "invalid_trade_reasons": invalid_reasons,
        "early_window_rule": "0 <= timestamp - market_start_ts < feature_window_seconds",
    }
    quality_tmp = outdir / "dataset_quality.json.tmp"
    quality_tmp.write_text(json.dumps(quality, indent=2), encoding="utf-8")
    quality_tmp.replace(outdir / "dataset_quality.json")
    return dataset


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades-csv", type=Path, required=True)
    parser.add_argument("--refs-csv", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--feature-window-seconds", type=int, default=120)
    args = parser.parse_args()
    build_dataset(args.trades_csv, args.refs_csv, args.outdir, args.feature_window_seconds)


if __name__ == "__main__":
    main()

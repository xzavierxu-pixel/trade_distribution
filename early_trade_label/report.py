from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


def _safe_accuracy(frame: pd.DataFrame) -> float:
    accepted = frame["prediction"] >= 0
    if not accepted.any():
        return float("nan")
    return float((frame.loc[accepted, "prediction"] == frame.loc[accepted, "label"]).mean())


def write_reports(train_pred: pd.DataFrame, val_pred: pd.DataFrame, feature_importance: pd.DataFrame, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    feature_importance.to_csv(outdir / "feature_importance.csv", index=False)
    dec = val_pred.copy()
    dec["decile"] = pd.qcut(dec["p_up"], 10, labels=False, duplicates="drop")
    dec["accepted"] = dec["prediction"] >= 0
    dec["accepted_correct"] = np.where(dec["accepted"], dec["prediction"] == dec["label"], np.nan)
    decile = dec.groupby("decile", dropna=False).agg(
        row_count=("label", "size"),
        p_up_min=("p_up", "min"),
        p_up_max=("p_up", "max"),
        p_up_mean=("p_up", "mean"),
        actual_up_rate=("label", "mean"),
        accepted_rate=("accepted", "mean"),
        accuracy=("accepted_correct", "mean"),
    ).reset_index()
    decile.to_csv(outdir / "probability_deciles.csv", index=False)
    for name, frame in {
        "false_up_slices.csv": val_pred[(val_pred["prediction"] == 1) & (val_pred["label"] == 0)],
        "false_down_slices.csv": val_pred[(val_pred["prediction"] == 0) & (val_pred["label"] == 1)],
    }.items():
        frame.head(500).to_csv(outdir / name, index=False)
    slices = val_pred.copy()
    slices["hour_utc_bucket"] = slices.get("hour_utc", pd.Series(0, index=slices.index)).astype(int)
    slices["accepted"] = slices["prediction"] >= 0
    slices["accepted_correct"] = np.where(slices["accepted"], slices["prediction"] == slices["label"], np.nan)
    regime = slices.groupby("hour_utc_bucket").agg(
        row_count=("label", "size"),
        actual_up_rate=("label", "mean"),
        p_up_mean=("p_up", "mean"),
        coverage=("accepted", "mean"),
        accuracy=("accepted_correct", "mean"),
    ).reset_index()
    baseline = float(max(val_pred["label"].mean(), 1 - val_pred["label"].mean()))
    regime["selection_score_proxy"] = (regime["accuracy"] - baseline) / (1.0 - regime["accuracy"]).clip(lower=1e-9)
    regime.to_csv(outdir / "regime_slices.csv", index=False)
    ref = {
        "train_p_up_mean": float(train_pred["p_up"].mean()),
        "validation_p_up_mean": float(val_pred["p_up"].mean()),
        "train_rows": int(len(train_pred)),
        "validation_rows": int(len(val_pred)),
        "accepted_accuracy_by_bucket": [
            {
                "decile": None if pd.isna(row["decile"]) else int(row["decile"]),
                "p_up_min": float(row["p_up_min"]),
                "p_up_max": float(row["p_up_max"]),
                "p_up_mean": float(row["p_up_mean"]),
                "actual_up_rate": float(row["actual_up_rate"]),
                "accepted_rate": float(row["accepted_rate"]),
                "accepted_accuracy": None if pd.isna(row["accuracy"]) else float(row["accuracy"]),
            }
            for row in decile.to_dict("records")
        ],
    }
    (outdir / "probability_reference.json").write_text(json.dumps(ref, indent=2), encoding="utf-8")

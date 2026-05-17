from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from early_trade_label.threshold_search import choose_best, score_policy, search_thresholds


def window(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "row_count": int(len(df)),
        "start": str(pd.to_datetime(df["market_start_ts"], unit="s", utc=True).min()) if len(df) else None,
        "end": str(pd.to_datetime(df["market_start_ts"], unit="s", utc=True).max()) if len(df) else None,
    }


def add_prob_metrics(metrics: dict[str, float], y: np.ndarray, p_up: np.ndarray) -> dict[str, float]:
    metrics = dict(metrics)
    metrics["roc_auc"] = float(roc_auc_score(y, p_up)) if len(np.unique(y)) == 2 else float("nan")
    metrics["brier_score"] = float(brier_score_loss(y, p_up))
    metrics["log_loss"] = float(log_loss(y, np.clip(p_up, 1e-6, 1 - 1e-6), labels=[0, 1]))
    return metrics


def probability_summary(train_pred: pd.DataFrame, val_pred: pd.DataFrame) -> dict[str, Any]:
    def stats(s: pd.Series) -> dict[str, float]:
        return {
            "mean": float(s.mean()), "std": float(s.std(ddof=0)),
            "p10": float(s.quantile(0.10)), "p50": float(s.quantile(0.50)), "p90": float(s.quantile(0.90)),
        }
    return {"p_up_train": stats(train_pred["p_up"]), "p_up_validation": stats(val_pred["p_up"])}


def write_evaluation(
    project: str,
    feature_columns: list[str],
    train_pred: pd.DataFrame,
    val_pred: pd.DataFrame,
    outdir: Path,
    min_coverage: float,
    threshold_step: float,
    feature_window_seconds: int,
    holdout_pred: pd.DataFrame | None = None,
    model_version: str | None = None,
) -> dict[str, Any]:
    outdir.mkdir(parents=True, exist_ok=True)
    baseline = float(max(val_pred["label"].mean(), 1 - val_pred["label"].mean()))
    search = search_thresholds(val_pred["label"].to_numpy(), val_pred["p_up"].to_numpy(), min_coverage, threshold_step, baseline)
    search.to_csv(outdir / "threshold_search.csv", index=False)
    best = choose_best(search, min_coverage)
    t_up = float(best["selected_t_up"])
    t_down = float(best["selected_t_down"])
    train_metrics = score_policy(train_pred["label"].to_numpy(), train_pred["p_up"].to_numpy(), t_up, t_down, baseline)
    val_metrics = score_policy(val_pred["label"].to_numpy(), val_pred["p_up"].to_numpy(), t_up, t_down, baseline)
    train_metrics = add_prob_metrics(train_metrics, train_pred["label"].to_numpy(), train_pred["p_up"].to_numpy())
    val_metrics = add_prob_metrics(val_metrics, val_pred["label"].to_numpy(), val_pred["p_up"].to_numpy())
    holdout_metrics = None
    if holdout_pred is not None and not holdout_pred.empty:
        holdout_baseline = float(max(holdout_pred["label"].mean(), 1 - holdout_pred["label"].mean()))
        holdout_metrics = score_policy(holdout_pred["label"].to_numpy(), holdout_pred["p_up"].to_numpy(), t_up, t_down, holdout_baseline)
        holdout_metrics = add_prob_metrics(holdout_metrics, holdout_pred["label"].to_numpy(), holdout_pred["p_up"].to_numpy())
    result = {
        "project": project,
        "model_version": model_version,
        "artifact_hash": None,
        "market": "BTC/USDT",
        "exchange": "polymarket",
        "horizon": "5m",
        "objective": "weighted_binary_selective_direction",
        "feature_window_seconds": feature_window_seconds,
        "feature_count": len(feature_columns),
        "feature_columns": feature_columns,
        "label_column": "final_outcome",
        "prediction_column": "p_up",
        "decision_policy": {
            "coverage_constraint": min_coverage,
            "coverage_constraint_satisfied": bool(val_metrics["coverage"] >= min_coverage),
            "selected_t_up": t_up,
            "selected_t_down": t_down,
        },
        "probability_summary": probability_summary(train_pred, val_pred),
        "train_window": window(train_pred),
        "validation_window": window(val_pred),
        "holdout_window": window(holdout_pred) if holdout_pred is not None and not holdout_pred.empty else None,
        "train_metrics": train_metrics,
        "validation_metrics": val_metrics,
        "holdout_metrics": holdout_metrics,
        "threshold_search_path": "threshold_search.csv",
        "feature_importance_path": "feature_importance.csv",
        "probability_deciles_path": "probability_deciles.csv",
        "regime_slices_path": "regime_slices.csv",
        "false_up_slices_path": "false_up_slices.csv",
        "false_down_slices_path": "false_down_slices.csv",
        "probability_reference_path": "probability_reference.json",
    }
    (outdir / "evaluation.json").write_text(json.dumps(result, indent=2, allow_nan=True), encoding="utf-8")
    return result

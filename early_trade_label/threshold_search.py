from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def apply_policy(p_up: np.ndarray, t_up: float, t_down: float) -> np.ndarray:
    pred = np.full(len(p_up), -1, dtype=int)
    pred[p_up >= t_up] = 1
    pred[p_up < t_down] = 0
    if abs(t_up - t_down) < 1e-12:
        pred[p_up >= 0.5] = 1
        pred[p_up < 0.5] = 0
    return pred


def score_policy(y: np.ndarray, p_up: np.ndarray, t_up: float, t_down: float, baseline_accuracy: float) -> dict[str, float]:
    pred = apply_policy(p_up, t_up, t_down)
    accepted = pred >= 0
    accepted_count = int(accepted.sum())
    sample_count = len(y)
    if accepted_count == 0:
        return _empty(sample_count, t_up, t_down)
    y_acc = y[accepted]
    p_acc = pred[accepted]
    correct = p_acc == y_acc
    up_mask = p_acc == 1
    down_mask = p_acc == 0
    accepted_accuracy = float(correct.mean())
    utility = accepted_accuracy - baseline_accuracy
    downside = max(1e-9, 1.0 - accepted_accuracy)
    return {
        "sample_count": float(sample_count),
        "coverage": accepted_count / sample_count,
        "precision_up": float((y_acc[up_mask] == 1).mean()) if up_mask.any() else np.nan,
        "precision_down": float((y_acc[down_mask] == 0).mean()) if down_mask.any() else np.nan,
        "balanced_precision": float(np.nanmean([
            float((y_acc[up_mask] == 1).mean()) if up_mask.any() else np.nan,
            float((y_acc[down_mask] == 0).mean()) if down_mask.any() else np.nan,
        ])),
        "all_sample_accuracy": float(((p_up >= 0.5).astype(int) == y).mean()),
        "accepted_sample_accuracy": accepted_accuracy,
        "utility": utility,
        "downside_risk": downside,
        "selection_score": utility / downside,
        "share_up_predictions": float(up_mask.mean()),
        "share_down_predictions": float(down_mask.mean()),
        "selected_t_up": float(t_up),
        "selected_t_down": float(t_down),
        "accepted_count": float(accepted_count),
        "up_prediction_count": float(up_mask.sum()),
        "down_prediction_count": float(down_mask.sum()),
    }


def _empty(sample_count: int, t_up: float, t_down: float) -> dict[str, float]:
    return {
        "sample_count": float(sample_count), "coverage": 0.0, "precision_up": np.nan, "precision_down": np.nan,
        "balanced_precision": np.nan, "all_sample_accuracy": np.nan, "accepted_sample_accuracy": np.nan,
        "utility": -1.0, "downside_risk": 1.0, "selection_score": -1.0, "share_up_predictions": 0.0,
        "share_down_predictions": 0.0, "selected_t_up": float(t_up), "selected_t_down": float(t_down),
        "accepted_count": 0.0, "up_prediction_count": 0.0, "down_prediction_count": 0.0,
    }


def search_thresholds(y: np.ndarray, p_up: np.ndarray, min_coverage: float, step: float, baseline_accuracy: float) -> pd.DataFrame:
    rows = []
    ups = np.round(np.arange(0.50, 0.750001, step), 6)
    downs = np.round(np.arange(0.25, 0.500001, step), 6)
    for t_up in ups:
        for t_down in downs:
            if t_down > t_up:
                continue
            rows.append(score_policy(y, p_up, float(t_up), float(t_down), baseline_accuracy))
    df = pd.DataFrame(rows)
    df["coverage_constraint_satisfied"] = df["coverage"] >= min_coverage
    return df.sort_values(["coverage_constraint_satisfied", "selection_score", "coverage"], ascending=[False, False, False])


def choose_best(search: pd.DataFrame, min_coverage: float) -> dict[str, float]:
    feasible = search[search["coverage"] >= min_coverage]
    row = (feasible if not feasible.empty else search).iloc[0].to_dict()
    row["coverage_constraint_satisfied"] = bool(row.get("coverage", 0.0) >= min_coverage)
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions-csv", type=Path, required=True)
    parser.add_argument("--out-csv", type=Path, required=True)
    parser.add_argument("--min-coverage", type=float, default=0.70)
    parser.add_argument("--step", type=float, default=0.005)
    args = parser.parse_args()
    df = pd.read_csv(args.predictions_csv)
    baseline = max(df["label"].mean(), 1 - df["label"].mean())
    out = search_thresholds(df["label"].to_numpy(), df["p_up"].to_numpy(), args.min_coverage, args.step, float(baseline))
    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.out_csv, index=False)


if __name__ == "__main__":
    main()


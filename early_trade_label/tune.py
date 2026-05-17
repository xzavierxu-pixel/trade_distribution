from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import AdaBoostClassifier, ExtraTreesClassifier, GradientBoostingClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from early_trade_label.threshold_search import choose_best, search_thresholds
from early_trade_label.train_model import feature_columns, split_time


MIN_ACCEPTED_SAMPLE_ACCURACY = 0.80


def _optional_boosters(random_state: int) -> dict[str, Pipeline]:
    boosters: dict[str, Pipeline] = {}
    try:
        from lightgbm import LGBMClassifier

        boosters["lightgbm_gbdt"] = Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", LGBMClassifier(
                n_estimators=600,
                learning_rate=0.025,
                num_leaves=31,
                min_child_samples=30,
                subsample=0.85,
                colsample_bytree=0.85,
                reg_alpha=0.05,
                reg_lambda=0.20,
                objective="binary",
                random_state=random_state + 20,
                n_jobs=-1,
                verbosity=-1,
            )),
        ])
    except Exception:
        pass
    try:
        from catboost import CatBoostClassifier

        boosters["catboost_ordered"] = Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", CatBoostClassifier(
                iterations=700,
                learning_rate=0.025,
                depth=5,
                l2_leaf_reg=8.0,
                loss_function="Logloss",
                eval_metric="AUC",
                random_seed=random_state + 30,
                verbose=False,
                allow_writing_files=False,
            )),
        ])
    except Exception:
        pass
    return boosters


def candidate_models(random_state: int) -> dict[str, Pipeline]:
    models = {
        "hist_gradient_boosting_regularized": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", HistGradientBoostingClassifier(max_iter=400, learning_rate=0.03, l2_regularization=0.10, max_leaf_nodes=31, random_state=random_state)),
        ]),
        "hist_gradient_boosting_shallow": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, l2_regularization=0.01, max_leaf_nodes=15, random_state=random_state + 1)),
        ]),
        "random_forest_sqrt": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", RandomForestClassifier(n_estimators=500, min_samples_leaf=5, max_features="sqrt", n_jobs=-1, random_state=random_state + 2)),
        ]),
        "extra_trees_sqrt": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", ExtraTreesClassifier(n_estimators=500, min_samples_leaf=5, max_features="sqrt", n_jobs=-1, random_state=random_state + 3)),
        ]),
        "gradient_boosting_shallow": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", GradientBoostingClassifier(n_estimators=250, learning_rate=0.03, max_depth=2, random_state=random_state + 4)),
        ]),
        "adaboost": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", AdaBoostClassifier(n_estimators=300, learning_rate=0.03, random_state=random_state + 5)),
        ]),
        "logistic_regression_balanced": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", LogisticRegression(max_iter=3000, class_weight="balanced", random_state=random_state + 6)),
        ]),
    }
    models.update(_optional_boosters(random_state))
    return models


def run_tuning_audit(
    dataset_path: Path,
    outdir: Path,
    train_fraction: float = 0.70,
    purge_minutes: int = 10,
    min_coverage: float = 0.70,
    threshold_step: float = 0.005,
    random_state: int = 42,
) -> dict[str, Any]:
    outdir.mkdir(parents=True, exist_ok=True)
    dataset = pd.read_parquet(dataset_path)
    train_df, val_df = split_time(dataset, train_fraction, purge_minutes)
    cols = feature_columns(dataset)
    x_train, y_train = train_df[cols], train_df["label"].astype(int).to_numpy()
    x_val, y_val = val_df[cols], val_df["label"].astype(int).to_numpy()
    baseline = float(max(y_val.mean(), 1 - y_val.mean()))

    rows = []
    for name, model in candidate_models(random_state).items():
        model.fit(x_train, y_train)
        p_val = model.predict_proba(x_val)[:, 1]
        search = search_thresholds(y_val, p_val, min_coverage, threshold_step, baseline)
        chosen = choose_best(search, min_coverage)
        rows.append({
            "model": name,
            "roc_auc": float(roc_auc_score(y_val, p_val)) if len(np.unique(y_val)) == 2 else float("nan"),
            "coverage": float(chosen["coverage"]),
            "accepted_sample_accuracy": float(chosen["accepted_sample_accuracy"]),
            "accepted_count": int(chosen["accepted_count"]),
            "up_prediction_count": int(chosen["up_prediction_count"]),
            "down_prediction_count": int(chosen["down_prediction_count"]),
            "selected_t_up": float(chosen["selected_t_up"]),
            "selected_t_down": float(chosen["selected_t_down"]),
            "selection_score": float(chosen["selection_score"]),
            "coverage_constraint_satisfied": bool(chosen["coverage_constraint_satisfied"]),
        })

    result_df = pd.DataFrame(rows).sort_values(
        ["coverage_constraint_satisfied", "accepted_sample_accuracy", "balanced_precision" if "balanced_precision" in rows[0] else "roc_auc"],
        ascending=[False, False, False],
    )
    write_errors = []
    _write_text(outdir / "tuning_candidates.csv", result_df.to_csv(index=False), write_errors)
    best = result_df.iloc[0].to_dict()
    gates = {
        "coverage_gte_0_70": float(best["coverage"]) >= 0.70,
        "accepted_sample_accuracy_gt_0_80": float(best["accepted_sample_accuracy"]) > MIN_ACCEPTED_SAMPLE_ACCURACY,
        "accepted_count_gte_1000": int(best["accepted_count"]) >= 1000,
        "up_prediction_count_gte_200": int(best["up_prediction_count"]) >= 200,
        "down_prediction_count_gte_200": int(best["down_prediction_count"]) >= 200,
    }
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_path": str(dataset_path),
        "train_fraction": train_fraction,
        "purge_minutes": purge_minutes,
        "min_coverage": min_coverage,
        "threshold_step": threshold_step,
        "train_rows": int(len(train_df)),
        "validation_rows": int(len(val_df)),
        "candidate_count": int(len(result_df)),
        "candidates": rows,
        "best_candidate": best,
        "live_gate_checks": gates,
        "live_eligible": all(gates.values()),
        "blocked_reasons": [name for name, passed in gates.items() if not passed],
        "write_errors": write_errors,
    }
    _write_text(outdir / "tuning_report.json", json.dumps(report, indent=2, allow_nan=True), write_errors)
    return report


def _write_text(path: Path, text: str, write_errors: list[str]) -> None:
    try:
        path.write_text(text, encoding="utf-8")
        return
    except PermissionError as exc:
        write_errors.append(f"{path}: {exc}")
    try:
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import base64, pathlib, sys; pathlib.Path(sys.argv[1]).write_bytes(base64.b64decode(sys.argv[2].encode('ascii')))",
                str(path),
                base64.b64encode(text.encode("utf-8")).decode("ascii"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        write_errors.append(f"{path}: child writer failed: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--purge-minutes", type=int, default=10)
    parser.add_argument("--min-coverage", type=float, default=0.70)
    parser.add_argument("--threshold-step", type=float, default=0.005)
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()
    report = run_tuning_audit(
        args.dataset,
        args.outdir,
        args.train_fraction,
        args.purge_minutes,
        args.min_coverage,
        args.threshold_step,
        args.random_state,
    )
    print(json.dumps(report, indent=2, allow_nan=True))


if __name__ == "__main__":
    main()

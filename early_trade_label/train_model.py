from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.inspection import permutation_importance
from sklearn.preprocessing import StandardScaler

from early_trade_label.evaluate import write_evaluation
from early_trade_label.report import write_reports
from early_trade_label.threshold_search import apply_policy, choose_best, search_thresholds


NON_FEATURE_COLUMNS = {"condition_id", "final_outcome", "label", "market_start_ts"}


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


def split_time(dataset: pd.DataFrame, train_fraction: float, purge_minutes: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = dataset.sort_values("market_start_ts").reset_index(drop=True)
    cut = int(len(df) * train_fraction)
    train = df.iloc[:cut].copy()
    val = df.iloc[cut:].copy()
    if purge_minutes > 0 and not train.empty and not val.empty:
        purge_seconds = purge_minutes * 60
        train_end = train["market_start_ts"].max()
        val = val[val["market_start_ts"] >= train_end + purge_seconds].copy()
    return train, val


def feature_columns(df: pd.DataFrame) -> list[str]:
    cols = []
    for col in df.columns:
        if col in NON_FEATURE_COLUMNS:
            continue
        if pd.api.types.is_numeric_dtype(df[col]):
            cols.append(col)
    return sorted(cols)


def candidates(random_state: int) -> dict[str, Pipeline]:
    models = {
        "hist_gradient_boosting": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", HistGradientBoostingClassifier(max_iter=250, learning_rate=0.04, l2_regularization=0.05, random_state=random_state)),
        ]),
        "random_forest": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", RandomForestClassifier(n_estimators=300, min_samples_leaf=8, n_jobs=-1, random_state=random_state)),
        ]),
        "logistic_regression": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", LogisticRegression(max_iter=2000, class_weight="balanced", random_state=random_state)),
        ]),
    }
    models.update(_optional_boosters(random_state))
    return models


def train(
    dataset_path: Path,
    outdir: Path,
    project: str,
    train_fraction: float,
    purge_minutes: int,
    min_coverage: float,
    threshold_step: float,
    random_state: int,
    feature_window_seconds: int = 120,
) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    dataset = pd.read_parquet(dataset_path)
    train_df, val_df = split_time(dataset, train_fraction, purge_minutes)
    cols = feature_columns(dataset)
    if not cols:
        raise ValueError("No numeric feature columns available for training")
    x_train, y_train = train_df[cols], train_df["label"].astype(int)
    x_val, y_val = val_df[cols], val_df["label"].astype(int)
    train_df.to_parquet(outdir / "features_train.parquet", index=False)
    val_df.to_parquet(outdir / "features_validation.parquet", index=False)

    best_name = ""
    best_model: Pipeline | None = None
    best_score = -np.inf
    selection_rows = []
    baseline = float(max(y_val.mean(), 1 - y_val.mean()))
    for name, model in candidates(random_state).items():
        model.fit(x_train, y_train)
        p_val = model.predict_proba(x_val)[:, 1]
        auc = roc_auc_score(y_val, p_val) if len(np.unique(y_val)) == 2 else 0.5
        search = search_thresholds(y_val.to_numpy(), p_val, min_coverage, threshold_step, baseline)
        chosen = choose_best(search, min_coverage)
        chosen["model"] = name
        chosen["roc_auc"] = float(auc)
        selection_rows.append(chosen)
        score = float(chosen["selection_score"])
        if bool(chosen["coverage_constraint_satisfied"]) and score > best_score:
            best_name, best_model, best_score = name, model, score
    if best_model is None:
        fallback = max(selection_rows, key=lambda r: (float(r["coverage"]), float(r["selection_score"])))
        best_name = str(fallback["model"])
        best_model = candidates(random_state)[best_name]
        best_model.fit(x_train, y_train)
        best_score = float(fallback["selection_score"])
    assert best_model is not None

    train_pred = train_df[["condition_id", "market_start_ts", "final_outcome", "label"]].copy()
    val_pred = val_df[["condition_id", "market_start_ts", "final_outcome", "label"]].copy()
    train_pred["p_up"] = best_model.predict_proba(x_train)[:, 1]
    val_pred["p_up"] = best_model.predict_proba(x_val)[:, 1]

    evaluation = write_evaluation(project, cols, train_pred, val_pred, outdir, min_coverage, threshold_step, feature_window_seconds)
    t_up = evaluation["decision_policy"]["selected_t_up"]
    t_down = evaluation["decision_policy"]["selected_t_down"]
    train_pred["prediction"] = apply_policy(train_pred["p_up"].to_numpy(), t_up, t_down)
    val_pred["prediction"] = apply_policy(val_pred["p_up"].to_numpy(), t_up, t_down)
    train_pred.to_csv(outdir / "predictions_train.csv", index=False)
    val_pred.to_csv(outdir / "predictions_validation.csv", index=False)

    importance = make_importance(best_model, cols, x_val, y_val, random_state)
    write_reports(train_pred.merge(train_df[["condition_id"] + [c for c in ["hour_utc"] if c in train_df.columns]], on="condition_id", how="left"),
                  val_pred.merge(val_df[["condition_id"] + [c for c in ["hour_utc"] if c in val_df.columns]], on="condition_id", how="left"),
                  importance, outdir)
    with (outdir / "model.pkl").open("wb") as f:
        pickle.dump(best_model, f)
    (outdir / "feature_columns.json").write_text(json.dumps(cols, indent=2), encoding="utf-8")
    pd.DataFrame(selection_rows).to_csv(outdir / "model_selection.csv", index=False)
    (outdir / "model_selection.json").write_text(
        json.dumps({"selected_model": best_name, "validation_selection_score": best_score}, indent=2),
        encoding="utf-8",
    )


def make_importance(model: Pipeline, cols: list[str], x_val: pd.DataFrame, y_val: pd.Series, random_state: int) -> pd.DataFrame:
    estimator = model.named_steps["model"]
    if hasattr(estimator, "feature_importances_"):
        imp = getattr(estimator, "feature_importances_")
    elif hasattr(estimator, "coef_"):
        imp = np.abs(getattr(estimator, "coef_")[0])
    else:
        try:
            result = permutation_importance(
                model,
                x_val,
                y_val,
                scoring="roc_auc",
                n_repeats=3,
                random_state=random_state,
                n_jobs=1,
            )
            imp = result.importances_mean
        except Exception:
            imp = np.zeros(len(cols))
    return pd.DataFrame({"feature": cols, "importance": imp}).sort_values("importance", ascending=False).assign(rank=lambda d: range(1, len(d) + 1))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--project", default="btc-polymarket-early-trade-v1")
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--purge-minutes", type=int, default=10)
    parser.add_argument("--min-coverage", type=float, default=0.70)
    parser.add_argument("--threshold-step", type=float, default=0.005)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--feature-window-seconds", type=int, default=120)
    args = parser.parse_args()
    train(
        args.dataset,
        args.outdir,
        args.project,
        args.train_fraction,
        args.purge_minutes,
        args.min_coverage,
        args.threshold_step,
        args.random_state,
        args.feature_window_seconds,
    )


if __name__ == "__main__":
    main()

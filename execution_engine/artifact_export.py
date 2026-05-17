from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_MODEL_FILES = ["model.pkl", "feature_columns.json", "evaluation.json", "threshold_search.csv"]
MIN_ACCEPTED_SAMPLE_ACCURACY = 0.80
OPTIONAL_MODEL_FILES = [
    "calibrator.pkl",
    "probability_reference.json",
    "probability_deciles.csv",
    "regime_slices.csv",
    "false_up_slices.csv",
    "false_down_slices.csv",
    "features_validation.parquet",
    "features_holdout.parquet",
    "predictions_holdout.csv",
    "tuning_report.json",
    "tuning_candidates.csv",
]


def export_artifact(
    model_dir: Path,
    deploy_dir: Path,
    model_version: str | None = None,
    maker_fill_table: Path | None = None,
    bundle_out: Path | None = None,
) -> dict[str, Any]:
    missing = [name for name in REQUIRED_MODEL_FILES if not (model_dir / name).exists()]
    if missing:
        raise FileNotFoundError(f"model_dir is missing required files: {missing}")
    model_version = model_version or model_dir.name
    target = deploy_dir / model_version
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in REQUIRED_MODEL_FILES + OPTIONAL_MODEL_FILES:
        src = model_dir / name
        if src.exists():
            dst_name = "threshold_search.json" if name == "threshold_search.csv" else name
            if name == "threshold_search.csv":
                _csv_to_json(src, target / dst_name)
            else:
                _copy_file(src, target / dst_name)
            copied.append(dst_name)
    if maker_fill_table:
        maker_dst = target / ("maker_fill_table.parquet" if maker_fill_table.suffix.lower() == ".parquet" else "maker_fill_table.csv")
        if maker_fill_table.resolve() != maker_dst.resolve():
            _copy_file(maker_fill_table, maker_dst)
    else:
        maker_dst = target / "maker_fill_table.csv"
        maker_dst.write_text("", encoding="utf-8")
    copied.append(maker_dst.name)

    evaluation = json.loads((model_dir / "evaluation.json").read_text(encoding="utf-8"))
    val = evaluation["validation_metrics"]
    holdout = evaluation.get("holdout_metrics") or {}
    policy = evaluation["decision_policy"]
    holdout_gates = _metric_gates(holdout, "holdout") if holdout else {}
    live_eligible = (
        float(val.get("coverage", 0.0)) >= 0.70
        and float(val.get("accepted_sample_accuracy", 0.0)) > MIN_ACCEPTED_SAMPLE_ACCURACY
        and int(val.get("accepted_count", 0)) >= 1000
        and int(val.get("up_prediction_count", 0)) >= 200
        and int(val.get("down_prediction_count", 0)) >= 200
        and bool(policy.get("coverage_constraint_satisfied", False))
        and all(holdout_gates.values())
    )
    gate_checks = {
        "coverage_gte_0_70": float(val.get("coverage", 0.0)) >= 0.70,
        "accepted_sample_accuracy_gt_0_80": float(val.get("accepted_sample_accuracy", 0.0)) > MIN_ACCEPTED_SAMPLE_ACCURACY,
        "accepted_count_gte_1000": int(val.get("accepted_count", 0)) >= 1000,
        "up_prediction_count_gte_200": int(val.get("up_prediction_count", 0)) >= 200,
        "down_prediction_count_gte_200": int(val.get("down_prediction_count", 0)) >= 200,
        "coverage_constraint_satisfied": bool(policy.get("coverage_constraint_satisfied", False)),
    }
    gate_checks.update(holdout_gates)
    blocked_reasons = [name for name, passed in gate_checks.items() if not passed]
    manifest = {
        "project": "btc-polymarket-5m-maker",
        "model_version": model_version,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "live_eligible": live_eligible,
        "deployment_status": "live_candidate" if live_eligible else "paper_only_blocked",
        "blocked_reasons": blocked_reasons,
        "model_gate_checks": gate_checks,
        "model_file": "model.pkl",
        "calibrator_file": "calibrator.pkl" if (target / "calibrator.pkl").exists() else None,
        "feature_columns_file": "feature_columns.json",
        "probability_reference_file": "probability_reference.json" if (target / "probability_reference.json").exists() else None,
        "maker_fill_table_file": maker_dst.name,
        "thresholds": {
            "t_up": float(policy["selected_t_up"]),
            "t_down": float(policy["selected_t_down"]),
            "min_coverage": 0.70,
            "min_accepted_sample_accuracy": MIN_ACCEPTED_SAMPLE_ACCURACY,
        },
        "validation_metrics": {
            "coverage": float(val.get("coverage", 0.0)),
            "accepted_sample_accuracy": float(val.get("accepted_sample_accuracy", 0.0)),
            "accepted_count": int(val.get("accepted_count", 0)),
            "up_prediction_count": int(val.get("up_prediction_count", 0)),
            "down_prediction_count": int(val.get("down_prediction_count", 0)),
        },
        "holdout_metrics": {
            "coverage": float(holdout.get("coverage", 0.0)),
            "accepted_sample_accuracy": float(holdout.get("accepted_sample_accuracy", 0.0)),
            "accepted_count": int(holdout.get("accepted_count", 0)),
            "up_prediction_count": int(holdout.get("up_prediction_count", 0)),
            "down_prediction_count": int(holdout.get("down_prediction_count", 0)),
        } if holdout else None,
        "decision_policy": {
            "coverage_constraint_satisfied": bool(policy.get("coverage_constraint_satisfied", False)),
        },
        "trading": {
            "max_total_budget_usdc": 7.0,
            "max_order_budget_usdc": 4.0,
            "min_shares": 5.0,
            "maker_only": True,
        },
        "files": {},
    }
    _copy_file(model_dir / "evaluation.json", target / "metrics.json")
    manifest_path = target / "artifact_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["files"] = {p.name: sha256_file(p) for p in sorted(target.iterdir()) if p.is_file() and p.name != "artifact_manifest.json"}
    manifest["artifact_hash"] = artifact_hash_from_files(manifest["files"])
    _inject_evaluation_export_metadata(target / "evaluation.json", model_version, manifest["artifact_hash"])
    _inject_evaluation_export_metadata(target / "metrics.json", model_version, manifest["artifact_hash"])
    manifest["files"] = {p.name: sha256_file(p) for p in sorted(target.iterdir()) if p.is_file() and p.name != "artifact_manifest.json"}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if bundle_out:
        bundle_out.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(bundle_out, "w:gz") as tar:
            tar.add(target, arcname="execution_engine/deploy/" + model_version)
            for path in [
                Path("execution_engine"),
                Path("early_trade_label"),
                Path("deploy"),
                Path("config.example.yaml"),
                Path("config.polymarket_live.example.yaml"),
                Path("requirements.txt"),
            ]:
                if path.exists():
                    tar.add(path, arcname=str(path), filter=_tar_filter)
    return manifest


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_hash_from_files(files: dict[str, str]) -> str:
    stable_files = {name: digest for name, digest in files.items() if name not in {"evaluation.json", "metrics.json"}}
    return sha256_text(json.dumps(stable_files, sort_keys=True))


def _inject_evaluation_export_metadata(path: Path, model_version: str, artifact_hash: str) -> None:
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    data["model_version"] = model_version
    data["artifact_hash"] = artifact_hash
    path.write_text(json.dumps(data, indent=2, allow_nan=True), encoding="utf-8")


def _metric_gates(metrics: dict[str, Any], prefix: str) -> dict[str, bool]:
    return {
        f"{prefix}_coverage_gte_0_70": float(metrics.get("coverage", 0.0)) >= 0.70,
        f"{prefix}_accepted_sample_accuracy_gt_0_80": float(metrics.get("accepted_sample_accuracy", 0.0)) > MIN_ACCEPTED_SAMPLE_ACCURACY,
    }


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copyfile(src, tmp)
    tmp.replace(dst)


def _csv_to_json(src: Path, dst: Path) -> None:
    with src.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    dst.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = set(Path(info.name).parts)
    if "__pycache__" in parts or "deploy2" in parts:
        return None
    if info.name == "execution_engine/artifacts" or info.name.startswith("execution_engine/artifacts/"):
        return None
    return info


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--deploy-dir", type=Path, default=Path("execution_engine/deploy"))
    parser.add_argument("--model-version")
    parser.add_argument("--maker-fill-table", type=Path)
    parser.add_argument("--bundle-out", type=Path)
    args = parser.parse_args()
    manifest = export_artifact(args.model_dir, args.deploy_dir, args.model_version, args.maker_fill_table, args.bundle_out)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

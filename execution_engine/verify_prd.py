from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import tarfile
from pathlib import Path
from typing import Any

from execution_engine.artifact_export import sha256_file, sha256_text


MIN_ACCEPTED_SAMPLE_ACCURACY = 0.80


REQUIRED_MANIFEST_KEYS = {
    "project",
    "model_version",
    "created_at_utc",
    "live_eligible",
    "deployment_status",
    "blocked_reasons",
    "model_gate_checks",
    "model_file",
    "feature_columns_file",
    "maker_fill_table_file",
    "thresholds",
    "validation_metrics",
    "trading",
    "files",
    "artifact_hash",
}

REQUIRED_FILL_COLUMNS = {
    "prediction_side",
    "decision_second_bucket",
    "current_price_bucket",
    "order_delay_seconds",
    "limit_price",
    "win_market_count",
    "win_fill_market_count",
    "a_win_market_fill",
    "lose_market_count",
    "lose_fill_market_count",
    "a_lose_market_fill",
    "fallback_level",
}


def verify_artifact(artifact_dir: Path, bundle: Path | None = None, require_live: bool = False) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    manifest_path = artifact_dir / "artifact_manifest.json"
    manifest = _read_json(manifest_path, findings)
    if not manifest:
        return _result(False, findings, {})

    _check_required_keys(manifest, REQUIRED_MANIFEST_KEYS, "manifest", findings)
    _check_project_contract(manifest, findings)
    _check_file_hashes(artifact_dir, manifest, findings)
    _check_model_gates(manifest, require_live, findings)
    _check_tuning_report(artifact_dir, manifest, findings)
    _check_trading_contract(manifest, findings)
    _check_fill_table(artifact_dir / str(manifest.get("maker_fill_table_file", "")), findings)
    _scan_artifact_secrets(artifact_dir, findings)
    if bundle:
        _check_bundle(bundle, findings)
    ok = not any(f["level"] == "error" for f in findings)
    return _result(ok, findings, manifest)


def _check_required_keys(data: dict[str, Any], required: set[str], label: str, findings: list[dict[str, str]]) -> None:
    for key in sorted(required - set(data)):
        findings.append({"level": "error", "item": f"{label}.{key}", "message": "missing required key"})


def _check_project_contract(manifest: dict[str, Any], findings: list[dict[str, str]]) -> None:
    if manifest.get("project") != "btc-polymarket-5m-maker":
        findings.append({"level": "error", "item": "manifest.project", "message": "unexpected project"})
    thresholds = manifest.get("thresholds") or {}
    if float(thresholds.get("min_coverage", 0.0)) != 0.70:
        findings.append({"level": "error", "item": "thresholds.min_coverage", "message": "must be 0.70"})
    if float(thresholds.get("min_accepted_sample_accuracy", 0.0)) != MIN_ACCEPTED_SAMPLE_ACCURACY:
        findings.append({"level": "error", "item": "thresholds.min_accepted_sample_accuracy", "message": "must be 0.80"})


def _check_file_hashes(artifact_dir: Path, manifest: dict[str, Any], findings: list[dict[str, str]]) -> None:
    files = manifest.get("files") or {}
    for name, expected in files.items():
        path = artifact_dir / name
        if not path.exists():
            findings.append({"level": "error", "item": f"files.{name}", "message": "listed file missing"})
            continue
        actual = sha256_file(path)
        if actual != expected:
            findings.append({"level": "error", "item": f"files.{name}", "message": "sha256 mismatch"})
    actual_artifact_hash = sha256_text(json.dumps(files, sort_keys=True))
    if manifest.get("artifact_hash") != actual_artifact_hash:
        findings.append({"level": "error", "item": "artifact_hash", "message": "artifact hash does not match file hash map"})


def _check_model_gates(manifest: dict[str, Any], require_live: bool, findings: list[dict[str, str]]) -> None:
    val = manifest.get("validation_metrics") or {}
    holdout = manifest.get("holdout_metrics") or {}
    policy = manifest.get("decision_policy") or {}
    gates = {
        "coverage_gte_0_70": float(val.get("coverage", 0.0)) >= 0.70,
        "accepted_sample_accuracy_gt_0_80": float(val.get("accepted_sample_accuracy", 0.0)) > MIN_ACCEPTED_SAMPLE_ACCURACY,
        "accepted_count_gte_1000": int(val.get("accepted_count", 0)) >= 1000,
        "up_prediction_count_gte_200": int(val.get("up_prediction_count", 0)) >= 200,
        "down_prediction_count_gte_200": int(val.get("down_prediction_count", 0)) >= 200,
        "coverage_constraint_satisfied": bool(policy.get("coverage_constraint_satisfied", False)),
    }
    if holdout:
        gates.update(
            {
                "holdout_coverage_gte_0_70": float(holdout.get("coverage", 0.0)) >= 0.70,
                "holdout_accepted_sample_accuracy_gt_0_80": float(holdout.get("accepted_sample_accuracy", 0.0)) > MIN_ACCEPTED_SAMPLE_ACCURACY,
            }
        )
    computed_live = all(gates.values())
    if bool(manifest.get("live_eligible")) != computed_live:
        findings.append({"level": "error", "item": "live_eligible", "message": "does not match computed model gates"})
    expected_status = "live_candidate" if computed_live else "paper_only_blocked"
    if manifest.get("deployment_status") != expected_status:
        findings.append({"level": "error", "item": "deployment_status", "message": f"expected {expected_status}"})
    expected_blocked = [name for name, passed in gates.items() if not passed]
    if sorted(manifest.get("blocked_reasons") or []) != sorted(expected_blocked):
        findings.append({"level": "error", "item": "blocked_reasons", "message": "does not match computed model gates"})
    manifest_gate_checks = manifest.get("model_gate_checks") or {}
    for name, passed in gates.items():
        if bool(manifest_gate_checks.get(name)) != passed:
            findings.append({"level": "error", "item": f"model_gate_checks.{name}", "message": "does not match computed gate"})
    for name, passed in gates.items():
        if not passed:
            level = "error" if require_live else "warning"
            findings.append({"level": level, "item": f"model_gate.{name}", "message": "gate not satisfied"})


def _check_trading_contract(manifest: dict[str, Any], findings: list[dict[str, str]]) -> None:
    trading = manifest.get("trading") or {}
    expected = {
        "max_total_budget_usdc": 7.0,
        "max_order_budget_usdc": 4.0,
        "min_shares": 5.0,
        "maker_only": True,
    }
    for key, value in expected.items():
        if trading.get(key) != value:
            findings.append({"level": "error", "item": f"trading.{key}", "message": f"expected {value!r}"})


def _check_tuning_report(artifact_dir: Path, manifest: dict[str, Any], findings: list[dict[str, str]]) -> None:
    path = artifact_dir / "tuning_report.json"
    if not path.exists():
        if manifest.get("live_eligible"):
            findings.append({"level": "warning", "item": "tuning_report", "message": "missing tuning report for live candidate"})
        else:
            findings.append({"level": "error", "item": "tuning_report", "message": "paper-only artifact must include tuning failure evidence"})
        return
    report = json.loads(path.read_text(encoding="utf-8-sig"))
    best = report.get("best_candidate") or {}
    if int(report.get("candidate_count", 0)) <= 0:
        findings.append({"level": "error", "item": "tuning_report.candidate_count", "message": "must evaluate at least one candidate"})
    if float(best.get("coverage", 0.0)) < 0.70:
        findings.append({"level": "error", "item": "tuning_report.best_candidate.coverage", "message": "best candidate does not meet coverage floor"})
    if bool(report.get("live_eligible")) != bool(manifest.get("live_eligible")):
        findings.append({"level": "error", "item": "tuning_report.live_eligible", "message": "does not match manifest"})
    if sorted(report.get("blocked_reasons") or []) != sorted(manifest.get("blocked_reasons") or []):
        findings.append({"level": "error", "item": "tuning_report.blocked_reasons", "message": "does not match manifest"})


def _check_fill_table(path: Path, findings: list[dict[str, str]]) -> None:
    if not path.exists():
        findings.append({"level": "error", "item": "maker_fill_table", "message": "file missing"})
        return
    if path.suffix.lower() != ".csv":
        findings.append({"level": "warning", "item": "maker_fill_table", "message": "verifier only inspects csv tables"})
        return
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_FILL_COLUMNS - columns
        if missing:
            findings.append({"level": "error", "item": "maker_fill_table.columns", "message": f"missing {sorted(missing)}"})
            return
        rows = list(reader)
    if not rows:
        findings.append({"level": "error", "item": "maker_fill_table.rows", "message": "table is empty"})
    sides = {r["prediction_side"].lower() for r in rows}
    if sides != {"up", "down"}:
        findings.append({"level": "error", "item": "maker_fill_table.prediction_side", "message": "must include up and down"})
    fallback_levels = {r["fallback_level"] for r in rows}
    if not fallback_levels:
        findings.append({"level": "error", "item": "maker_fill_table.fallback_level", "message": "missing fallback levels"})
    for idx, row in enumerate(rows, start=2):
        _check_fill_probability(row, idx, "win", findings)
        _check_fill_probability(row, idx, "lose", findings)


def _check_fill_probability(row: dict[str, str], line_no: int, outcome: str, findings: list[dict[str, str]]) -> None:
    count_key = f"{outcome}_market_count"
    fill_key = f"{outcome}_fill_market_count"
    prob_key = f"a_{outcome}_market_fill"
    try:
        count = int(float(row[count_key]))
        fill_count = int(float(row[fill_key]))
        probability = float(row[prob_key])
    except (KeyError, TypeError, ValueError):
        findings.append({"level": "error", "item": f"maker_fill_table.{prob_key}", "message": f"invalid numeric value at csv line {line_no}"})
        return
    if count < 0 or fill_count < 0 or fill_count > count:
        findings.append({"level": "error", "item": f"maker_fill_table.{fill_key}", "message": f"invalid counts at csv line {line_no}"})
        return
    expected = fill_count / count if count else 0.0
    if not math.isclose(probability, expected, rel_tol=1e-9, abs_tol=1e-9):
        findings.append({"level": "error", "item": f"maker_fill_table.{prob_key}", "message": f"does not equal {fill_key}/{count_key} at csv line {line_no}"})


def _check_bundle(bundle: Path, findings: list[dict[str, str]]) -> None:
    if not bundle.exists():
        findings.append({"level": "error", "item": "bundle", "message": "bundle missing"})
        return
    with tarfile.open(bundle, "r:gz") as tar:
        names = tar.getnames()
    required = {
        "config.example.yaml",
        "deploy/version3_deploy.ps1",
        "deploy/version3_rollback.ps1",
        "deploy/fortune-bot.service",
        "deploy/fortune-bot.timer",
        "execution_engine/observe_paper.py",
    }
    for item in sorted(required):
        if item not in names:
            findings.append({"level": "error", "item": f"bundle.{item}", "message": "missing from bundle"})
    banned = [n for n in names if "__pycache__" in n or "secrets.env" in n or n.startswith("execution_engine/artifacts")]
    if banned:
        findings.append({"level": "error", "item": "bundle.contents", "message": f"contains banned entries: {banned[:5]}"})
    _check_bundle_text_contracts(bundle, names, findings)


def _check_bundle_text_contracts(bundle: Path, names: list[str], findings: list[dict[str, str]]) -> None:
    with tarfile.open(bundle, "r:gz") as tar:
        config_text = _read_tar_text(tar, "config.example.yaml")
        service_text = _read_tar_text(tar, "deploy/fortune-bot.service")
        deploy_text = _read_tar_text(tar, "deploy/version3_deploy.ps1")
    if "mode: paper" not in config_text:
        findings.append({"level": "error", "item": "config.example.yaml.runtime.mode", "message": "default mode must be paper"})
    if "enabled: false" not in config_text:
        findings.append({"level": "error", "item": "config.example.yaml.orders.enabled", "message": "orders must default to disabled"})
    if "signature_type: 3" not in config_text:
        findings.append({"level": "error", "item": "config.example.yaml.polymarket.signature_type", "message": "deposit-wallet flow must default to POLY_1271/signature type 3"})
    if "funder_env: DEPOSIT_WALLET_ADDRESS" not in config_text:
        findings.append({"level": "error", "item": "config.example.yaml.polymarket.funder_env", "message": "deposit-wallet flow must use DEPOSIT_WALLET_ADDRESS"})
    if "--mode paper" not in service_text:
        findings.append({"level": "error", "item": "fortune-bot.service", "message": "systemd service must run paper mode by default"})
    for expected in ["verify_prd", "self_test", "preflight", "run_once --config config.yaml --mode paper", "observe_paper"]:
        if expected not in deploy_text:
            findings.append({"level": "error", "item": "version3_deploy.ps1", "message": f"missing deployment step: {expected}"})
    text_members = [n for n in names if n.endswith((".py", ".ps1", ".yaml", ".yml", ".json", ".service", ".timer", ".md"))]
    with tarfile.open(bundle, "r:gz") as tar:
        for name in text_members:
            text = _read_tar_text(tar, name)
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    findings.append({"level": "error", "item": f"bundle.secret_scan.{name}", "message": "possible secret literal"})
                    return


SECRET_PATTERNS = [
    re.compile(r"(?i)(private[_-]?key|clob[_-]?secret|clob[_-]?api[_-]?key|pass[_-]?phrase|deposit[_-]?wallet[_-]?address)\s*[:=]\s*['\"][^'\"]{12,}['\"]"),
    re.compile(r"(?i)(POLYMARKET_PRIVATE_KEY|CLOB_SECRET|CLOB_API_KEY|CLOB_PASS_PHRASE|DEPOSIT_WALLET_ADDRESS)\s*=\s*[^ \r\n]{12,}"),
]


def _scan_artifact_secrets(artifact_dir: Path, findings: list[dict[str, str]]) -> None:
    for path in artifact_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".json", ".csv", ".yaml", ".yml", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append({"level": "error", "item": f"artifact.secret_scan.{path.name}", "message": "possible secret literal"})
                return


def _read_tar_text(tar: tarfile.TarFile, name: str) -> str:
    try:
        member = tar.extractfile(name)
        if member is None:
            return ""
        return member.read().decode("utf-8", errors="ignore")
    except KeyError:
        return ""


def _read_json(path: Path, findings: list[dict[str, str]]) -> dict[str, Any]:
    if not path.exists():
        findings.append({"level": "error", "item": str(path), "message": "file missing"})
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _result(ok: bool, findings: list[dict[str, str]], manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": ok,
        "live_eligible": bool(manifest.get("live_eligible", False)),
        "model_version": manifest.get("model_version"),
        "artifact_hash": manifest.get("artifact_hash"),
        "findings": findings,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--require-live", action="store_true")
    args = parser.parse_args()
    result = verify_artifact(args.artifact_dir, args.bundle, args.require_live)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()

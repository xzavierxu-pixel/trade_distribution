from __future__ import annotations

import argparse
import base64
import json
import pickle
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from execution_engine.config import load_engine_config
from execution_engine.feature_source import load_feature_row
from execution_engine.idempotency import filter_new_orders, load_seen, mark_orders
from execution_engine.maker_order_plan import PlannerLimits, build_order_plan
from execution_engine.polymarket_adapter import PolymarketOrderRequest, submit_limit_buy_orders
from early_trade_label.threshold_search import apply_policy


def run_once(config_path: Path, mode: str | None = None, print_json: bool = False) -> dict[str, Any]:
    cfg = load_engine_config(config_path)
    runtime_mode = mode or cfg.runtime.mode
    artifact_dir = cfg.baseline.artifact_dir
    manifest = json.loads((artifact_dir / cfg.baseline.manifest_file).read_text(encoding="utf-8"))
    if runtime_mode == "live" and (not cfg.orders.enabled or not manifest.get("live_eligible", False)):
        raise RuntimeError("live mode requires orders.enabled=true and artifact_manifest.live_eligible=true")
    with (artifact_dir / manifest["model_file"]).open("rb") as f:
        model = pickle.load(f)
    feature_columns = json.loads((artifact_dir / manifest["feature_columns_file"]).read_text(encoding="utf-8"))
    row, feature_source = load_feature_row(cfg, manifest, feature_columns)
    x = pd.DataFrame([{col: row.get(col, 0.0) for col in feature_columns}])
    p_up = float(model.predict_proba(x)[:, 1][0])
    thresholds = manifest["thresholds"]
    pred = int(apply_policy(pd.Series([p_up]).to_numpy(), float(thresholds["t_up"]), float(thresholds["t_down"]))[0])
    decision = "abstain" if pred < 0 else ("up" if pred == 1 else "down")
    probability_reference = _read_json_if_exists(artifact_dir / str(manifest.get("probability_reference_file") or ""))
    fill_table_path = artifact_dir / manifest["maker_fill_table_file"]
    maker_fill_table = pd.read_parquet(fill_table_path) if fill_table_path.suffix.lower() == ".parquet" else pd.read_csv(fill_table_path)
    current_price = float(row.get(f"{decision}_token_last_price", 0.5)) if decision in {"up", "down"} else 0.5
    token_id = _token_id_for_side(row, decision)
    plan = {
        "prediction_side": decision,
        "q": None,
        "q_source": None,
        "candidate_orders": [],
        "selected_orders": [],
        "skip_reasons": {"model_abstained": 1},
    }
    if decision in {"up", "down"}:
        plan = build_order_plan(
            prediction_side=decision,
            p_up=p_up,
            current_price=current_price,
            maker_fill_table=maker_fill_table,
            validation_metrics=manifest["validation_metrics"],
            probability_reference=probability_reference,
            calibrated_probability_available=bool(manifest.get("calibrator_file")),
            limits=PlannerLimits(
                max_total_budget_usdc=cfg.orders.max_total_budget_usdc,
                max_order_budget_usdc=cfg.orders.max_order_budget_usdc,
                min_shares=cfg.orders.min_shares,
                max_orders_per_window=cfg.orders.max_orders_per_window,
                tick_size=cfg.orders.tick_size,
                min_price=cfg.orders.min_price,
                max_price=cfg.orders.max_price,
            ),
            market_key=str(row.get("condition_id", "unknown")),
        )
    store = load_seen(cfg.runtime.idempotency_store_path)
    selected_orders, duplicate_orders = filter_new_orders(plan["selected_orders"], store)
    if duplicate_orders:
        plan["skip_reasons"]["duplicate_order_key"] = len(duplicate_orders)
    polymarket_responses: list[dict[str, Any]] = []
    response_status = "paper_no_submit"
    if runtime_mode == "live" and selected_orders:
        live_requests = []
        for order in selected_orders:
            if not token_id:
                raise RuntimeError("live order requires token_id for selected side")
            live_requests.append(
                PolymarketOrderRequest(
                    token_id=token_id,
                    price=float(order["limit_price"]),
                    shares=float(order["shares"]),
                    order_key=str(order["order_key"]),
                )
            )
        polymarket_responses = submit_limit_buy_orders(cfg.polymarket, live_requests)
        response_status = "submitted" if polymarket_responses else "no_orders"
    mark_orders(cfg.runtime.idempotency_store_path, store, selected_orders, runtime_mode)
    summary = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "mode": runtime_mode,
        "model_version": manifest["model_version"],
        "artifact_hash": manifest.get("artifact_hash"),
        "market": {"condition_id": str(row.get("condition_id", "")), "market_start_ts": int(row.get("market_start_ts", 0))},
        "window": {"decision_second": 120},
        "features": {"feature_count": len(feature_columns), **feature_source},
        "p_up": p_up,
        "p_down": 1.0 - p_up,
        "thresholds": {"t_up": thresholds["t_up"], "t_down": thresholds["t_down"]},
        "decision": decision,
        "q": plan.get("q"),
        "q_source": plan.get("q_source"),
        "candidate_orders": plan["candidate_orders"],
        "selected_orders": selected_orders if runtime_mode == "paper" or cfg.orders.enabled else [],
        "duplicate_orders": duplicate_orders,
        "skip_reasons": plan["skip_reasons"] if cfg.orders.enabled or runtime_mode == "paper" else {"orders_disabled": 1},
        "polymarket_response_status": response_status,
        "polymarket_responses": polymarket_responses,
    }
    _append_audit_log(cfg.runtime.audit_log, summary)
    _write_summary(cfg.runtime.summary_dir, summary)
    if print_json:
        print(json.dumps(summary, indent=2))
    return summary


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if path.exists() and path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _token_id_for_side(row: pd.Series, decision: str) -> str | None:
    if decision == "up":
        return _clean_token(row.get("up_token_id") or row.get("_up_token_id"))
    if decision == "down":
        return _clean_token(row.get("down_token_id") or row.get("_down_token_id"))
    return None


def _clean_token(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _write_summary(summary_dir: Path, summary: dict[str, Any]) -> None:
    summary_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = summary_dir / f"summary_{stamp}.json"
    data = json.dumps(summary, indent=2)
    try:
        path.write_text(data, encoding="utf-8")
    except PermissionError as exc:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "execution_engine.write_json",
                    "--path",
                    str(path),
                    "--data-b64",
                    base64.b64encode(data.encode("utf-8")).decode("ascii"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as child_exc:
            if _write_summary_with_powershell(path, data):
                return
            fallback = Path("summary_" + path.name)
            try:
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import base64, pathlib, sys; pathlib.Path(sys.argv[1]).write_bytes(base64.b64decode(sys.argv[2].encode('ascii')))",
                        str(fallback),
                        base64.b64encode(data.encode("utf-8")).decode("ascii"),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                summary["summary_write_fallback"] = str(fallback)
            except Exception:
                summary["summary_write_error"] = str(exc)
        except Exception as child_exc:
            summary["summary_write_error"] = f"{exc}; child_writer={child_exc}"


def _write_summary_with_powershell(path: Path, data: str) -> bool:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "param($p,$b) [IO.File]::WriteAllBytes($p, [Convert]::FromBase64String($b))",
                str(path),
                base64.b64encode(data.encode("utf-8")).decode("ascii"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def _append_audit_log(path: Path, summary: dict[str, Any]) -> None:
    event = _build_audit_event(summary)
    line = json.dumps(event, separators=(",", ":")) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except PermissionError:
        return


def _build_audit_event(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp_utc": summary["timestamp_utc"],
        "model_version": summary["model_version"],
        "artifact_hash": summary["artifact_hash"],
        "market": summary["market"],
        "window": summary["window"],
        "feature_count": summary["features"]["feature_count"],
        "p_up": summary["p_up"],
        "p_down": summary["p_down"],
        "thresholds": summary["thresholds"],
        "decision": summary["decision"],
        "q": summary["q"],
        "q_source": summary["q_source"],
        "candidate_order_count": len(summary["candidate_orders"]),
        "selected_order_count": len(summary["selected_orders"]),
        "selected_orders": [
            {
                "order_key": order["order_key"],
                "prediction_side": order["prediction_side"],
                "limit_price": order["limit_price"],
                "shares": order["shares"],
                "budget_usdc": order["budget_usdc"],
                "edge": order["edge"],
                "maker_only": order["maker_only"],
                "fallback_level": order.get("fallback_level"),
            }
            for order in summary["selected_orders"]
        ],
        "skip_reasons": summary["skip_reasons"],
        "mode": summary["mode"],
        "polymarket_response_status": summary["polymarket_response_status"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--mode", choices=["paper", "live"])
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    run_once(args.config, args.mode, args.print_json)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from execution_engine.config import load_engine_config
from execution_engine.idempotency import filter_new_orders
from execution_engine.maker_order_plan import PlannerLimits, build_order_plan, floor_to_tick
from execution_engine.observe_paper import observe_paper_cycles
from execution_engine.preflight import run_preflight
from execution_engine.run_once import _build_audit_event
from execution_engine.verify_prd import verify_artifact


def run_self_test(artifact_dir: Path, bundle: Path | None, config: Path) -> dict[str, object]:
    manifest = json.loads((artifact_dir / "artifact_manifest.json").read_text(encoding="utf-8"))
    fill_path = artifact_dir / manifest["maker_fill_table_file"]
    fill_table = pd.read_csv(fill_path) if fill_path.suffix.lower() == ".csv" else pd.read_parquet(fill_path)

    paper_verify = verify_artifact(artifact_dir, bundle, require_live=False)
    live_verify = verify_artifact(artifact_dir, bundle, require_live=True)
    plan = build_order_plan(
        prediction_side="up",
        p_up=0.90,
        current_price=0.55,
        maker_fill_table=fill_table,
        validation_metrics=manifest["validation_metrics"],
        probability_reference=json.loads((artifact_dir / manifest["probability_reference_file"]).read_text(encoding="utf-8")),
        limits=PlannerLimits(),
        market_key="self_test_market",
    )
    fallback_plan = build_order_plan(
        prediction_side="up",
        p_up=0.90,
        current_price=0.55,
        maker_fill_table=fill_table,
        validation_metrics=manifest["validation_metrics"],
        probability_reference=None,
        limits=PlannerLimits(),
        market_key="self_test_market_fallback",
    )
    selected = plan["selected_orders"]
    duplicate_store = {"orders": {selected[0]["order_key"]: {}}} if selected else {"orders": {}}
    new_orders, duplicates = filter_new_orders(selected, duplicate_store)
    cfg = load_engine_config(config)
    live_guard_enabled = (not cfg.orders.enabled) and (not manifest["live_eligible"])
    audit_probe = _audit_probe()
    paper_preflight = run_preflight(config, "paper")
    live_preflight = run_preflight(config, "live")
    observe_probe = _observe_probe(config)

    checks = {
        "paper_verify_ok": bool(paper_verify["ok"]),
        "live_verify_blocks_current_artifact": not bool(live_verify["ok"]),
        "planner_selected_positive_ev": bool(selected) and all(o["edge"] > 0 for o in selected),
        "planner_uses_probability_bucket_q": plan["q_source"] == "probability_bucket_accuracy",
        "planner_falls_back_to_validation_q": fallback_plan["q"] == manifest["validation_metrics"]["accepted_sample_accuracy"] and fallback_plan["q_source"] == "validation_accepted_sample_accuracy",
        "planner_candidate_order_keys_unique": len({c["order_key"] for c in plan["candidate_orders"]}) == len(plan["candidate_orders"]),
        "planner_budget_limit": sum(o["budget_usdc"] for o in selected) <= 7.0 + 1e-9,
        "planner_order_budget_limit": all(o["budget_usdc"] <= 4.0 + 1e-9 for o in selected),
        "planner_min_shares": all(o["shares"] >= 5.0 for o in selected),
        "planner_tick_floor": floor_to_tick(0.579, 0.01) == 0.57,
        "idempotency_filters_duplicate": (len(duplicates) == 1 and len(new_orders) == max(len(selected) - 1, 0)) if selected else True,
        "live_guard_default_off": live_guard_enabled,
        "audit_event_shape": audit_probe,
        "paper_preflight_ok": bool(paper_preflight["ok"]),
        "live_preflight_blocks_current_artifact": not bool(live_preflight["ok"]),
        "observe_paper_one_cycle": observe_probe,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "selected_order_count": len(selected),
        "selected_budget_total": sum(o["budget_usdc"] for o in selected),
        "paper_verify_findings": paper_verify["findings"],
        "live_verify_findings": live_verify["findings"],
    }


def _audit_probe() -> bool:
    summary = {
        "timestamp_utc": "2026-01-01T00:00:00+00:00",
        "mode": "paper",
        "model_version": "self_test",
        "artifact_hash": "hash",
        "market": {"condition_id": "condition", "market_start_ts": 0},
        "window": {"decision_second": 120},
        "features": {"feature_count": 1, "source": "self_test"},
        "p_up": 0.9,
        "p_down": 0.1,
        "thresholds": {"t_up": 0.6, "t_down": 0.4},
        "decision": "up",
        "q": 0.9,
        "q_source": "calibrated_probability",
        "candidate_orders": [{}],
        "selected_orders": [
            {
                "order_key": "self_test",
                "prediction_side": "up",
                "limit_price": 0.5,
                "shares": 5.0,
                "budget_usdc": 2.5,
                "edge": 0.1,
                "maker_only": True,
                "fallback_level": "side_delay_price",
            }
        ],
        "skip_reasons": {},
        "polymarket_response_status": "paper_no_submit",
    }
    try:
        event = _build_audit_event(summary)
        required = {
            "timestamp_utc",
            "model_version",
            "artifact_hash",
            "market",
            "window",
            "feature_count",
            "p_up",
            "p_down",
            "thresholds",
            "decision",
            "q",
            "q_source",
            "candidate_order_count",
            "selected_order_count",
            "selected_orders",
            "skip_reasons",
            "mode",
            "polymarket_response_status",
        }
        return required.issubset(event) and "token_id" not in json.dumps(event).lower()
    except Exception:
        return False


def _observe_probe(config: Path) -> bool:
    try:
        result = observe_paper_cycles(config, cycles=1, sleep_seconds=0, print_json=False)
        return bool(result["ok"]) and result["cycles_completed"] == 1
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=Path("execution_engine/deploy/early_trade_label_v1"))
    parser.add_argument("--bundle", type=Path, default=Path("dist/fortune_bot_early_trade_label_v1.tar.gz"))
    parser.add_argument("--config", type=Path, default=Path("config.example.yaml"))
    args = parser.parse_args()
    result = run_self_test(args.artifact_dir, args.bundle, args.config)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()

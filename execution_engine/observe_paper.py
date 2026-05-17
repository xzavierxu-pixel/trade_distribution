from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from execution_engine.preflight import run_preflight
from execution_engine.prewarm import prewarm
from execution_engine.run_once import run_once


def observe_paper_cycles(config_path: Path, cycles: int = 3, sleep_seconds: int = 300, print_json: bool = False) -> dict[str, Any]:
    if cycles < 1:
        raise ValueError("cycles must be >= 1")
    preflight = run_preflight(config_path, "paper")
    if not preflight["ok"]:
        raise RuntimeError("paper preflight failed")
    runs = []
    for idx in range(cycles):
        prewarm_status = prewarm(config_path, print_json=False)
        summary = run_once(config_path, mode="paper", print_json=False)
        runs.append({
            "cycle": idx + 1,
            "prewarm": prewarm_status,
            "summary": {
                "timestamp_utc": summary["timestamp_utc"],
                "model_version": summary["model_version"],
                "artifact_hash": summary["artifact_hash"],
                "decision": summary["decision"],
                "candidate_order_count": len(summary["candidate_orders"]),
                "selected_order_count": len(summary["selected_orders"]),
                "skip_reasons": summary["skip_reasons"],
                "polymarket_response_status": summary["polymarket_response_status"],
            },
        })
        if idx < cycles - 1 and sleep_seconds > 0:
            time.sleep(sleep_seconds)
    result = {
        "ok": True,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "config": str(config_path),
        "cycles_requested": cycles,
        "cycles_completed": len(runs),
        "sleep_seconds": sleep_seconds,
        "runs": runs,
    }
    if print_json:
        print(json.dumps(result, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--sleep-seconds", type=int, default=300)
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    observe_paper_cycles(args.config, args.cycles, args.sleep_seconds, args.print_json)


if __name__ == "__main__":
    main()

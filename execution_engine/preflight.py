from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any

from execution_engine.config import load_engine_config


def run_preflight(config_path: Path, mode: str = "paper") -> dict[str, Any]:
    cfg = load_engine_config(config_path)
    manifest_path = cfg.baseline.artifact_dir / cfg.baseline.manifest_file
    checks: dict[str, bool] = {
        "config_exists": config_path.exists(),
        "manifest_exists": manifest_path.exists(),
        "mode_valid": mode in {"paper", "live"},
        "maker_only": bool(cfg.orders.maker_only),
        "budget_total_lte_7": cfg.orders.max_total_budget_usdc <= 7.0,
        "budget_order_lte_4": cfg.orders.max_order_budget_usdc <= 4.0,
        "min_shares_gte_5": cfg.orders.min_shares >= 5.0,
        "features_source_valid": cfg.features.source in {"validation_snapshot", "latest_feature_file", "trades_csv_snapshot", "polymarket_live"},
    }
    if cfg.features.source in {"latest_feature_file", "trades_csv_snapshot"}:
        checks["features_path_exists"] = bool(cfg.features.path and cfg.features.path.exists())
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        checks["artifact_hash_present"] = bool(manifest.get("artifact_hash"))
        checks["manifest_maker_only"] = bool((manifest.get("trading") or {}).get("maker_only"))
    else:
        checks["artifact_hash_present"] = False
        checks["manifest_maker_only"] = False

    credential_env = {
        "private_key": cfg.polymarket.private_key_env,
        "api_key": cfg.polymarket.api_key_env,
        "api_secret": cfg.polymarket.api_secret_env,
        "api_passphrase": cfg.polymarket.api_passphrase_env,
        "funder": cfg.polymarket.funder_env,
    }
    credential_presence = {name: bool(os.getenv(env_name)) for name, env_name in credential_env.items()}
    if mode == "live":
        checks["orders_enabled_for_live"] = bool(cfg.orders.enabled)
        checks["manifest_allows_live"] = bool(manifest.get("live_eligible"))
        checks["credentials_present_for_live"] = all(credential_presence.values())
        checks["signature_type_poly_1271"] = cfg.polymarket.signature_type == 3
        checks["deposit_wallet_env_used"] = cfg.polymarket.funder_env == "DEPOSIT_WALLET_ADDRESS"
        funder = os.getenv(cfg.polymarket.funder_env, "")
        checks["deposit_wallet_format_valid"] = bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", funder))
        checks["py_clob_client_available"] = importlib.util.find_spec("py_clob_client") is not None
        checks["py_builder_relayer_client_available"] = importlib.util.find_spec("py_builder_relayer_client") is not None
    else:
        checks["orders_disabled_by_default"] = not cfg.orders.enabled
        checks["runtime_mode_paper"] = cfg.runtime.mode == "paper"

    ok = all(checks.values())
    return {
        "ok": ok,
        "mode": mode,
        "config": str(config_path),
        "artifact_dir": str(cfg.baseline.artifact_dir),
        "model_version": manifest.get("model_version"),
        "artifact_hash": manifest.get("artifact_hash"),
        "live_eligible": bool(manifest.get("live_eligible", False)),
        "deployment_status": manifest.get("deployment_status"),
        "checks": checks,
        "credential_env_names": credential_env,
        "credential_presence": credential_presence,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    result = run_preflight(args.config, args.mode)
    if args.print_json:
        print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()

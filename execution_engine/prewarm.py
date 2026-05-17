from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from execution_engine.config import load_engine_config
from execution_engine.feature_source import load_feature_row


def prewarm(config_path: Path, print_json: bool = False) -> dict[str, object]:
    cfg = load_engine_config(config_path)
    manifest_path = cfg.baseline.artifact_dir / cfg.baseline.manifest_file
    status = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "artifact_dir": str(cfg.baseline.artifact_dir),
        "manifest_exists": manifest_path.exists(),
        "mode": cfg.runtime.mode,
        "orders_enabled": cfg.orders.enabled,
        "features_source": cfg.features.source,
        "features_path": str(cfg.features.path) if cfg.features.path else None,
    }
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing artifact manifest: {manifest_path}")
    if cfg.features.source == "polymarket_live":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        feature_columns = json.loads((cfg.baseline.artifact_dir / manifest["feature_columns_file"]).read_text(encoding="utf-8"))
        _, feature_status = load_feature_row(cfg, manifest, feature_columns)
        status["features_available"] = True
        status["features"] = feature_status
    if print_json:
        print(json.dumps(status, indent=2))
    return status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    prewarm(args.config, args.print_json)


if __name__ == "__main__":
    main()

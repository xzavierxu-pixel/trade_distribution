from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OrdersConfig:
    enabled: bool = False
    planner: str = "best_bid_ladder"
    kelly_fallback_enabled: bool = False
    best_bid_ladder_max_price: float = 0.80
    best_bid_ladder_second_offset: float = 0.10
    best_bid_ladder_shares: float = 5.0
    fractional_kelly: float = 0.25
    min_alpha_margin: float = 0.02
    min_q_margin: float = 0.02
    min_f_kelly: float = 0.005
    max_total_budget_usdc: float = 10.0
    max_order_budget_usdc: float = 4.0
    min_shares: float = 5.0
    max_orders_per_window: int = 3
    maker_only: bool = True
    tick_size: float = 0.01
    min_price: float = 0.01
    max_price: float = 0.99
    max_limit_price: float = 0.80
    min_market_count: int = 20
    allowed_fallback_levels: tuple[str, ...] = ("level_0", "level_1")


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str = "paper"
    audit_log: Path = Path("artifacts/logs/execution_engine/live.jsonl")
    summary_dir: Path = Path("artifacts/logs/execution_engine/summaries")
    idempotency_store_path: Path = Path("artifacts/state/execution_engine/idempotency.json")


@dataclass(frozen=True)
class FeaturesConfig:
    source: str = "validation_snapshot"
    path: Path | None = None
    feature_window_seconds: int = 120
    source_is_sell_only: bool = False


@dataclass(frozen=True)
class ThresholdsConfig:
    t_up: float | None = 0.51
    t_down: float | None = 0.49


@dataclass(frozen=True)
class BaselineConfig:
    artifact_dir: Path
    manifest_file: str = "artifact_manifest.json"


@dataclass(frozen=True)
class PolymarketConfig:
    host: str = "https://clob.polymarket.com"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    data_api_url: str = "https://data-api.polymarket.com"
    chain_id: int = 137
    signature_type: int = 1
    private_key_env: str = "POLYMARKET_PRIVATE_KEY"
    api_key_env: str = "CLOB_API_KEY"
    api_secret_env: str = "CLOB_SECRET"
    api_passphrase_env: str = "CLOB_PASS_PHRASE"
    funder_env: str = "POLYMARKET_FUNDER"


@dataclass(frozen=True)
class EngineConfig:
    baseline: BaselineConfig
    runtime: RuntimeConfig
    features: FeaturesConfig = FeaturesConfig()
    thresholds: ThresholdsConfig = ThresholdsConfig()
    orders: OrdersConfig = OrdersConfig()
    polymarket: PolymarketConfig = PolymarketConfig()


def load_engine_config(path: str | Path) -> EngineConfig:
    raw = _load_raw(Path(path))
    baseline = raw.get("baseline", {})
    runtime = raw.get("runtime", {})
    features = raw.get("features", {})
    thresholds = raw.get("thresholds", {})
    orders = raw.get("orders", {})
    polymarket = raw.get("polymarket", {})
    if "artifact_dir" not in baseline:
        raise ValueError("config must set baseline.artifact_dir")
    return EngineConfig(
        baseline=BaselineConfig(
            artifact_dir=Path(str(baseline["artifact_dir"])),
            manifest_file=str(baseline.get("manifest_file", "artifact_manifest.json")),
        ),
        runtime=RuntimeConfig(
            mode=str(runtime.get("mode", "paper")),
            audit_log=Path(str(runtime.get("audit_log", "artifacts/logs/execution_engine/live.jsonl"))),
            summary_dir=Path(str(runtime.get("summary_dir", "artifacts/logs/execution_engine/summaries"))),
            idempotency_store_path=Path(str(runtime.get("idempotency_store_path", "artifacts/state/execution_engine/idempotency.json"))),
        ),
        features=FeaturesConfig(
            source=str(features.get("source", "validation_snapshot")),
            path=Path(str(features["path"])) if features.get("path") else None,
            feature_window_seconds=int(features.get("feature_window_seconds", 120)),
            source_is_sell_only=bool(features.get("source_is_sell_only", False)),
        ),
        thresholds=ThresholdsConfig(
            t_up=float(thresholds["t_up"]) if thresholds.get("t_up") is not None else None,
            t_down=float(thresholds["t_down"]) if thresholds.get("t_down") is not None else None,
        ),
        orders=OrdersConfig(
            enabled=bool(orders.get("enabled", False)),
            planner=str(orders.get("planner", "best_bid_ladder")),
            kelly_fallback_enabled=bool(orders.get("kelly_fallback_enabled", False)),
            best_bid_ladder_max_price=float(orders.get("best_bid_ladder_max_price", 0.80)),
            best_bid_ladder_second_offset=float(orders.get("best_bid_ladder_second_offset", 0.10)),
            best_bid_ladder_shares=float(orders.get("best_bid_ladder_shares", 5.0)),
            fractional_kelly=float(orders.get("fractional_kelly", 0.25)),
            min_alpha_margin=float(orders.get("min_alpha_margin", 0.02)),
            min_q_margin=float(orders.get("min_q_margin", 0.02)),
            min_f_kelly=float(orders.get("min_f_kelly", 0.005)),
            max_total_budget_usdc=float(orders.get("max_total_budget_usdc", 10.0)),
            max_order_budget_usdc=float(orders.get("max_order_budget_usdc", 4.0)),
            min_shares=float(orders.get("min_shares", 5.0)),
            max_orders_per_window=int(orders.get("max_orders_per_window", 3)),
            maker_only=bool(orders.get("maker_only", True)),
            tick_size=float(orders.get("tick_size", 0.01)),
            min_price=float(orders.get("min_price", 0.01)),
            max_price=float(orders.get("max_price", 0.99)),
            max_limit_price=float(orders.get("max_limit_price", 0.80)),
            min_market_count=int(orders.get("min_market_count", 20)),
            allowed_fallback_levels=tuple(orders.get("allowed_fallback_levels", ["level_0", "level_1"])),
        ),
        polymarket=PolymarketConfig(
            host=str(polymarket.get("host", "https://clob.polymarket.com")),
            gamma_base_url=str(polymarket.get("gamma_base_url", "https://gamma-api.polymarket.com")),
            data_api_url=str(polymarket.get("data_api_url", "https://data-api.polymarket.com")),
            chain_id=int(polymarket.get("chain_id", 137)),
            signature_type=int(polymarket.get("signature_type", 1)),
            private_key_env=str(polymarket.get("private_key_env", "POLYMARKET_PRIVATE_KEY")),
            api_key_env=str(polymarket.get("api_key_env", "CLOB_API_KEY")),
            api_secret_env=str(polymarket.get("api_secret_env", "CLOB_SECRET")),
            api_passphrase_env=str(polymarket.get("api_passphrase_env", "CLOB_PASS_PHRASE")),
            funder_env=str(polymarket.get("funder_env", "POLYMARKET_FUNDER")),
        ),
    )


def _load_raw(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        return data or {}
    except Exception:
        return _minimal_yaml(text)


def _minimal_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, data)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        key = key.strip()
        value = value.strip()
        if not value:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _coerce(value)
    return data


def _coerce(value: str) -> Any:
    value = value.strip().strip('"').strip("'")
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.lower() in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value

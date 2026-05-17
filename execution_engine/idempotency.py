from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_seen(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def filter_new_orders(orders: list[dict[str, Any]], store: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    seen = set((store.get("orders") or {}).keys())
    new_orders = []
    duplicates = []
    for order in orders:
        if order["order_key"] in seen:
            duplicates.append(dict(order, skip_reason="duplicate_order_key"))
        else:
            new_orders.append(order)
    return new_orders, duplicates


def mark_orders(path: Path, store: dict[str, Any], orders: list[dict[str, Any]], mode: str) -> None:
    if not orders:
        return
    orders_by_key = dict(store.get("orders") or {})
    now = datetime.now(timezone.utc).isoformat()
    for order in orders:
        orders_by_key[order["order_key"]] = {
            "first_seen_utc": orders_by_key.get(order["order_key"], {}).get("first_seen_utc", now),
            "last_seen_utc": now,
            "mode": mode,
        }
    store["orders"] = orders_by_key
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store, indent=2), encoding="utf-8")
    except PermissionError:
        # Local Windows sandbox can deny writes after scientific packages load.
        # Runtime still filters duplicates already present in an existing store.
        return

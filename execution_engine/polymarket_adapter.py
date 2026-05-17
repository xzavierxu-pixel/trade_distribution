from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from execution_engine.config import PolymarketConfig


@dataclass(frozen=True)
class PolymarketOrderRequest:
    token_id: str
    price: float
    shares: float
    order_key: str


def submit_limit_buy_orders(config: PolymarketConfig, orders: list[PolymarketOrderRequest]) -> list[dict[str, Any]]:
    if not orders:
        return []
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
    except Exception as exc:  # pragma: no cover - depends on deployment env
        raise RuntimeError("py-clob-client is required for live order submission") from exc

    private_key = _required_env(config.private_key_env)
    creds = ApiCreds(
        api_key=_required_env(config.api_key_env),
        api_secret=_required_env(config.api_secret_env),
        api_passphrase=_required_env(config.api_passphrase_env),
    )
    funder = os.getenv(config.funder_env)
    client = ClobClient(
        config.host,
        chain_id=config.chain_id,
        key=private_key,
        creds=creds,
        signature_type=config.signature_type,
        funder=funder,
    )
    responses = []
    for order in orders:
        args = OrderArgs(token_id=order.token_id, price=order.price, size=order.shares, side="BUY")
        signed = client.create_order(args)
        response = client.post_order(signed, OrderType.GTC, post_only=True)
        responses.append({"order_key": order.order_key, "status": "submitted", "response": _safe_response(response)})
    return responses


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _safe_response(response: Any) -> Any:
    if isinstance(response, dict):
        return {k: v for k, v in response.items() if "secret" not in str(k).lower() and "key" not in str(k).lower()}
    return str(response)


from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class PlannerLimits:
    max_total_budget_usdc: float = 7.0
    max_order_budget_usdc: float = 4.0
    min_shares: float = 5.0
    max_orders_per_window: int = 3
    tick_size: float = 0.01
    min_price: float = 0.01
    max_price: float = 0.99


def edge(q: float, a_win: float, price: float, b_lose: float = 1.0) -> float:
    if price <= 0:
        return float("-inf")
    return float(q * a_win * (1.0 / price - 1.0) - (1.0 - q) * b_lose)


def floor_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    decimals = max(0, int(round(-math.log10(tick_size)))) if tick_size < 1 else 0
    return round(math.floor((price + 1e-12) / tick_size) * tick_size, decimals)


def choose_q(
    side: str,
    p_up: float,
    probability_reference: dict[str, Any] | None,
    validation_metrics: dict[str, Any],
    calibrated_probability_available: bool = False,
) -> tuple[float, str]:
    p_side = p_up if side == "up" else 1.0 - p_up
    if calibrated_probability_available and 0.0 <= p_side <= 1.0:
        return float(p_side), "calibrated_probability"
    if probability_reference:
        bucket_q = q_from_probability_bucket(side, p_up, probability_reference)
        if bucket_q is not None:
            return bucket_q, "probability_bucket_accuracy"
    return float(validation_metrics["accepted_sample_accuracy"]), "validation_accepted_sample_accuracy"


def q_from_probability_bucket(side: str, p_up: float, probability_reference: dict[str, Any]) -> float | None:
    buckets = probability_reference.get("accepted_accuracy_by_bucket") or []
    if not buckets:
        return None
    matched = None
    for bucket in buckets:
        lo = float(bucket.get("p_up_min", float("-inf")))
        hi = float(bucket.get("p_up_max", float("inf")))
        if lo <= p_up <= hi:
            matched = bucket
            break
    if matched is None:
        matched = min(
            buckets,
            key=lambda b: min(
                abs(float(b.get("p_up_min", 0.0)) - p_up),
                abs(float(b.get("p_up_max", 1.0)) - p_up),
            ),
        )
    accepted_accuracy = matched.get("accepted_accuracy")
    if accepted_accuracy is not None and not pd.isna(accepted_accuracy):
        return float(accepted_accuracy)
    actual_up_rate = matched.get("actual_up_rate")
    if actual_up_rate is None or pd.isna(actual_up_rate):
        return None
    actual_up = float(actual_up_rate)
    return actual_up if side == "up" else 1.0 - actual_up


def build_order_plan(
    *,
    prediction_side: str,
    p_up: float,
    current_price: float,
    maker_fill_table: pd.DataFrame,
    validation_metrics: dict[str, Any],
    probability_reference: dict[str, Any] | None = None,
    calibrated_probability_available: bool = False,
    decision_second: int = 120,
    limits: PlannerLimits = PlannerLimits(),
    market_key: str = "unknown",
) -> dict[str, Any]:
    side = prediction_side.lower()
    q, q_source = choose_q(side, p_up, probability_reference, validation_metrics, calibrated_probability_available)
    price_bucket = bucket_price(current_price)
    decision_bucket = bucket_second(decision_second)
    rows = lookup_rows(maker_fill_table, side, decision_bucket, price_bucket)
    candidates = []
    for row in rows:
        raw_price = float(row["limit_price"])
        price = floor_to_tick(raw_price, limits.tick_size)
        if not limits.min_price <= price <= limits.max_price:
            continue
        min_budget = limits.min_shares * price
        if min_budget > limits.max_order_budget_usdc:
            continue
        a_win = float(row["a_win_market_fill"])
        order_edge = edge(q, a_win, price)
        key = f"{market_key}:{side}:{int(row['order_delay_seconds'])}:{price:.4f}"
        candidates.append(
            {
                "order_key": key,
                "prediction_side": side,
                "decision_second_bucket": str(row["decision_second_bucket"]),
                "current_price_bucket": str(row["current_price_bucket"]),
                "order_delay_seconds": int(row["order_delay_seconds"]),
                "limit_price": round(price, 4),
                "raw_limit_price": raw_price,
                "a_win_market_fill": a_win,
                "a_lose_market_fill": float(row.get("a_lose_market_fill", 1.0)),
                "fallback_level": str(row.get("fallback_level", "unknown")),
                "q": q,
                "q_source": q_source,
                "edge": order_edge,
                "min_budget": min_budget,
                "skip_reason": None if order_edge > 0 else "non_positive_edge",
            }
        )
    positive = [c for c in candidates if c["edge"] > 0]
    selected = allocate_budget(positive, limits)
    return {
        "prediction_side": side,
        "q": q,
        "q_source": q_source,
        "decision_second_bucket": decision_bucket,
        "current_price_bucket": price_bucket,
        "candidate_orders": candidates,
        "selected_orders": selected,
        "skip_reasons": summarize_skips(candidates, selected),
    }


def lookup_rows(table: pd.DataFrame, side: str, decision_bucket: str, price_bucket: str) -> list[dict[str, Any]]:
    side_rows = table[table["prediction_side"].astype(str).str.lower().eq(side)].copy()
    exact = side_rows[
        side_rows["decision_second_bucket"].astype(str).eq(decision_bucket)
        & side_rows["current_price_bucket"].astype(str).eq(price_bucket)
    ]
    if not exact.empty:
        return dedupe_candidate_rows(exact.to_dict("records"))
    fallback = side_rows[side_rows["fallback_level"].astype(str).isin(["side_delay_price", "global"])]
    return dedupe_candidate_rows(fallback.to_dict("records"))


def dedupe_candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[int, float], dict[str, Any]] = {}
    for row in rows:
        key = (int(row["order_delay_seconds"]), float(row["limit_price"]))
        previous = best.get(key)
        if previous is None or _row_rank(row) > _row_rank(previous):
            best[key] = row
    return sorted(best.values(), key=lambda r: (int(r["order_delay_seconds"]), float(r["limit_price"])))


def _row_rank(row: dict[str, Any]) -> tuple[int, int, float]:
    fallback_rank = {"fine": 3, "side_delay_price": 2, "global": 1}.get(str(row.get("fallback_level")), 0)
    is_any_bucket = int(str(row.get("decision_second_bucket")) == "any" and str(row.get("current_price_bucket")) == "any")
    return (fallback_rank, is_any_bucket, float(row.get("win_market_count", 0)) + float(row.get("lose_market_count", 0)))


def allocate_budget(candidates: list[dict[str, Any]], limits: PlannerLimits) -> list[dict[str, Any]]:
    ordered = sorted(candidates, key=lambda c: c["edge"], reverse=True)
    best: list[dict[str, Any]] = []
    best_ev = 0.0
    max_n = min(limits.max_orders_per_window, len(ordered))
    for n in range(1, max_n + 1):
        for combo in itertools.combinations(ordered, n):
            min_total = sum(c["min_budget"] for c in combo)
            if min_total > limits.max_total_budget_usdc:
                continue
            remaining = limits.max_total_budget_usdc - min_total
            allocated = [dict(c, budget_usdc=c["min_budget"]) for c in combo]
            for item in sorted(allocated, key=lambda c: c["edge"], reverse=True):
                add = min(remaining, limits.max_order_budget_usdc - item["budget_usdc"])
                if add > 0:
                    item["budget_usdc"] += add
                    remaining -= add
                if remaining <= 1e-9:
                    break
            total_ev = sum(item["budget_usdc"] * item["edge"] for item in allocated)
            if total_ev > best_ev:
                best_ev = total_ev
                best = allocated
    for item in best:
        item["shares"] = item["budget_usdc"] / item["limit_price"]
        item["expected_value_usdc"] = item["budget_usdc"] * item["edge"]
        item["maker_only"] = True
        item["order_type"] = "limit_buy"
    return best


def summarize_skips(candidates: list[dict[str, Any]], selected: list[dict[str, Any]]) -> dict[str, int]:
    selected_keys = {s["order_key"] for s in selected}
    reasons: dict[str, int] = {}
    for candidate in candidates:
        reason = candidate.get("skip_reason")
        if not reason and candidate["order_key"] not in selected_keys:
            reason = "not_selected_by_budget_optimizer"
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    if not selected:
        reasons["no_positive_ev_order"] = reasons.get("no_positive_ev_order", 0) + 1
    return reasons


def bucket_second(second: int) -> str:
    if second < 90:
        return "lt90"
    if second < 120:
        return "90_119"
    if second < 150:
        return "120_149"
    return "150_plus"


def bucket_price(price: float) -> str:
    lo = max(0, min(95, int(math.floor(price / 0.05)) * 5))
    hi = lo + 5
    return f"{lo / 100:.2f}_{hi / 100:.2f}"

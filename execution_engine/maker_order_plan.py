from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class PlannerLimits:
    max_total_budget_usdc: float = 10.0
    max_order_budget_usdc: float = 4.0
    min_shares: float = 5.0
    max_orders_per_window: int = 3
    fractional_kelly: float = 0.25
    min_alpha_margin: float = 0.02
    min_q_margin: float = 0.02
    min_f_kelly: float = 0.005
    tick_size: float = 0.01
    min_price: float = 0.01
    max_price: float = 0.99
    max_limit_price: float = 0.80
    min_market_count: int = 20
    allowed_fallback_levels: tuple[str, ...] = ("level_0", "level_1")


@dataclass(frozen=True)
class BestBidLadderConfig:
    max_price: float = 0.80
    second_offset: float = 0.10
    shares: float = 5.0
    tick_size: float = 0.01
    min_price: float = 0.01


def build_best_bid_ladder_plan(
    *,
    prediction_side: str,
    best_bid: float | None,
    current_price: float,
    market_key: str = "unknown",
    config: BestBidLadderConfig = BestBidLadderConfig(),
) -> dict[str, Any]:
    side = prediction_side.lower()
    if best_bid is None or pd.isna(best_bid):
        return {
            "prediction_side": side,
            "q": None,
            "q_source": None,
            "decision_second_bucket": None,
            "current_price_bucket": bucket_price(current_price),
            "candidate_orders": [],
            "selected_orders": [],
            "skip_reasons": {"best_bid_unavailable": 1},
        }
    first_price = min(float(best_bid), config.max_price)
    raw_prices = [first_price, first_price - config.second_offset]
    candidates: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    skip_reasons: dict[str, int] = {}
    for idx, raw_price in enumerate(raw_prices, start=1):
        price = floor_to_tick(raw_price, config.tick_size)
        order_key = f"{market_key}:{side}:best_bid_ladder:{idx}:{price:.4f}"
        candidate = {
            "order_key": order_key,
            "prediction_side": side,
            "planner": "best_bid_ladder",
            "ladder_level": idx,
            "best_bid": float(best_bid),
            "current_price": float(current_price),
            "limit_price_anchor": round(price, 4),
            "raw_limit_price": raw_price,
            "shares": float(config.shares),
            "budget_usdc": float(config.shares * price),
            "maker_only": True,
            "order_type": "limit_buy",
        }
        if price < config.min_price:
            candidate["skip_reason"] = "price_below_min"
            skip_reasons["price_below_min"] = skip_reasons.get("price_below_min", 0) + 1
        elif config.shares <= 0:
            candidate["skip_reason"] = "non_positive_shares"
            skip_reasons["non_positive_shares"] = skip_reasons.get("non_positive_shares", 0) + 1
        else:
            candidate["skip_reason"] = None
            selected.append(dict(candidate))
        candidates.append(candidate)
    if not selected:
        skip_reasons["no_best_bid_ladder_order"] = skip_reasons.get("no_best_bid_ladder_order", 0) + 1
    return {
        "prediction_side": side,
        "q": None,
        "q_source": None,
        "decision_second_bucket": None,
        "current_price_bucket": bucket_price(current_price),
        "candidate_orders": candidates,
        "selected_orders": selected,
        "skip_reasons": skip_reasons,
    }


def kelly_metrics(q: float, a_win: float, price: float) -> dict[str, float]:
    if price <= 0 or price >= 1:
        return {
            "R_payoff": float("inf"),
            "edge": float("-inf"),
            "q_required": float("inf"),
            "q_margin": float("-inf"),
            "f_kelly_raw": float("-inf"),
            "f_kelly": 0.0,
        }
    r_payoff = (1.0 - price) / price
    edge = q * a_win * r_payoff - (1.0 - q)
    q_required = price / (price + a_win * (1.0 - price)) if (price + a_win * (1.0 - price)) > 0 else float("inf")
    denom = r_payoff * (q * a_win + 1.0 - q)
    f_raw = edge / denom if denom > 0 else float("-inf")
    return {
        "R_payoff": float(r_payoff),
        "edge": float(edge),
        "q_required": float(q_required),
        "q_margin": float(q - q_required),
        "f_kelly_raw": float(f_raw),
        "f_kelly": float(max(0.0, f_raw)),
    }


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
    del probability_reference, validation_metrics
    q_model = p_up if side == "up" else 1.0 - p_up
    price_bucket = bucket_price(current_price)
    decision_bucket = bucket_second(decision_second)
    rows = lookup_rows(maker_fill_table, side, decision_bucket, price_bucket, limits.allowed_fallback_levels)
    candidates = []
    for row in rows:
        raw_price = float(row["limit_price_anchor"])
        price = floor_to_tick(raw_price, limits.tick_size)
        if not limits.min_price <= price <= min(limits.max_price, limits.max_limit_price):
            continue
        if price >= current_price:
            continue
        if not _boolish(row.get("is_valid_maker_candidate", True)):
            continue
        market_count = int(float(row.get("win_market_count", 0))) + int(float(row.get("lose_market_count", 0)))
        if market_count < limits.min_market_count:
            continue
        if not _boolish(row.get("is_reliable", True)):
            continue
        a_win_raw = float(row["a_win_market_fill"])
        a_win = float(row.get("a_win_LCB", a_win_raw))
        q_market = float(row.get("q_market", 0.0))
        q_market_lcb = float(row.get("q_market_LCB", q_market))
        q_used = q_market_lcb
        q_source = "q_market_LCB"
        if calibrated_probability_available and q_model > q_market_lcb + limits.min_alpha_margin:
            q_used = float(q_model)
            q_source = "q_model_lower_bound"
        metrics = kelly_metrics(q_used, a_win, price)
        order_edge = float(row.get("edge_market_q", metrics["edge"])) if q_source == "q_market_LCB" else metrics["edge"]
        q_margin = float(row.get("q_margin", metrics["q_margin"])) if q_source == "q_market_LCB" else metrics["q_margin"]
        f_kelly = float(row.get("f_kelly", metrics["f_kelly"])) if q_source == "q_market_LCB" else metrics["f_kelly"]
        if q_margin < limits.min_q_margin or f_kelly < limits.min_f_kelly:
            continue
        min_budget = limits.min_shares * price
        if min_budget > limits.max_order_budget_usdc:
            continue
        key = f"{market_key}:{side}:{decision_bucket}:{price:.4f}"
        candidates.append(
            {
                "order_key": key,
                "prediction_side": side,
                "decision_second_bucket": str(row.get("decision_time_regime", row.get("decision_second_bucket"))),
                "decision_time_regime": str(row.get("decision_time_regime", row.get("decision_second_bucket"))),
                "current_price_bucket": str(row["current_price_bucket"]),
                "limit_price_anchor": round(price, 4),
                "raw_limit_price": raw_price,
                "a_win_market_fill": a_win_raw,
                "a_win_raw": a_win_raw,
                "a_win_LCB": float(row.get("a_win_LCB", a_win)),
                "kelly_a_win_used": a_win,
                "a_lose_market_fill": float(row.get("a_lose_market_fill", 0.0)),
                "a_lose_assumption": 1.0,
                "fallback_level": str(row.get("fallback_level", "unknown")),
                "win_market_count": int(float(row.get("win_market_count", 0))),
                "lose_market_count": int(float(row.get("lose_market_count", 0))),
                "sample_market_count": market_count,
                "q_market": q_market,
                "q_market_LCB": q_market_lcb,
                "q_model": float(q_model),
                "q_used": q_used,
                "q": q_used,
                "q_source": q_source,
                "q_required": metrics["q_required"],
                "q_margin": q_margin,
                "R_payoff": metrics["R_payoff"],
                "edge": order_edge,
                "f_kelly_raw": metrics["f_kelly_raw"],
                "f_kelly": f_kelly,
                "min_budget": min_budget,
                "skip_reason": None if order_edge > 0 else "non_positive_ev",
            }
        )
    positive = [c for c in candidates if c["edge"] > 0]
    selected = allocate_budget(positive, limits)
    return {
        "prediction_side": side,
        "q": None,
        "q_source": "per_candidate_q_market_LCB",
        "decision_second_bucket": decision_bucket,
        "current_price_bucket": price_bucket,
        "candidate_orders": candidates,
        "selected_orders": selected,
        "skip_reasons": summarize_skips(candidates, selected),
    }


def lookup_rows(table: pd.DataFrame, side: str, decision_bucket: str, price_bucket: str, allowed_fallback_levels: tuple[str, ...] = ("level_0", "level_1")) -> list[dict[str, Any]]:
    side_rows = table.copy()
    if "prediction_side" in side_rows.columns:
        side_rows = side_rows[side_rows["prediction_side"].astype(str).str.lower().eq(side)].copy()
    decision_col = "decision_time_regime" if "decision_time_regime" in side_rows.columns else "decision_second_bucket"
    exact = side_rows[side_rows[decision_col].astype(str).eq(decision_bucket) & side_rows["current_price_bucket"].astype(str).eq(price_bucket)]
    if not exact.empty:
        return dedupe_candidate_rows(exact.to_dict("records"))
    fallback = side_rows[side_rows["fallback_level"].astype(str).isin(allowed_fallback_levels)]
    return dedupe_candidate_rows(fallback.to_dict("records"))


def dedupe_candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[float, dict[str, Any]] = {}
    for row in rows:
        key = float(row["limit_price_anchor"])
        previous = best.get(key)
        if previous is None or _row_rank(row) > _row_rank(previous):
            best[key] = row
    return sorted(best.values(), key=lambda r: float(r["limit_price_anchor"]))


def _row_rank(row: dict[str, Any]) -> tuple[int, int, float]:
    fallback_rank = {
        "level_0": 8,
        "level_1": 7,
        "level_2": 6,
        "level_3": 5,
        "level_4": 4,
        "level_5": 1,
    }.get(str(row.get("fallback_level")), 0)
    is_any_bucket = int(str(row.get("decision_time_regime", row.get("decision_second_bucket"))) == "any" and str(row.get("current_price_bucket")) == "any")
    return (fallback_rank, is_any_bucket, float(row.get("win_market_count", 0)) + float(row.get("lose_market_count", 0)))


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def allocate_budget(candidates: list[dict[str, Any]], limits: PlannerLimits) -> list[dict[str, Any]]:
    ordered = sorted(candidates, key=lambda c: c["f_kelly"], reverse=True)
    selected: list[dict[str, Any]] = []
    remaining = limits.max_total_budget_usdc
    for candidate in ordered:
        if len(selected) >= limits.max_orders_per_window or remaining <= 0:
            break
        raw_budget = limits.max_total_budget_usdc * limits.fractional_kelly * candidate["f_kelly"]
        capped_budget = min(raw_budget, limits.max_order_budget_usdc, remaining)
        shares = math.floor(capped_budget / candidate["limit_price_anchor"])
        if shares < limits.min_shares:
            continue
        budget = shares * candidate["limit_price_anchor"]
        if budget > remaining + 1e-9:
            continue
        item = dict(candidate, budget_usdc=budget, shares=float(shares))
        selected.append(item)
        remaining -= budget
    for item in selected:
        item["expected_value_usdc"] = item["budget_usdc"] * item["edge"]
        item["kelly_budget_usdc"] = limits.max_total_budget_usdc * limits.fractional_kelly * item["f_kelly"]
        item["fractional_kelly"] = limits.fractional_kelly
        item["maker_only"] = True
        item["order_type"] = "limit_buy"
    return selected


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
    start = min(270, max(0, int(second // 30) * 30))
    end = min(start + 30, 300)
    right = "]" if end >= 300 else ")"
    return f"[{start},{end}{right}"


def bucket_price(price: float) -> str:
    lo = max(0, min(95, int(math.floor(price / 0.05)) * 5))
    hi = min(lo + 5, 100)
    right = "]" if hi >= 100 else ")"
    return f"[{lo / 100:.2f},{hi / 100:.2f}{right}"

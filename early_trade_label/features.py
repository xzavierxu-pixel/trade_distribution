from __future__ import annotations

import numpy as np
import pandas as pd


BUCKETS = [(0, 15), (15, 30), (30, 60), (60, 90), (90, 120)]


def safe_div(num: float, den: float, default: float = 0.0) -> float:
    return float(num / den) if den else default


def weighted_avg(values: pd.Series, weights: pd.Series, default: float = 0.5) -> float:
    den = float(weights.sum())
    return safe_div(float((values * weights).sum()), den, default)


def build_market_features(events: pd.DataFrame, market_start_ts: int, source_is_sell_only: bool) -> dict[str, float]:
    out: dict[str, float] = {
        "market_start_ts": float(market_start_ts),
        "source_is_sell_only": float(source_is_sell_only),
        "early_has_trade": float(not events.empty),
    }
    _calendar_features(out, market_start_ts)
    if events.empty:
        _fill_empty(out)
        return out

    events = events.sort_values("second_from_start").copy()
    events["notional"] = events["price"] * events["size"]
    out.update(
        early_trade_count=float(len(events)),
        early_size_sum=float(events["size"].sum()),
        early_notional_sum=float(events["notional"].sum()),
        early_avg_price_size_weighted=weighted_avg(events["price"], events["size"]),
        early_first_trade_second=float(events["second_from_start"].min()),
        early_last_trade_second=float(events["second_from_start"].max()),
        early_active_seconds=float(events["second_from_start"].nunique()),
        early_unique_tx_count=float(events.get("transaction_hash", pd.Series(dtype=str)).nunique()),
    )

    for token in ["up", "down"]:
        part = events[events["outcome_norm"].eq(token)]
        _token_features(out, f"{token}_token", part)

    up_size = out["up_token_size_sum"]
    down_size = out["down_token_size_sum"]
    up_count = out["up_token_trade_count"]
    down_count = out["down_token_trade_count"]
    out["up_minus_down_vwap"] = out["up_token_vwap"] - out["down_token_vwap"]
    out["up_div_down_vwap"] = safe_div(out["up_token_vwap"], out["down_token_vwap"], 1.0)
    out["up_minus_down_trade_count"] = up_count - down_count
    out["up_share_trade_count"] = safe_div(up_count, up_count + down_count, 0.5)
    out["up_share_size"] = safe_div(up_size, up_size + down_size, 0.5)
    out["up_share_notional"] = safe_div(out["up_token_notional_sum"], out["up_token_notional_sum"] + out["down_token_notional_sum"], 0.5)
    out["up_last_price_minus_down_last_price"] = out["up_token_last_price"] - out["down_token_last_price"]
    out["up_price_momentum_minus_down_price_momentum"] = out["up_token_price_return_first_last"] - out["down_token_price_return_first_last"]
    denom_last = out["up_token_last_price"] + (1.0 - out["down_token_last_price"])
    out["market_implied_up_prob_last"] = safe_div(out["up_token_last_price"], denom_last, 0.5)
    denom_vwap = out["up_token_vwap"] + (1.0 - out["down_token_vwap"])
    out["market_implied_up_prob_vwap"] = safe_div(out["up_token_vwap"], denom_vwap, 0.5)
    _pair_price_features(out, "token_last", out["up_token_last_price"], out["down_token_last_price"])
    _pair_price_features(out, "token_first", out["up_token_first_price"], out["down_token_first_price"])
    _pair_price_features(out, "token_vwap", out["up_token_vwap"], out["down_token_vwap"])
    _pair_price_features(out, "token_size_weighted_last_30s", out["up_token_size_weighted_last_30s_price"], out["down_token_size_weighted_last_30s_price"])

    for lo, hi in BUCKETS:
        _bucket_features(out, events, lo, hi)

    _side_features(out, events)
    _distribution_features(out, events)
    return out


def _fill_empty(out: dict[str, float]) -> None:
    for token in ["up_token", "down_token"]:
        for key in ["trade_count", "size_sum", "notional_sum", "vwap", "first_price", "last_price", "max_price", "min_price", "price_range", "price_return_first_last", "price_slope_time", "size_weighted_last_30s_price", "large_trade_count", "large_trade_size_sum", "missing_early"]:
            if key == "missing_early":
                value = 1.0
            elif "price" in key or key == "vwap":
                value = 0.5
            else:
                value = 0.0
            out[f"{token}_{key}"] = value
    base = {
        "early_trade_count": 0, "early_size_sum": 0, "early_notional_sum": 0, "early_avg_price_size_weighted": 0.5,
        "early_first_trade_second": -1, "early_last_trade_second": -1, "early_active_seconds": 0, "early_unique_tx_count": 0,
        "up_minus_down_vwap": 0, "up_div_down_vwap": 1, "up_minus_down_trade_count": 0, "up_share_trade_count": 0.5,
        "up_share_size": 0.5, "up_share_notional": 0.5, "up_last_price_minus_down_last_price": 0,
        "up_price_momentum_minus_down_price_momentum": 0, "market_implied_up_prob_last": 0.5,
        "market_implied_up_prob_vwap": 0.5,
        "token_last_no_vig_up": 0.5, "token_last_complement_up": 0.5, "token_last_sum_minus_one": 0.0, "token_last_abs_sum_minus_one": 0.0,
        "token_first_no_vig_up": 0.5, "token_first_complement_up": 0.5, "token_first_sum_minus_one": 0.0, "token_first_abs_sum_minus_one": 0.0,
        "token_vwap_no_vig_up": 0.5, "token_vwap_complement_up": 0.5, "token_vwap_sum_minus_one": 0.0, "token_vwap_abs_sum_minus_one": 0.0,
        "token_size_weighted_last_30s_no_vig_up": 0.5, "token_size_weighted_last_30s_complement_up": 0.5, "token_size_weighted_last_30s_sum_minus_one": 0.0, "token_size_weighted_last_30s_abs_sum_minus_one": 0.0,
    }
    out.update({k: float(v) for k, v in base.items()})
    for lo, hi in BUCKETS:
        prefix = f"bucket_{lo}_{hi}"
        for key in ["trade_count", "size_sum", "notional_sum", "up_size_share", "up_trade_share", "up_vwap", "down_vwap", "up_last_price", "down_last_price", "up_price_change", "down_price_change"]:
            out[f"{prefix}_{key}"] = 0.0 if "share" not in key and "vwap" not in key and "price" not in key else 0.5
        for key, value in {
            "last_no_vig_up": 0.5,
            "last_complement_up": 0.5,
            "last_sum_minus_one": 0.0,
            "last_abs_sum_minus_one": 0.0,
            "vwap_no_vig_up": 0.5,
            "vwap_complement_up": 0.5,
            "vwap_sum_minus_one": 0.0,
            "vwap_abs_sum_minus_one": 0.0,
        }.items():
            out[f"{prefix}_{key}"] = value
    for key in ["buy_trade_count", "sell_trade_count", "buy_size_sum", "sell_size_sum", "buy_sell_count_imbalance", "buy_sell_size_imbalance", "up_sell_pressure", "down_sell_pressure"]:
        out[key] = 0.0
    for key in ["price_p10", "price_p25", "price_p50", "price_p75", "price_p90", "size_p50", "size_p90", "size_p95", "price_std", "size_std", "outcome_volume_entropy", "top1_trade_size_share", "top3_trade_size_share", "whale_size_share"]:
        out[key] = 0.0


def _token_features(out: dict[str, float], prefix: str, part: pd.DataFrame) -> None:
    if part.empty:
        out.update({f"{prefix}_trade_count": 0.0, f"{prefix}_size_sum": 0.0, f"{prefix}_notional_sum": 0.0, f"{prefix}_vwap": 0.5, f"{prefix}_first_price": 0.5, f"{prefix}_last_price": 0.5, f"{prefix}_max_price": 0.5, f"{prefix}_min_price": 0.5, f"{prefix}_price_range": 0.0, f"{prefix}_price_return_first_last": 0.0, f"{prefix}_price_slope_time": 0.0, f"{prefix}_size_weighted_last_30s_price": 0.5, f"{prefix}_large_trade_count": 0.0, f"{prefix}_large_trade_size_sum": 0.0, f"{prefix}_missing_early": 1.0})
        return
    first = float(part.iloc[0]["price"])
    last = float(part.iloc[-1]["price"])
    span = max(float(part["second_from_start"].max() - part["second_from_start"].min()), 1.0)
    large_cut = float(part["size"].quantile(0.90))
    large = part[part["size"] >= large_cut]
    last_30 = part[part["second_from_start"] >= 90]
    out.update({
        f"{prefix}_trade_count": float(len(part)),
        f"{prefix}_size_sum": float(part["size"].sum()),
        f"{prefix}_notional_sum": float(part["notional"].sum()),
        f"{prefix}_vwap": weighted_avg(part["price"], part["size"]),
        f"{prefix}_first_price": first,
        f"{prefix}_last_price": last,
        f"{prefix}_max_price": float(part["price"].max()),
        f"{prefix}_min_price": float(part["price"].min()),
        f"{prefix}_price_range": float(part["price"].max() - part["price"].min()),
        f"{prefix}_price_return_first_last": last - first,
        f"{prefix}_price_slope_time": (last - first) / span,
        f"{prefix}_size_weighted_last_30s_price": weighted_avg(last_30["price"], last_30["size"]) if not last_30.empty else last,
        f"{prefix}_large_trade_count": float(len(large)),
        f"{prefix}_large_trade_size_sum": float(large["size"].sum()),
        f"{prefix}_missing_early": 0.0,
    })


def _bucket_features(out: dict[str, float], events: pd.DataFrame, lo: int, hi: int) -> None:
    b = events[(events["second_from_start"] >= lo) & (events["second_from_start"] < hi)]
    prefix = f"bucket_{lo}_{hi}"
    up = b[b["outcome_norm"].eq("up")]
    down = b[b["outcome_norm"].eq("down")]
    out[f"{prefix}_trade_count"] = float(len(b))
    out[f"{prefix}_size_sum"] = float(b["size"].sum()) if not b.empty else 0.0
    out[f"{prefix}_notional_sum"] = float(b["notional"].sum()) if not b.empty else 0.0
    out[f"{prefix}_up_size_share"] = safe_div(float(up["size"].sum()), float(b["size"].sum()), 0.5) if not b.empty else 0.5
    out[f"{prefix}_up_trade_share"] = safe_div(float(len(up)), float(len(b)), 0.5) if not b.empty else 0.5
    out[f"{prefix}_up_vwap"] = weighted_avg(up["price"], up["size"]) if not up.empty else 0.5
    out[f"{prefix}_down_vwap"] = weighted_avg(down["price"], down["size"]) if not down.empty else 0.5
    out[f"{prefix}_up_last_price"] = float(up.iloc[-1]["price"]) if not up.empty else 0.5
    out[f"{prefix}_down_last_price"] = float(down.iloc[-1]["price"]) if not down.empty else 0.5
    out[f"{prefix}_up_price_change"] = out[f"{prefix}_up_last_price"] - (float(up.iloc[0]["price"]) if not up.empty else 0.5)
    out[f"{prefix}_down_price_change"] = out[f"{prefix}_down_last_price"] - (float(down.iloc[0]["price"]) if not down.empty else 0.5)
    _pair_price_features(out, f"{prefix}_last", out[f"{prefix}_up_last_price"], out[f"{prefix}_down_last_price"])
    _pair_price_features(out, f"{prefix}_vwap", out[f"{prefix}_up_vwap"], out[f"{prefix}_down_vwap"])


def _pair_price_features(out: dict[str, float], prefix: str, up_price: float, down_price: float) -> None:
    up_price = float(up_price)
    down_price = float(down_price)
    price_sum = up_price + down_price
    out[f"{prefix}_no_vig_up"] = safe_div(up_price, price_sum, 0.5)
    out[f"{prefix}_complement_up"] = 1.0 - down_price
    out[f"{prefix}_sum_minus_one"] = price_sum - 1.0
    out[f"{prefix}_abs_sum_minus_one"] = abs(price_sum - 1.0)


def _side_features(out: dict[str, float], events: pd.DataFrame) -> None:
    side = events.get("side", pd.Series("", index=events.index)).astype(str).str.lower()
    buy = events[side.eq("buy")]
    sell = events[side.eq("sell")]
    out["buy_trade_count"] = float(len(buy))
    out["sell_trade_count"] = float(len(sell))
    out["buy_size_sum"] = float(buy["size"].sum()) if not buy.empty else 0.0
    out["sell_size_sum"] = float(sell["size"].sum()) if not sell.empty else 0.0
    out["buy_sell_count_imbalance"] = safe_div(len(buy) - len(sell), len(buy) + len(sell), 0.0)
    out["buy_sell_size_imbalance"] = safe_div(out["buy_size_sum"] - out["sell_size_sum"], out["buy_size_sum"] + out["sell_size_sum"], 0.0)
    out["up_sell_pressure"] = float(sell[sell["outcome_norm"].eq("up")]["size"].sum()) if not sell.empty else 0.0
    out["down_sell_pressure"] = float(sell[sell["outcome_norm"].eq("down")]["size"].sum()) if not sell.empty else 0.0


def _distribution_features(out: dict[str, float], events: pd.DataFrame) -> None:
    for q in [10, 25, 50, 75, 90]:
        out[f"price_p{q}"] = float(events["price"].quantile(q / 100))
    for q in [50, 90, 95]:
        out[f"size_p{q}"] = float(events["size"].quantile(q / 100))
    out["price_std"] = float(events["price"].std(ddof=0))
    out["size_std"] = float(events["size"].std(ddof=0))
    shares = events.groupby("outcome_norm")["size"].sum()
    probs = shares / shares.sum()
    out["outcome_volume_entropy"] = float(-(probs * np.log(probs + 1e-12)).sum())
    sizes = events["size"].sort_values(ascending=False)
    total = float(sizes.sum())
    out["top1_trade_size_share"] = safe_div(float(sizes.head(1).sum()), total)
    out["top3_trade_size_share"] = safe_div(float(sizes.head(3).sum()), total)
    out["whale_size_share"] = out["top1_trade_size_share"]


def _calendar_features(out: dict[str, float], market_start_ts: int) -> None:
    dt = pd.to_datetime(market_start_ts, unit="s", utc=True)
    out["hour_utc"] = float(dt.hour)
    out["dayofweek_utc"] = float(dt.dayofweek)
    out["minute_of_hour"] = float(dt.minute)
    out["is_us_hours"] = float(13 <= dt.hour <= 21)
    out["is_asia_hours"] = float(dt.hour >= 23 or dt.hour <= 7)
    out["is_europe_hours"] = float(7 <= dt.hour <= 16)

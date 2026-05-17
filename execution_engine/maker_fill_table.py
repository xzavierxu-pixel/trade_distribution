from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


DECISION_ANCHORS = list(range(0, 300, 30))
LIMIT_PRICE_ANCHORS = [round(x / 100, 2) for x in range(5, 100, 5)]


def generate_maker_fill_table(
    trades_csv: Path,
    out_path: Path,
    decision_second: int = 120,
    limit_prices: list[float] | None = None,
    order_delays: list[int] | None = None,
    min_bucket_markets: int = 20,
    b_assumption: float = 1.0,
) -> pd.DataFrame:
    del decision_second, order_delays
    limit_prices = limit_prices or LIMIT_PRICE_ANCHORS
    created_at = datetime.now(timezone.utc).isoformat()
    trades = _prepare_trades(trades_csv)
    markets = trades[["condition_id", "market_start_ts", "final_outcome"]].drop_duplicates("condition_id")
    sell = trades[trades["side"].eq("sell")].copy()
    if sell.empty:
        sell = trades.copy()

    rows: list[dict[str, Any]] = []
    base_frames = []
    diagnostic_frames = []
    for side in ["up", "down"]:
        snapshots = _decision_snapshots(markets, trades, side)
        fills = _fill_flags(markets, sell, side, limit_prices)
        base = snapshots.merge(fills, on=["condition_id", "decision_time_bucket_start"], how="left")
        base["prediction_side"] = side
        base["is_win"] = base["final_outcome"].eq(side)
        base["filled"] = base["filled"].fillna(False).astype(bool)
        base_frames.append(base)
        diagnostic_frames.append(_aggregate(base, ["prediction_side", "decision_time_bucket_start", "current_price_bucket_start", "limit_price_anchor"]))

    base_all = pd.concat(base_frames, ignore_index=True)
    level_tables = _build_level_tables(base_all)
    for decision_start in DECISION_ANCHORS:
        decision_end = min(decision_start + 30, 300)
        for price_start in [round(x / 100, 2) for x in range(0, 100, 5)]:
            price_end = round(min(price_start + 0.05, 1.0), 2)
            context = {
                "decision_time_bucket_start": decision_start,
                "decision_time_bucket_end": decision_end,
                "decision_time_regime": _time_bucket_label(decision_start, decision_end),
                "current_price_bucket_start": price_start,
                "current_price_bucket_end": price_end,
                "current_price_bucket": _price_bucket_label(price_start, price_end),
            }
            for limit_price in limit_prices:
                rows.append(
                    _surface_row(
                        context,
                        limit_price,
                        level_tables,
                        min_bucket_markets,
                        created_at,
                        markets,
                        b_assumption,
                    )
                )

    df = pd.DataFrame(rows)
    df = _smooth_monotonicity(df)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".parquet":
        df.to_parquet(out_path, index=False)
    else:
        df.to_csv(out_path, index=False)
    _write_sidecars(df, out_path, trades_csv, created_at, min_bucket_markets, markets, b_assumption)
    if diagnostic_frames:
        pd.concat(diagnostic_frames, ignore_index=True).to_csv(out_path.with_name("maker_fill_side_diagnostics.csv"), index=False)
    return df


def _prepare_trades(path: Path) -> pd.DataFrame:
    trades = _load_trade_subset(path)
    trades = trades.dropna(subset=["condition_id", "market_start_ts", "timestamp", "price", "size", "final_outcome", "outcome_norm"]).copy()
    trades["second_from_start"] = trades["timestamp"] - trades["market_start_ts"]
    trades = trades[(trades["second_from_start"] >= 0) & trades["price"].between(0, 1)].copy()
    trades["side"] = trades.get("side", pd.Series("", index=trades.index)).astype(str).str.lower()
    return trades


def _decision_snapshots(markets: pd.DataFrame, trades: pd.DataFrame, side: str) -> pd.DataFrame:
    side_trades = trades[trades["outcome_norm"].eq(side)].sort_values(["condition_id", "second_from_start"])
    frames = []
    for start in DECISION_ANCHORS:
        end = min(start + 30, 300)
        current = side_trades[side_trades["second_from_start"] <= start].groupby("condition_id").tail(1)[["condition_id", "price"]]
        current = current.rename(columns={"price": "current_price"})
        snap = markets.merge(current, on="condition_id", how="left")
        snap["current_price"] = snap["current_price"].fillna(0.5)
        snap["decision_time_bucket_start"] = start
        snap["decision_time_bucket_end"] = end
        snap["decision_time_regime"] = _time_bucket_label(start, end)
        snap["decision_time_bucket_start_60"] = (start // 60) * 60
        snap["decision_time_regime_60"] = snap["decision_time_bucket_start_60"].map(lambda x: _time_bucket_label(int(x), min(int(x) + 60, 300)))
        snap["current_price_bucket_start"] = snap["current_price"].map(lambda p: _price_bucket_start(float(p), 0.05))
        snap["current_price_bucket_end"] = snap["current_price_bucket_start"].map(lambda x: round(min(float(x) + 0.05, 1.0), 2))
        snap["current_price_bucket"] = snap.apply(lambda r: _price_bucket_label(float(r["current_price_bucket_start"]), float(r["current_price_bucket_end"])), axis=1)
        snap["current_price_bucket_start_10"] = snap["current_price"].map(lambda p: _price_bucket_start(float(p), 0.10))
        snap["current_price_bucket_10"] = snap["current_price_bucket_start_10"].map(lambda x: _price_bucket_label(float(x), round(min(float(x) + 0.10, 1.0), 2)))
        frames.append(snap)
    return pd.concat(frames, ignore_index=True)


def _fill_flags(markets: pd.DataFrame, sell: pd.DataFrame, side: str, limit_prices: list[float]) -> pd.DataFrame:
    side_sell = sell[sell["outcome_norm"].eq(side)]
    min_price_by_decision = {
        decision: side_sell[side_sell["second_from_start"] >= decision].groupby("condition_id")["price"].min()
        for decision in DECISION_ANCHORS
    }
    rows = []
    ids = markets["condition_id"].astype(str)
    for decision in DECISION_ANCHORS:
        min_price = min_price_by_decision[decision].reindex(ids).reset_index(drop=True)
        for limit_price in limit_prices:
            rows.append(
                pd.DataFrame(
                    {
                        "condition_id": ids.to_numpy(),
                        "decision_time_bucket_start": decision,
                        "limit_price_anchor": limit_price,
                        "filled": (min_price <= limit_price).fillna(False).to_numpy(),
                    }
                )
            )
    return pd.concat(rows, ignore_index=True)


def _build_level_tables(base: pd.DataFrame) -> list[tuple[str, dict[tuple[Any, ...], dict[str, Any]]]]:
    levels = [
        ("level_0", ["decision_time_bucket_start", "current_price_bucket_start", "limit_price_anchor"]),
        ("level_1", ["decision_time_bucket_start_60", "current_price_bucket_start", "limit_price_anchor"]),
        ("level_2", ["decision_time_bucket_start", "current_price_bucket_start_10", "limit_price_anchor"]),
        ("level_3", ["decision_time_bucket_start_60", "current_price_bucket_start_10", "limit_price_anchor"]),
        ("level_4", ["current_price_bucket_start_10", "limit_price_anchor"]),
        ("level_5", ["limit_price_anchor"]),
    ]
    out = []
    for name, keys in levels:
        table = _aggregate(base, keys)
        lookup = {tuple(row[key] for key in keys): row.to_dict() for _, row in table.iterrows()}
        out.append((name, lookup))
    return out


def _aggregate(part: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for values, group in part.groupby(keys, dropna=False):
        if not isinstance(values, tuple):
            values = (values,)
        row = dict(zip(keys, values))
        win = group[group["is_win"]]
        lose = group[~group["is_win"]]
        row.update(_counts(win, "win"))
        row.update(_counts(lose, "lose"))
        row["sample_market_count"] = int(row["win_market_count"] + row["lose_market_count"])
        rows.append(row)
    return pd.DataFrame(rows)


def _counts(part: pd.DataFrame, prefix: str) -> dict[str, int | float]:
    market_count = int(part["condition_id"].nunique())
    fill_count = int(part.loc[part["filled"], "condition_id"].nunique()) if market_count else 0
    return {
        f"{prefix}_market_count": market_count,
        f"{prefix}_fill_market_count": fill_count,
        f"a_{prefix}_market_fill": float(fill_count / market_count) if market_count else 0.0,
    }


def _surface_row(
    context: dict[str, Any],
    limit_price: float,
    level_tables: list[tuple[str, dict[tuple[Any, ...], dict[str, Any]]]],
    min_market_count: int,
    created_at: str,
    markets: pd.DataFrame,
    b_assumption: float,
) -> dict[str, Any]:
    chosen_level = "missing"
    chosen_counts: dict[str, Any] = {}
    for level, lookup in level_tables:
        match = lookup.get(_level_key(context, level, limit_price))
        if match is None:
            continue
        chosen_level = level
        chosen_counts = match
        if int(match["win_market_count"]) + int(match["lose_market_count"]) >= min_market_count:
            break
    is_reliable = int(chosen_counts.get("win_market_count", 0)) + int(chosen_counts.get("lose_market_count", 0)) >= min_market_count
    data_start = pd.to_datetime(markets["market_start_ts"], unit="s", utc=True).min().isoformat()
    data_end = pd.to_datetime(markets["market_start_ts"], unit="s", utc=True).max().isoformat()
    a_win = float(chosen_counts.get("a_win_market_fill", 0.0))
    win_count = int(chosen_counts.get("win_market_count", 0))
    lose_count = int(chosen_counts.get("lose_market_count", 0))
    sample_count = win_count + lose_count
    q_market = float(win_count / sample_count) if sample_count else 0.0
    q_used = q_market
    kelly = _kelly_metrics(q_used, a_win, limit_price)
    is_valid_maker_candidate = float(limit_price) < float(context["current_price_bucket_start"])
    return {
        **context,
        "limit_price_anchor": float(limit_price),
        "win_market_count": win_count,
        "win_fill_market_count": int(chosen_counts.get("win_fill_market_count", 0)),
        "a_win_market_fill": a_win,
        "lose_market_count": lose_count,
        "lose_fill_market_count": int(chosen_counts.get("lose_fill_market_count", 0)),
        "a_lose_market_fill": float(chosen_counts.get("a_lose_market_fill", 0.0)),
        "a_lose_assumption": float(b_assumption),
        "sample_market_count": sample_count,
        "a_win_LCB": _wilson_lower_bound(int(chosen_counts.get("win_fill_market_count", 0)), win_count),
        "q_market": q_market,
        "q_market_LCB": _wilson_lower_bound(win_count, sample_count),
        "q_used_default": _wilson_lower_bound(win_count, sample_count),
        "kelly_a_win_used": _wilson_lower_bound(int(chosen_counts.get("win_fill_market_count", 0)), win_count),
        "q_required": kelly["q_required"],
        "q_margin": kelly["q_margin"],
        "R_payoff": kelly["R_payoff"],
        "edge_market_q": kelly["edge"],
        "f_kelly_raw": kelly["f_kelly_raw"],
        "f_kelly": kelly["f_kelly"],
        "kelly_growth": kelly["kelly_growth"],
        "fallback_level": chosen_level,
        "is_reliable": bool(is_reliable),
        "is_valid_maker_candidate": bool(is_valid_maker_candidate),
        "is_positive_ev": bool(kelly["edge"] > 0),
        "created_at_utc": created_at,
        "data_start_utc": data_start,
        "data_end_utc": data_end,
        "current_price_source": "last_valid_trade",
        "fill_proxy": "taker_sell_price_lte_limit_price_after_decision_second",
        "order_valid_until": "market_end",
    }


def _level_key(context: dict[str, Any], level: str, limit_price: float) -> tuple[Any, ...]:
    base: list[Any] = []
    if level in {"level_0", "level_2"}:
        base.append(int(context["decision_time_bucket_start"]))
    if level in {"level_1", "level_3"}:
        base.append((int(context["decision_time_bucket_start"]) // 60) * 60)
    if level in {"level_0", "level_1"}:
        base.append(float(context["current_price_bucket_start"]))
    if level in {"level_2", "level_3", "level_4"}:
        base.append(_price_bucket_start(float(context["current_price_bucket_start"]), 0.10))
    base.append(float(limit_price))
    return tuple(base)


def _smooth_monotonicity(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["decision_time_bucket_start", "current_price_bucket_start", "limit_price_anchor"]).copy()
    df["a_win_market_fill_raw"] = df["a_win_market_fill"]
    df["a_lose_market_fill_raw"] = df["a_lose_market_fill"]
    for idx, row in df.iterrows():
        kelly = _kelly_metrics(float(row["q_market_LCB"]), float(row["a_win_LCB"]), float(row["limit_price_anchor"]))
        df.at[idx, "q_used_default"] = float(row["q_market_LCB"])
        df.at[idx, "kelly_a_win_used"] = float(row["a_win_LCB"])
        df.at[idx, "q_required"] = kelly["q_required"]
        df.at[idx, "q_margin"] = kelly["q_margin"]
        df.at[idx, "R_payoff"] = kelly["R_payoff"]
        df.at[idx, "edge_market_q"] = kelly["edge"]
        df.at[idx, "f_kelly_raw"] = kelly["f_kelly_raw"]
        df.at[idx, "f_kelly"] = kelly["f_kelly"]
        df.at[idx, "kelly_growth"] = kelly["kelly_growth"]
        df.at[idx, "is_positive_ev"] = bool(kelly["edge"] > 0)
    return df.sort_values(["decision_time_bucket_start", "current_price_bucket_start", "limit_price_anchor"]).reset_index(drop=True)


def _wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p_hat = successes / total
    z2 = z * z
    denom = 1.0 + z2 / total
    centre = p_hat + z2 / (2.0 * total)
    radius = z * math.sqrt((p_hat * (1.0 - p_hat) + z2 / (4.0 * total)) / total)
    return float(max(0.0, (centre - radius) / denom))


def _kelly_metrics(q: float, a_win: float, price: float) -> dict[str, float]:
    if price <= 0 or price >= 1:
        return {
            "R_payoff": float("inf"),
            "edge": float("-inf"),
            "q_required": float("inf"),
            "q_margin": float("-inf"),
            "f_kelly_raw": float("-inf"),
            "f_kelly": 0.0,
            "kelly_growth": 0.0,
        }
    r_payoff = (1.0 - price) / price
    edge = q * a_win * r_payoff - (1.0 - q)
    q_required = price / (price + a_win * (1.0 - price)) if (price + a_win * (1.0 - price)) > 0 else float("inf")
    denom = r_payoff * (q * a_win + 1.0 - q)
    f_raw = edge / denom if denom > 0 else float("-inf")
    f = max(0.0, f_raw)
    growth = q * a_win * math.log1p(f * r_payoff) + (1.0 - q) * math.log1p(-f) if 0.0 <= f < 1.0 else float("-inf")
    return {
        "R_payoff": float(r_payoff),
        "edge": float(edge),
        "q_required": float(q_required),
        "q_margin": float(q - q_required),
        "f_kelly_raw": float(f_raw),
        "f_kelly": float(f),
        "kelly_growth": float(growth),
    }


def _write_sidecars(df: pd.DataFrame, out_path: Path, trades_csv: Path, created_at: str, min_market_count: int, markets: pd.DataFrame, b_assumption: float) -> None:
    summary = (
        df.groupby(["fallback_level", "is_reliable", "is_positive_ev"], dropna=False)
        .agg(row_count=("fallback_level", "size"), avg_a_win=("a_win_market_fill", "mean"), avg_f_kelly=("f_kelly", "mean"), avg_sample_markets=("sample_market_count", "mean"))
        .reset_index()
    )
    summary.to_csv(out_path.with_name(out_path.stem + "_summary.csv"), index=False)
    metadata = {
        "market_type": "BTC 5m Up/Down",
        "source_trades_csv": str(trades_csv),
        "decision_time_step_seconds": 30,
        "current_price_bucket_size": 0.05,
        "limit_price_step": 0.05,
        "min_market_count": min_market_count,
        "q_default": "q_market_LCB",
        "a_win_default": "a_win_LCB",
        "wilson_z": 1.96,
        "a_lose_assumption": b_assumption,
        "fill_proxy": "taker_sell_price_lte_limit_price_after_decision_second",
        "lose_fill_assumption_for_runtime": 1.0,
        "order_valid_until": "market_end",
        "created_at_utc": created_at,
        "data_start_utc": pd.to_datetime(markets["market_start_ts"], unit="s", utc=True).min().isoformat(),
        "data_end_utc": pd.to_datetime(markets["market_start_ts"], unit="s", utc=True).max().isoformat(),
        "row_count": int(len(df)),
    }
    out_path.with_name(out_path.stem + "_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def _price_bucket_start(price: float, step: float) -> float:
    if price >= 1.0:
        return round(1.0 - step, 2)
    return round(max(0.0, min(1.0 - step, int(price / step) * step)), 2)


def _price_bucket_label(start: float, end: float) -> str:
    right = "]" if end >= 1.0 else ")"
    return f"[{start:.2f},{end:.2f}{right}"


def _time_bucket_label(start: int, end: int) -> str:
    right = "]" if end >= 300 else ")"
    return f"[{start},{end}{right}"


def _load_trade_subset(path: Path) -> pd.DataFrame:
    rename = {
        "conditionId": "condition_id",
        "_condition_id": "condition_id",
        "_market_start_ts": "market_start_ts",
        "_final_outcome": "final_outcome",
        "_outcome_norm": "outcome_norm",
    }
    wanted = {
        "conditionId",
        "_condition_id",
        "condition_id",
        "_market_start_ts",
        "market_start_ts",
        "timestamp",
        "price",
        "size",
        "side",
        "_final_outcome",
        "final_outcome",
        "_outcome_norm",
        "outcome_norm",
    }
    df = pd.read_csv(path, usecols=lambda c: c in wanted)
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    df = _coalesce_duplicate_columns(df)
    for col in ["timestamp", "market_start_ts", "price", "size"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["condition_id", "side", "final_outcome", "outcome_norm"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.lower().str.strip()
    return df


def _coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if not df.columns.has_duplicates:
        return df
    out = pd.DataFrame(index=df.index)
    for col in dict.fromkeys(df.columns):
        same = df.loc[:, df.columns == col]
        out[col] = same.bfill(axis=1).iloc[:, 0] if same.shape[1] > 1 else same.iloc[:, 0]
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades-csv", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--decision-second", type=int, default=120)
    args = parser.parse_args()
    generate_maker_fill_table(args.trades_csv, args.out, args.decision_second)


if __name__ == "__main__":
    main()

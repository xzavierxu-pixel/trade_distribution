from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


DECISION_ANCHORS = list(range(0, 300, 30))
SUBMIT_ANCHORS = list(range(0, 300, 30))
LIMIT_PRICE_ANCHORS = [round(x / 100, 2) for x in range(5, 100, 5)]


def generate_maker_fill_table(
    trades_csv: Path,
    out_path: Path,
    decision_second: int = 120,
    limit_prices: list[float] | None = None,
    order_delays: list[int] | None = None,
    min_bucket_markets: int = 30,
) -> pd.DataFrame:
    del decision_second
    limit_prices = limit_prices or LIMIT_PRICE_ANCHORS
    order_delays = order_delays or SUBMIT_ANCHORS
    created_at = datetime.now(timezone.utc).isoformat()
    trades = _prepare_trades(trades_csv)
    markets = trades[["condition_id", "market_start_ts", "final_outcome"]].drop_duplicates("condition_id")
    sell = trades[trades["side"].eq("sell")].copy()
    if sell.empty:
        sell = trades.copy()

    rows: list[dict[str, Any]] = []
    level_tables_by_side: dict[str, list[tuple[str, pd.DataFrame]]] = {}
    for side in ["up", "down"]:
        snapshots = _decision_snapshots(markets, trades, side)
        fills = _fill_flags(markets, sell, side, order_delays, limit_prices)
        base = snapshots.merge(fills, on="condition_id", how="left")
        base["is_win"] = base["final_outcome"].eq(side)
        base["filled"] = base["filled"].fillna(False).astype(bool)
        level_tables_by_side[side] = _build_level_tables(base, side)
        for decision_start in DECISION_ANCHORS:
            decision_end = min(decision_start + 30, 300)
            for price_start in [round(x / 100, 2) for x in range(0, 100, 5)]:
                price_end = round(min(price_start + 0.05, 1.0), 2)
                context = {
                    "prediction_side": side,
                    "decision_time_bucket_start": decision_start,
                    "decision_time_bucket_end": decision_end,
                    "decision_time_regime": _time_bucket_label(decision_start, decision_end),
                    "current_price_bucket_start": price_start,
                    "current_price_bucket_end": price_end,
                    "current_price_bucket": _price_bucket_label(price_start, price_end),
                }
                for submit_anchor in order_delays:
                    if submit_anchor < decision_start:
                        continue
                    for limit_price in limit_prices:
                        rows.append(
                            _surface_row(
                                context,
                                submit_anchor,
                                limit_price,
                                level_tables_by_side[side],
                                min_bucket_markets,
                                created_at,
                                markets,
                            )
                        )

    df = pd.DataFrame(rows)
    df = _smooth_monotonicity(df)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".parquet":
        df.to_parquet(out_path, index=False)
    else:
        df.to_csv(out_path, index=False)
    _write_sidecars(df, out_path, trades_csv, created_at, min_bucket_markets, markets)
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


def _fill_flags(markets: pd.DataFrame, sell: pd.DataFrame, side: str, submit_anchors: list[int], limit_prices: list[float]) -> pd.DataFrame:
    side_sell = sell[sell["outcome_norm"].eq(side)]
    min_price_by_submit = {
        submit: side_sell[side_sell["second_from_start"] >= submit].groupby("condition_id")["price"].min()
        for submit in submit_anchors
    }
    rows = []
    ids = markets["condition_id"].astype(str)
    for submit in submit_anchors:
        min_price = min_price_by_submit[submit].reindex(ids).reset_index(drop=True)
        for limit_price in limit_prices:
            rows.append(
                pd.DataFrame(
                    {
                        "condition_id": ids.to_numpy(),
                        "submit_second_anchor": submit,
                        "limit_price_anchor": limit_price,
                        "filled": (min_price <= limit_price).fillna(False).to_numpy(),
                    }
                )
            )
    return pd.concat(rows, ignore_index=True)


def _build_level_tables(base: pd.DataFrame, side: str) -> list[tuple[str, dict[tuple[Any, ...], dict[str, Any]]]]:
    levels = [
        ("level_0_side_time30_price005", ["prediction_side", "decision_time_bucket_start", "current_price_bucket_start", "submit_second_anchor", "limit_price_anchor"]),
        ("level_1_side_time60_price005", ["prediction_side", "decision_time_bucket_start_60", "current_price_bucket_start", "submit_second_anchor", "limit_price_anchor"]),
        ("level_2_side_time30_price010", ["prediction_side", "decision_time_bucket_start", "current_price_bucket_start_10", "submit_second_anchor", "limit_price_anchor"]),
        ("level_3_side_time60_price010", ["prediction_side", "decision_time_bucket_start_60", "current_price_bucket_start_10", "submit_second_anchor", "limit_price_anchor"]),
        ("level_4_side_price010", ["prediction_side", "current_price_bucket_start_10", "submit_second_anchor", "limit_price_anchor"]),
        ("level_5_side_time60", ["prediction_side", "decision_time_bucket_start_60", "submit_second_anchor", "limit_price_anchor"]),
        ("level_6_side_global", ["prediction_side", "submit_second_anchor", "limit_price_anchor"]),
    ]
    base = base.copy()
    base["prediction_side"] = side
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
    submit_anchor: int,
    limit_price: float,
    level_tables: list[tuple[str, dict[tuple[Any, ...], dict[str, Any]]]],
    min_win_market_count: int,
    created_at: str,
    markets: pd.DataFrame,
) -> dict[str, Any]:
    side = str(context["prediction_side"])
    chosen_level = "missing"
    chosen_counts: dict[str, Any] = {}
    for level, lookup in level_tables:
        match = lookup.get(_level_key(context, level, submit_anchor, limit_price))
        if match is None:
            continue
        chosen_level = level
        chosen_counts = match
        if int(match["win_market_count"]) >= min_win_market_count:
            break
    is_reliable = int(chosen_counts.get("win_market_count", 0)) >= min_win_market_count
    data_start = pd.to_datetime(markets["market_start_ts"], unit="s", utc=True).min().isoformat()
    data_end = pd.to_datetime(markets["market_start_ts"], unit="s", utc=True).max().isoformat()
    return {
        **context,
        "submit_second_anchor": int(submit_anchor),
        "order_delay_seconds": int(submit_anchor),
        "limit_price_anchor": float(limit_price),
        "limit_price": float(limit_price),
        "win_market_count": int(chosen_counts.get("win_market_count", 0)),
        "win_fill_market_count": int(chosen_counts.get("win_fill_market_count", 0)),
        "a_win_market_fill": float(chosen_counts.get("a_win_market_fill", 0.0)),
        "lose_market_count": int(chosen_counts.get("lose_market_count", 0)),
        "lose_fill_market_count": int(chosen_counts.get("lose_fill_market_count", 0)),
        "a_lose_market_fill": float(chosen_counts.get("a_lose_market_fill", 0.0)),
        "sample_market_count": int(chosen_counts.get("sample_market_count", 0)),
        "fallback_level": chosen_level,
        "is_reliable": bool(is_reliable),
        "created_at_utc": created_at,
        "data_start_utc": data_start,
        "data_end_utc": data_end,
        "current_price_source": "last_valid_trade",
        "fill_proxy": "taker_sell_price_lte_limit_price_after_submit_second",
        "order_valid_until": "market_end",
    }


def _level_key(context: dict[str, Any], level: str, submit_anchor: int, limit_price: float) -> tuple[Any, ...]:
    base: list[Any] = [str(context["prediction_side"])]
    if "time30" in level:
        base.append(int(context["decision_time_bucket_start"]))
    if "time60" in level:
        base.append((int(context["decision_time_bucket_start"]) // 60) * 60)
    if "price005" in level:
        base.append(float(context["current_price_bucket_start"]))
    if "price010" in level:
        base.append(_price_bucket_start(float(context["current_price_bucket_start"]), 0.10))
    base.extend([int(submit_anchor), float(limit_price)])
    return tuple(base)


def _smooth_monotonicity(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["prediction_side", "decision_time_bucket_start", "current_price_bucket_start", "submit_second_anchor", "limit_price_anchor"]).copy()
    group_cols = ["prediction_side", "decision_time_bucket_start", "current_price_bucket_start", "submit_second_anchor"]
    df["a_win_market_fill_raw"] = df["a_win_market_fill"]
    df["a_lose_market_fill_raw"] = df["a_lose_market_fill"]
    df["a_win_market_fill"] = df.groupby(group_cols, group_keys=False)["a_win_market_fill"].cummax()
    df["a_lose_market_fill"] = df.groupby(group_cols, group_keys=False)["a_lose_market_fill"].cummax()
    time_group_cols = ["prediction_side", "decision_time_bucket_start", "current_price_bucket_start", "limit_price_anchor"]
    df = df.sort_values(time_group_cols + ["submit_second_anchor"], ascending=[True, True, True, True, False])
    df["a_win_market_fill"] = df.groupby(time_group_cols, group_keys=False)["a_win_market_fill"].cummax()
    df["a_lose_market_fill"] = df.groupby(time_group_cols, group_keys=False)["a_lose_market_fill"].cummax()
    return df.sort_values(["prediction_side", "decision_time_bucket_start", "current_price_bucket_start", "submit_second_anchor", "limit_price_anchor"]).reset_index(drop=True)


def _write_sidecars(df: pd.DataFrame, out_path: Path, trades_csv: Path, created_at: str, min_win_market_count: int, markets: pd.DataFrame) -> None:
    summary = (
        df.groupby(["prediction_side", "fallback_level", "is_reliable"], dropna=False)
        .agg(row_count=("prediction_side", "size"), avg_a_win=("a_win_market_fill", "mean"), avg_sample_markets=("sample_market_count", "mean"))
        .reset_index()
    )
    summary.to_csv(out_path.with_name(out_path.stem + "_summary.csv"), index=False)
    metadata = {
        "market_type": "BTC 5m Up/Down",
        "source_trades_csv": str(trades_csv),
        "decision_time_step_seconds": 30,
        "current_price_bucket_size": 0.05,
        "submit_second_step_seconds": 30,
        "limit_price_step": 0.05,
        "min_win_market_count": min_win_market_count,
        "fill_proxy": "taker_sell_price_lte_limit_price_after_submit_second",
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

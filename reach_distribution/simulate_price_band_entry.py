#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simulate one passive-buy entry per market from historical taker SELL fills.

The simulation is intentionally market-level: each market contributes at most
one randomly selected order per run, so markets with many trades do not dominate
the result.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def safe_div(num: float, den: float) -> float:
    return float(num / den) if den else float("nan")


def summarize_trades(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    g = df.groupby(group_cols, dropna=False).agg(
        rows=("price", "size"),
        markets=("condition_id", "nunique"),
        size_sum=("size", "sum"),
        cost=("cost", "sum"),
        payout=("payout", "sum"),
        avg_price=("price", lambda s: np.average(s, weights=df.loc[s.index, "size"])),
        win_rate_size=("payout", lambda s: safe_div(float(s.sum()), float(df.loc[s.index, "size"].sum()))),
    )
    g["profit"] = g["payout"] - g["cost"]
    g["roi_on_cost"] = g["profit"] / g["cost"]
    return g.reset_index()


def summarize_orders(orders: pd.DataFrame, group_cols: list[str]) -> dict[str, float | int | str]:
    cost = float(orders["price"].sum())
    payout = float(orders["is_winner"].sum())
    profit = payout - cost
    out: dict[str, float | int | str] = {
        "orders": int(len(orders)),
        "markets": int(orders["condition_id"].nunique()),
        "avg_entry_price": float(orders["price"].mean()) if len(orders) else float("nan"),
        "win_rate_orders": safe_div(payout, len(orders)),
        "cost": cost,
        "payout": payout,
        "profit": profit,
        "roi_on_cost": safe_div(profit, cost),
    }
    for col in group_cols:
        out[col] = orders[col].iloc[0]
    return out


def monte_carlo_one_per_group(
    eligible: pd.DataFrame,
    group_cols: list[str],
    market_key_cols: list[str],
    runs: int,
    seed: int,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for group_key, g in eligible.groupby(group_cols, dropna=False):
        group_key_tuple = group_key if isinstance(group_key, tuple) else (group_key,)
        group_values = dict(zip(group_cols, group_key_tuple))
        g = g.reset_index(drop=True)
        prices = g["price"].astype(float).to_numpy()
        winners = g["is_winner"].astype(float).to_numpy()
        market_indices = [
            np.asarray(idx, dtype=int)
            for _, idx in g.groupby(market_key_cols, dropna=False).indices.items()
        ]
        for run in range(runs):
            picked = np.fromiter(
                (choices[int(rng.integers(0, len(choices)))] for choices in market_indices),
                dtype=int,
                count=len(market_indices),
            )
            cost = float(prices[picked].sum())
            payout = float(winners[picked].sum())
            profit = payout - cost
            rows.append(
                {
                    **group_values,
                    "run": run,
                    "orders": int(len(picked)),
                    "markets": int(len(picked)),
                    "avg_entry_price": float(prices[picked].mean()) if len(picked) else float("nan"),
                    "win_rate_orders": safe_div(payout, len(picked)),
                    "cost": cost,
                    "payout": payout,
                    "profit": profit,
                    "roi_on_cost": safe_div(profit, cost),
                }
            )
    return pd.DataFrame(rows)


def summarize_mc(mc: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if mc.empty:
        return pd.DataFrame()
    agg = mc.groupby(group_cols, dropna=False).agg(
        runs=("run", "nunique"),
        markets_mean=("markets", "mean"),
        orders_mean=("orders", "mean"),
        avg_entry_price_mean=("avg_entry_price", "mean"),
        win_rate_mean=("win_rate_orders", "mean"),
        win_rate_p05=("win_rate_orders", lambda s: s.quantile(0.05)),
        win_rate_p95=("win_rate_orders", lambda s: s.quantile(0.95)),
        roi_mean=("roi_on_cost", "mean"),
        roi_p05=("roi_on_cost", lambda s: s.quantile(0.05)),
        roi_p95=("roi_on_cost", lambda s: s.quantile(0.95)),
        profit_mean=("profit", "mean"),
    )
    return agg.reset_index()


def load_eligible(args: argparse.Namespace) -> pd.DataFrame:
    usecols = [
        "side",
        "price",
        "size",
        "timestamp",
        "_condition_id",
        "_market_start_ts",
        "_final_outcome",
        "_outcome_norm",
    ]
    parts = []
    for ch in pd.read_csv(args.trades_csv, usecols=usecols, chunksize=args.chunksize, dtype=str):
        ch = ch.rename(columns={"_condition_id": "condition_id"})
        ch["side"] = ch["side"].astype(str).str.lower().str.strip()
        ch["_outcome_norm"] = ch["_outcome_norm"].astype(str).str.lower().str.strip()
        ch["_final_outcome"] = ch["_final_outcome"].astype(str).str.lower().str.strip()
        ch["condition_id"] = ch["condition_id"].astype(str)
        ch["price"] = pd.to_numeric(ch["price"], errors="coerce")
        ch["size"] = pd.to_numeric(ch["size"], errors="coerce")
        ch["timestamp"] = pd.to_numeric(ch["timestamp"], errors="coerce")
        ch["_market_start_ts"] = pd.to_numeric(ch["_market_start_ts"], errors="coerce")
        ch = ch.dropna(subset=["price", "size", "timestamp", "_market_start_ts"])
        ch["minute_from_start"] = np.floor((ch["timestamp"] - ch["_market_start_ts"]) / 60).astype(int)
        ch = ch[
            ch["side"].eq("sell")
            & (ch["price"] >= args.price_low)
            & (ch["price"] < args.price_high)
            & (ch["minute_from_start"] >= args.min_minute)
            & (ch["minute_from_start"] <= args.max_minute)
            & (ch["size"] > 0)
        ].copy()
        if ch.empty:
            continue
        ch["is_winner"] = (ch["_outcome_norm"] == ch["_final_outcome"]).astype(int)
        ch["cost"] = ch["price"] * ch["size"]
        ch["payout"] = ch["is_winner"] * ch["size"]
        ch["token_side"] = ch["_outcome_norm"]
        parts.append(ch)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def write_report(outdir: Path, eligible: pd.DataFrame, minute_mc_summary: pd.DataFrame, overall_summary: pd.DataFrame) -> None:
    lines = [
        "# 0.60-0.80 Passive Buy Simulation",
        "",
        "Universe: taker SELL fills only, one passive-buy entry simulated per market.",
        "",
        "## Eligible Fill Set",
        "",
        f"- rows: `{len(eligible):,}`",
        f"- markets: `{eligible['condition_id'].nunique():,}`",
        f"- minute range: `{eligible['minute_from_start'].min()}` to `{eligible['minute_from_start'].max()}`",
        "",
        "## Random One Order Per Market, Overall",
        "",
        overall_summary.to_markdown(index=False) if not overall_summary.empty else "No eligible fills.",
        "",
        "## Random One Order Per Market, By Minute",
        "",
        minute_mc_summary.to_markdown(index=False) if not minute_mc_summary.empty else "No eligible fills.",
        "",
        "Interpretation: `win_rate_mean` is order-level settlement win rate. `roi_mean` assumes one share per selected order.",
        "",
    ]
    (outdir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades-csv", type=Path, default=Path("out_btc5m_7d_full_strategy/trades_raw.csv"))
    parser.add_argument("--outdir", type=Path, default=Path("analysis_clean/btc5m_7d_conservative/band_060_080_sim"))
    parser.add_argument("--price-low", type=float, default=0.60)
    parser.add_argument("--price-high", type=float, default=0.80)
    parser.add_argument("--min-minute", type=int, default=0)
    parser.add_argument("--max-minute", type=int, default=4)
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--chunksize", type=int, default=250_000)
    args = parser.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)
    eligible = load_eligible(args)
    if eligible.empty:
        raise SystemExit("No eligible fills.")

    all_fills_minute = summarize_trades(eligible, ["minute_from_start"])
    all_fills_token_minute = summarize_trades(eligible, ["token_side", "minute_from_start"])
    all_fills_minute.to_csv(args.outdir / "all_fills_by_minute.csv", index=False)
    all_fills_token_minute.to_csv(args.outdir / "all_fills_by_token_side_minute.csv", index=False)

    minute_mc = monte_carlo_one_per_group(
        eligible,
        group_cols=["minute_from_start"],
        market_key_cols=["condition_id"],
        runs=args.runs,
        seed=args.seed,
    )
    minute_mc.to_csv(args.outdir / "random_one_order_by_market_minute_runs.csv", index=False)
    minute_mc_summary = summarize_mc(minute_mc, ["minute_from_start"])
    minute_mc_summary.to_csv(args.outdir / "random_one_order_by_market_minute_summary.csv", index=False)

    overall_mc = monte_carlo_one_per_group(
        eligible.assign(scope="all_minutes"),
        group_cols=["scope"],
        market_key_cols=["condition_id"],
        runs=args.runs,
        seed=args.seed + 1,
    )
    overall_mc.to_csv(args.outdir / "random_one_order_by_market_overall_runs.csv", index=False)
    overall_summary = summarize_mc(overall_mc, ["scope"])
    overall_summary.to_csv(args.outdir / "random_one_order_by_market_overall_summary.csv", index=False)

    minute_selection = (
        eligible.groupby(["condition_id", "minute_from_start"], as_index=False)
        .size()
        .groupby("minute_from_start", as_index=False)
        .agg(markets_with_opportunity=("condition_id", "nunique"), fill_rows=("size", "sum"))
    )
    minute_selection.to_csv(args.outdir / "markets_with_opportunity_by_minute.csv", index=False)

    write_report(args.outdir, eligible, minute_mc_summary, overall_summary)


if __name__ == "__main__":
    main()

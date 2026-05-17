#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fine-grained realized ROI by taker side, 10-second bin, and 0.02 price bin."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def safe_div(num: float, den: float) -> float:
    return float(num / den) if den else float("nan")


def price_bin_label(price: float, width: float) -> str | None:
    if price < 0 or price > 1:
        return None
    lo = np.floor(price / width) * width
    hi = min(lo + width, 1.0)
    if price == 1.0:
        lo = 1.0 - width
        hi = 1.0
    return f"{lo:.2f}-{hi:.2f}"


def bump(store: dict[tuple[Any, ...], dict[str, float]], key: tuple[Any, ...], **vals: float) -> None:
    d = store.setdefault(key, {})
    for k, v in vals.items():
        d[k] = d.get(k, 0.0) + float(v)


def finalize(rows: dict[tuple[Any, ...], dict[str, float]], names: list[str]) -> pd.DataFrame:
    out = []
    for key, v in rows.items():
        item = dict(zip(names, key))
        size_sum = v.get("size", 0.0)
        cost = v.get("cost", 0.0)
        payout = v.get("payout", 0.0)
        item.update(
            rows=int(v.get("rows", 0)),
            size_sum=size_sum,
            cost=cost,
            payout=payout,
            profit=payout - cost,
            avg_price=safe_div(cost, size_sum),
            win_rate_size=safe_div(payout, size_sum),
            roi_on_cost=safe_div(payout - cost, cost),
        )
        avg_price = item["avg_price"]
        win_rate_size = item["win_rate_size"]
        if np.isfinite(avg_price) and np.isfinite(win_rate_size) and avg_price < 1:
            kelly_full = (win_rate_size - avg_price) / (1.0 - avg_price)
            item["kelly_full"] = max(0.0, float(kelly_full))
        else:
            item["kelly_full"] = float("nan")
        out.append(item)
    return pd.DataFrame(out)


def analyze(args: argparse.Namespace) -> None:
    args.outdir.mkdir(parents=True, exist_ok=True)
    store: dict[tuple[Any, ...], dict[str, float]] = {}
    usecols = [
        "side",
        "price",
        "size",
        "timestamp",
        "_market_start_ts",
        "_final_outcome",
        "_outcome_norm",
    ]

    for ch in pd.read_csv(args.trades_csv, usecols=usecols, chunksize=args.chunksize, dtype=str):
        ch["side"] = ch["side"].astype(str).str.lower().str.strip()
        ch["_outcome_norm"] = ch["_outcome_norm"].astype(str).str.lower().str.strip()
        ch["_final_outcome"] = ch["_final_outcome"].astype(str).str.lower().str.strip()
        ch["price"] = pd.to_numeric(ch["price"], errors="coerce")
        ch["size"] = pd.to_numeric(ch["size"], errors="coerce")
        ch["timestamp"] = pd.to_numeric(ch["timestamp"], errors="coerce")
        ch["_market_start_ts"] = pd.to_numeric(ch["_market_start_ts"], errors="coerce")
        ch = ch.dropna(subset=["price", "size", "timestamp", "_market_start_ts"])
        ch = ch[(ch["price"] >= 0) & (ch["price"] <= 1) & (ch["size"] > 0)].copy()

        ch["seconds_from_start"] = (ch["timestamp"] - ch["_market_start_ts"]).astype(int)
        ch = ch[
            (ch["seconds_from_start"] >= args.min_second)
            & (ch["seconds_from_start"] <= args.max_second)
        ].copy()
        if ch.empty:
            continue

        ch["time_bin_start_sec"] = (ch["seconds_from_start"] // args.time_bin_seconds) * args.time_bin_seconds
        ch["time_bin_end_sec"] = ch["time_bin_start_sec"] + args.time_bin_seconds
        ch["time_bin"] = ch["time_bin_start_sec"].map(lambda x: f"{int(x):03d}-{int(x + args.time_bin_seconds):03d}s")
        ch["price_bin"] = ch["price"].map(lambda p: price_bin_label(float(p), args.price_bin_width))
        ch = ch[ch["price_bin"].notna()].copy()
        ch["is_winner"] = (ch["_outcome_norm"] == ch["_final_outcome"]).astype(int)
        ch["cost"] = ch["price"] * ch["size"]
        ch["payout"] = ch["is_winner"] * ch["size"]

        g = ch.groupby(
            ["side", "time_bin_start_sec", "time_bin", "price_bin"],
            as_index=False,
            dropna=False,
        ).agg(
            rows=("price", "size"),
            size=("size", "sum"),
            cost=("cost", "sum"),
            payout=("payout", "sum"),
        )
        for r in g.itertuples(index=False):
            key = (r.side, int(r.time_bin_start_sec), r.time_bin, r.price_bin)
            bump(store, key, rows=r.rows, size=r.size, cost=r.cost, payout=r.payout)

    out = finalize(store, ["side", "time_bin_start_sec", "time_bin", "price_bin"])
    out = out.sort_values(["side", "time_bin_start_sec", "price_bin"])
    out.to_csv(args.outdir / "price_bin_002_time_bin_10s_realized_roi_by_side.csv", index=False)

    sell = out[out["side"].eq("sell")].copy()
    sell.to_csv(args.outdir / "price_bin_002_time_bin_10s_realized_roi_sell_only.csv", index=False)
    print(f"wrote {args.outdir / 'price_bin_002_time_bin_10s_realized_roi_by_side.csv'} rows={len(out)}")
    print(f"wrote {args.outdir / 'price_bin_002_time_bin_10s_realized_roi_sell_only.csv'} rows={len(sell)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades-csv", type=Path, default=Path("out_btc5m_7d_full_strategy/trades_raw.csv"))
    parser.add_argument("--outdir", type=Path, default=Path("analysis_clean/btc5m_7d_conservative"))
    parser.add_argument("--time-bin-seconds", type=int, default=10)
    parser.add_argument("--price-bin-width", type=float, default=0.02)
    parser.add_argument("--min-second", type=int, default=0)
    parser.add_argument("--max-second", type=int, default=299)
    parser.add_argument("--chunksize", type=int, default=250_000)
    args = parser.parse_args()
    analyze(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plot every UP trade curve by taker side and final outcome."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go


def load_curves(args: argparse.Namespace) -> pd.DataFrame:
    usecols = [
        "side",
        "price",
        "size",
        "timestamp",
        "_condition_id",
        "_market_start_ts",
        "_market_start_dt_utc",
        "_outcome_norm",
        "_final_outcome",
    ]
    parts = []
    for ch in pd.read_csv(args.trades_csv, usecols=usecols, chunksize=args.chunksize, dtype=str):
        ch["side"] = ch["side"].astype(str).str.lower().str.strip()
        ch["_outcome_norm"] = ch["_outcome_norm"].astype(str).str.lower().str.strip()
        ch["_final_outcome"] = ch["_final_outcome"].astype(str).str.lower().str.strip()
        ch = ch[
            ch["side"].eq(args.side.lower())
            & ch["_outcome_norm"].eq("up")
            & ch["_final_outcome"].eq(args.final_outcome.lower())
        ].copy()
        if ch.empty:
            continue

        ch["price"] = pd.to_numeric(ch["price"], errors="coerce")
        ch["size"] = pd.to_numeric(ch["size"], errors="coerce")
        ch["timestamp"] = pd.to_numeric(ch["timestamp"], errors="coerce")
        ch["_market_start_ts"] = pd.to_numeric(ch["_market_start_ts"], errors="coerce")
        ch = ch.dropna(subset=["price", "size", "timestamp", "_market_start_ts"])
        ch = ch[(ch["size"] > 0) & (ch["price"] >= 0) & (ch["price"] <= 1)].copy()
        ch["minute_from_start"] = np.floor((ch["timestamp"] - ch["_market_start_ts"]) / 60).astype(int)
        ch = ch[(ch["minute_from_start"] >= 0) & (ch["minute_from_start"] <= args.max_minute)].copy()
        if ch.empty:
            continue
        parts.append(ch)

    if not parts:
        return pd.DataFrame()

    df = pd.concat(parts, ignore_index=True)
    # Choose markets with the most complete 0..max_minute coverage, then highest trade count.
    coverage = (
        df.groupby(["_condition_id", "_market_start_dt_utc"], as_index=False)
        .agg(minutes=("minute_from_start", "nunique"), rows=("price", "size"), size_sum=("size", "sum"))
        .sort_values(["minutes", "rows", "size_sum"], ascending=[False, False, False])
        .head(args.markets)
    )
    df["minute_x"] = (df["timestamp"] - df["_market_start_ts"]) / 60.0
    return df.merge(coverage[["_condition_id"]], on="_condition_id", how="inner")


def write_plot(curves: pd.DataFrame, out_html: Path, max_minute: int, side: str, final_outcome: str) -> None:
    fig = go.Figure()
    for idx, (condition_id, g) in enumerate(curves.groupby("_condition_id")):
        g = g.sort_values(["timestamp", "price"])
        label_dt = str(g["_market_start_dt_utc"].iloc[0]).replace("T", " ").replace("+00:00", " UTC")
        fig.add_trace(
            go.Scatter(
                x=g["minute_x"],
                y=g["price"],
                mode="lines+markers",
                name=f"{idx + 1}: {label_dt}",
                customdata=np.stack([g["minute_from_start"], g["size"], g["timestamp"]], axis=-1),
                hovertemplate=(
                    "minute_x=%{x:.3f}<br>"
                    "minute=%{customdata[0]}<br>"
                    "price=%{y:.4f}<br>"
                    "size=%{customdata[1]:.2f}<br>"
                    "timestamp=%{customdata[2]}<extra></extra>"
                ),
            )
        )

    fig.update_layout(
        title=f"Every UP {side.upper()} trade for markets resolved {final_outcome.upper()}",
        xaxis=dict(title="Minute from market start", tickmode="array", tickvals=list(range(max_minute + 1))),
        yaxis=dict(title="Trade price", range=[0, 1], dtick=0.05),
        legend_title_text="Market start time",
        margin=dict(l=50, r=20, t=60, b=45),
    )
    out_html.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(out_html, include_plotlyjs="cdn")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades-csv", type=Path, default=Path("out_btc5m_7d_full_strategy/trades_raw.csv"))
    parser.add_argument("--out-html", type=Path, default=None)
    parser.add_argument("--markets", type=int, default=100)
    parser.add_argument("--max-minute", type=int, default=4)
    parser.add_argument("--side", choices=["BUY", "SELL", "buy", "sell"], default="SELL")
    parser.add_argument("--final-outcome", choices=["UP", "DOWN", "up", "down"], default="UP")
    parser.add_argument("--chunksize", type=int, default=250_000)
    args = parser.parse_args()
    if args.out_html is None:
        args.out_html = Path(
            f"analysis_clean/btc5m_7d_conservative/up_{args.side.lower()}_resolve_{args.final_outcome.lower()}_100_market_curves.html"
        )

    curves = load_curves(args)
    if curves.empty:
        raise SystemExit(f"No UP {args.side.upper()} resolved-{args.final_outcome.upper()} trades found.")
    write_plot(curves, args.out_html, args.max_minute, args.side, args.final_outcome)
    print(f"wrote {args.out_html} markets={curves['_condition_id'].nunique()} rows={len(curves)}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plot one hour of BTC 5m trade prices."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go


def find_busiest_hour(trades_csv: Path, chunksize: int) -> pd.Timestamp:
    counts = []
    for ch in pd.read_csv(trades_csv, usecols=["timestamp"], chunksize=chunksize, dtype=str):
        ts = pd.to_numeric(ch["timestamp"], errors="coerce")
        hour = pd.to_datetime(ts, unit="s", utc=True).dt.floor("h")
        counts.append(hour.value_counts())
    return pd.concat(counts).groupby(level=0).sum().idxmax()


def load_hour(trades_csv: Path, start: pd.Timestamp, chunksize: int) -> pd.DataFrame:
    end = start + pd.Timedelta(hours=1)
    usecols = [
        "side",
        "price",
        "size",
        "timestamp",
        "_market_start_ts",
        "_market_start_dt_utc",
        "_outcome_norm",
        "_final_outcome",
        "_condition_id",
    ]
    parts = []
    for ch in pd.read_csv(trades_csv, usecols=usecols, chunksize=chunksize, dtype=str):
        ch["timestamp"] = pd.to_numeric(ch["timestamp"], errors="coerce")
        ch["datetime_utc"] = pd.to_datetime(ch["timestamp"], unit="s", utc=True)
        ch = ch[(ch["datetime_utc"] >= start) & (ch["datetime_utc"] < end)].copy()
        if ch.empty:
            continue
        ch["price"] = pd.to_numeric(ch["price"], errors="coerce")
        ch["size"] = pd.to_numeric(ch["size"], errors="coerce")
        ch["_market_start_ts"] = pd.to_numeric(ch["_market_start_ts"], errors="coerce")
        ch["market_start_utc"] = pd.to_datetime(ch["_market_start_ts"], unit="s", utc=True)
        ch["minute_from_market_start"] = ((ch["timestamp"] - ch["_market_start_ts"]) // 60).astype("Int64")
        ch["side"] = ch["side"].astype(str).str.upper()
        ch["token"] = ch["_outcome_norm"].astype(str).str.upper()
        ch["final"] = ch["_final_outcome"].astype(str).str.upper()
        parts.append(ch)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True).dropna(subset=["price", "size", "datetime_utc"])


def write_plot(df: pd.DataFrame, out_html: Path, start: pd.Timestamp) -> None:
    end = start + pd.Timedelta(hours=1)
    title = f"BTC 5m trade prices, {start:%Y-%m-%d %H:%M} to {end:%H:%M} UTC"
    fig = go.Figure()
    colors = {"UP": "#2563eb", "DOWN": "#dc2626"}
    symbols = {"BUY": "circle", "SELL": "x"}
    for (token, side), g in df.sort_values("datetime_utc").groupby(["token", "side"]):
        marker_size = 4 + 8 * (g["size"].clip(lower=0, upper=g["size"].quantile(0.98)) / g["size"].quantile(0.98))
        hover = [
            f"time={row.datetime_utc}<br>"
            f"market_start={row.market_start_utc}<br>"
            f"minute={row.minute_from_market_start}<br>"
            f"token={row.token}<br>"
            f"side={row.side}<br>"
            f"price={row.price:.4f}<br>"
            f"size={row.size:.2f}<br>"
            f"final={row.final}"
            for row in g.itertuples(index=False)
        ]
        fig.add_trace(
            go.Scattergl(
                x=g["datetime_utc"],
                y=g["price"],
                mode="markers",
                name=f"{token} {side}",
                marker=dict(
                    color=colors.get(token, "#444"),
                    symbol=symbols.get(side, "circle"),
                    size=marker_size,
                    opacity=0.55,
                    line=dict(width=0),
                ),
                text=hover,
                hovertemplate="%{text}<extra></extra>",
            )
        )
    for t in pd.date_range(start=start, end=end, freq="5min"):
        fig.add_vline(x=t, line_width=1, line_dash="dot", line_color="rgba(80,80,80,0.35)")
    fig.update_layout(
        title=title,
        xaxis_title="Trade time UTC, 5-minute market intervals",
        yaxis_title="Trade price",
        yaxis=dict(range=[0, 1]),
        legend_title_text="Token / taker side",
        margin=dict(l=45, r=20, t=60, b=45),
    )
    out_html.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(out_html, include_plotlyjs="cdn")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades-csv", type=Path, default=Path("out_btc5m_7d_full_strategy/trades_raw.csv"))
    parser.add_argument("--out-html", type=Path, default=Path("analysis_clean/btc5m_7d_conservative/one_hour_trade_prices.html"))
    parser.add_argument("--start-utc", type=str, default=None, help="Example: 2026-05-10T15:00:00Z")
    parser.add_argument("--chunksize", type=int, default=250_000)
    args = parser.parse_args()

    if args.start_utc:
        start = pd.Timestamp(args.start_utc)
        if start.tzinfo is None:
            start = start.tz_localize("UTC")
        else:
            start = start.tz_convert("UTC")
        start = start.floor("h")
    else:
        start = find_busiest_hour(args.trades_csv, args.chunksize)

    df = load_hour(args.trades_csv, start, args.chunksize)
    if df.empty:
        raise SystemExit(f"No trades found for hour starting {start}.")
    write_plot(df, args.out_html, start)
    print(f"wrote {args.out_html} rows={len(df)} hour_start={start}")


if __name__ == "__main__":
    main()

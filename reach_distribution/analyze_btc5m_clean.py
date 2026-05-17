#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clean analysis pipeline for Polymarket BTC 5m Up/Down trades.

This script does not download data. It reads an existing trades_raw.csv and
writes compact, reproducible analysis outputs only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go


PRICE_BANDS = [
    (0.01, 0.10),
    (0.10, 0.20),
    (0.20, 0.30),
    (0.30, 0.40),
    (0.40, 0.50),
    (0.50, 0.60),
    (0.60, 0.70),
    (0.70, 0.80),
    (0.80, 0.90),
    (0.90, 1.000001),
]


def band_label(lo: float, hi: float) -> str:
    return f"{lo:.2f}-{(1.0 if hi > 1 else hi):.2f}"


def band_midpoint(label: str) -> float:
    lo, hi = label.split("-", 1)
    return (float(lo) + float(hi)) / 2


def safe_div(num: float, den: float) -> float:
    return float(num / den) if den else float("nan")


def load_refs(path: Path | None, trades_csv: Path) -> pd.DataFrame:
    if path and path.exists():
        refs = pd.read_csv(path, dtype=str)
        if "condition_id" in refs.columns and "final_outcome" in refs.columns:
            refs["condition_id"] = refs["condition_id"].astype(str)
            refs["final_outcome"] = refs["final_outcome"].astype(str).str.lower()
            return refs[["condition_id", "final_outcome"]].drop_duplicates()

    pairs: list[pd.DataFrame] = []
    for ch in pd.read_csv(
        trades_csv,
        usecols=["_condition_id", "_final_outcome"],
        chunksize=250_000,
        dtype=str,
    ):
        pairs.append(
            ch.rename(columns={"_condition_id": "condition_id", "_final_outcome": "final_outcome"})
            .drop_duplicates()
        )
    refs = pd.concat(pairs, ignore_index=True).drop_duplicates()
    refs["condition_id"] = refs["condition_id"].astype(str)
    refs["final_outcome"] = refs["final_outcome"].astype(str).str.lower()
    return refs


def add_band(df: pd.DataFrame) -> pd.DataFrame:
    labels = []
    for price in df["price"].to_numpy():
        label = None
        for lo, hi in PRICE_BANDS:
            if lo <= price < hi:
                label = band_label(lo, hi)
                break
        labels.append(label)
    df["price_band"] = labels
    return df[df["price_band"].notna()].copy()


def finalize_group(rows: dict[tuple[Any, ...], dict[str, float]], names: list[str]) -> pd.DataFrame:
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
        out.append(item)
    return pd.DataFrame(out)


def bump(store: dict[tuple[Any, ...], dict[str, float]], key: tuple[Any, ...], **vals: float) -> None:
    d = store.setdefault(key, {})
    for k, v in vals.items():
        d[k] = d.get(k, 0.0) + float(v)


def analyze(args: argparse.Namespace) -> None:
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    refs = load_refs(args.refs_csv, args.trades_csv)
    market_counts = refs["final_outcome"].value_counts().to_dict()

    dataset: dict[str, Any] = {
        "trades_csv": str(args.trades_csv),
        "refs_csv": str(args.refs_csv) if args.refs_csv else None,
        "max_minute": args.max_minute,
        "q": args.q,
        "markets_total": int(refs["condition_id"].nunique()),
        "final_up_markets": int(market_counts.get("up", 0)),
        "final_down_markets": int(market_counts.get("down", 0)),
    }

    minute_rows: dict[tuple[Any, ...], dict[str, float]] = {}
    band_rows: dict[tuple[Any, ...], dict[str, float]] = {}
    band_minute_rows: dict[tuple[Any, ...], dict[str, float]] = {}
    side_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    final_counts: dict[str, int] = {}
    total_rows = 0
    dedupe_seen: set[tuple[str, ...]] = set()
    duplicate_rows = 0

    # Market-level fill tables: side prediction token volume by market, minute, band.
    fill_parts: list[pd.DataFrame] = []

    usecols = [
        "side",
        "asset",
        "price",
        "size",
        "timestamp",
        "transactionHash",
        "_condition_id",
        "_market_start_ts",
        "_final_outcome",
        "_outcome_norm",
    ]

    for ch in pd.read_csv(args.trades_csv, usecols=usecols, chunksize=args.chunksize, dtype=str):
        total_rows += len(ch)
        ch["side"] = ch["side"].astype(str).str.lower().str.strip()
        ch["_outcome_norm"] = ch["_outcome_norm"].astype(str).str.lower().str.strip()
        ch["_final_outcome"] = ch["_final_outcome"].astype(str).str.lower().str.strip()
        ch["_condition_id"] = ch["_condition_id"].astype(str)

        for k, v in ch["side"].value_counts().items():
            side_counts[k] = side_counts.get(k, 0) + int(v)
        for k, v in ch["_outcome_norm"].value_counts().items():
            outcome_counts[k] = outcome_counts.get(k, 0) + int(v)
        for k, v in ch["_final_outcome"].value_counts().items():
            final_counts[k] = final_counts.get(k, 0) + int(v)

        keycols = ["transactionHash", "_condition_id", "asset", "_outcome_norm", "price", "size", "timestamp"]
        for key in map(tuple, ch[keycols].astype(str).itertuples(index=False, name=None)):
            if key in dedupe_seen:
                duplicate_rows += 1
            else:
                dedupe_seen.add(key)

        ch["price"] = pd.to_numeric(ch["price"], errors="coerce")
        ch["size"] = pd.to_numeric(ch["size"], errors="coerce")
        ch["timestamp"] = pd.to_numeric(ch["timestamp"], errors="coerce")
        ch["_market_start_ts"] = pd.to_numeric(ch["_market_start_ts"], errors="coerce")
        ch = ch.dropna(subset=["price", "size", "timestamp", "_market_start_ts"])
        ch = ch[(ch["price"] >= 0) & (ch["price"] <= 1) & (ch["size"] > 0)].copy()
        ch["minute_from_start"] = np.floor((ch["timestamp"] - ch["_market_start_ts"]) / 60).astype(int)
        ch = ch[(ch["minute_from_start"] >= args.min_minute) & (ch["minute_from_start"] <= args.max_minute)].copy()
        if ch.empty:
            continue

        ch["is_winner"] = (ch["_outcome_norm"] == ch["_final_outcome"]).astype(int)
        ch["cost"] = ch["price"] * ch["size"]
        ch["payout"] = ch["is_winner"] * ch["size"]
        ch = add_band(ch)

        for keys, store, names in [
            (["side", "minute_from_start"], minute_rows, ["side", "minute_from_start"]),
            (["side", "price_band"], band_rows, ["side", "price_band"]),
            (["side", "minute_from_start", "price_band"], band_minute_rows, ["side", "minute_from_start", "price_band"]),
        ]:
            g = ch.groupby(keys, dropna=False).agg(
                rows=("price", "size"),
                size=("size", "sum"),
                cost=("cost", "sum"),
                payout=("payout", "sum"),
            )
            for idx, r in g.iterrows():
                key = idx if isinstance(idx, tuple) else (idx,)
                bump(store, key, rows=r["rows"], size=r["size"], cost=r["cost"], payout=r["payout"])

        sell = ch[ch["side"].eq("sell")].copy()
        if not sell.empty:
            fill_parts.append(
                sell.groupby(
                    ["_condition_id", "_final_outcome", "_outcome_norm", "minute_from_start", "price_band"],
                    as_index=False,
                )
                .agg(volume=("size", "sum"))
            )

    dataset.update(
        total_trade_rows=int(total_rows),
        duplicate_dedupe_key_rows=int(duplicate_rows),
        side_counts=side_counts,
        outcome_counts=outcome_counts,
        final_counts=final_counts,
    )
    (outdir / "dataset_quality.json").write_text(json.dumps(dataset, indent=2), encoding="utf-8")

    minute_df = finalize_group(minute_rows, ["side", "minute_from_start"]).sort_values(["side", "minute_from_start"])
    band_df = finalize_group(band_rows, ["side", "price_band"]).sort_values(["side", "price_band"])
    band_minute_df = finalize_group(band_minute_rows, ["side", "minute_from_start", "price_band"]).sort_values(
        ["side", "minute_from_start", "price_band"]
    )
    minute_df.to_csv(outdir / "minute_summary_by_side.csv", index=False)
    band_df.to_csv(outdir / "price_band_realized_roi_by_side.csv", index=False)
    band_minute_df.to_csv(outdir / "price_band_minute_realized_roi_by_side.csv", index=False)

    strategy_df = build_strategy_ev(fill_parts, refs, args.q)
    strategy_df.to_csv(outdir / "strategy_ev_q60_passive_buy_sell_fills.csv", index=False)
    write_ev_3d_plots(outdir, strategy_df)
    write_a_win_3d_plots(outdir, strategy_df)

    write_report(outdir, dataset, band_df, band_minute_df, strategy_df, args.q)


def build_strategy_ev(fill_parts: list[pd.DataFrame], refs: pd.DataFrame, q: float) -> pd.DataFrame:
    if not fill_parts:
        return pd.DataFrame()
    fills = pd.concat(fill_parts, ignore_index=True)
    fills = fills.groupby(
        ["_condition_id", "_final_outcome", "_outcome_norm", "minute_from_start", "price_band"],
        as_index=False,
    ).agg(volume=("volume", "sum"))

    denom = refs.groupby("final_outcome")["condition_id"].nunique().to_dict()
    rows = []
    for pred in ["up", "down"]:
        wrong = "down" if pred == "up" else "up"
        for minute in sorted(fills["minute_from_start"].unique()):
            for band in [band_label(lo, hi) for lo, hi in PRICE_BANDS]:
                win = fills[
                    (fills["_outcome_norm"] == pred)
                    & (fills["_final_outcome"] == pred)
                    & (fills["minute_from_start"] == minute)
                    & (fills["price_band"] == band)
                ]
                lose = fills[
                    (fills["_outcome_norm"] == pred)
                    & (fills["_final_outcome"] == wrong)
                    & (fills["minute_from_start"] == minute)
                    & (fills["price_band"] == band)
                ]
                win_markets = int(win["_condition_id"].nunique())
                lose_markets = int(lose["_condition_id"].nunique())
                a_win = safe_div(win_markets, int(denom.get(pred, 0)))
                a_lose = safe_div(lose_markets, int(denom.get(wrong, 0)))
                price_mid = band_midpoint(band)
                ev_per_share = q * a_win * (1.0 - price_mid) - (1.0 - q) * price_mid
                rows.append(
                    {
                        "prediction_side": pred,
                        "minute_from_start": int(minute),
                        "price_band": band,
                        "price_mid": price_mid,
                        "q": q,
                        "a_win_market_fill": a_win,
                        "a_lose_market_fill": a_lose,
                        "win_fill_markets": win_markets,
                        "lose_fill_markets": lose_markets,
                        "ev_per_share_opportunity": ev_per_share,
                    }
                )
    return pd.DataFrame(rows).sort_values("ev_per_share_opportunity", ascending=False)


def write_ev_3d_plots(outdir: Path, strategy_df: pd.DataFrame) -> None:
    write_3d_bar_plots(
        outdir=outdir,
        strategy_df=strategy_df,
        value_col="ev_per_share_opportunity",
        z_title="EV/share opportunity",
        colorbar_title="EV/share",
        filename_prefix="strategy_ev_3d",
        title_suffix="conservative EV, q=0.60, b=1",
        hover_value_label="EV",
    )


def write_a_win_3d_plots(outdir: Path, strategy_df: pd.DataFrame) -> None:
    write_3d_bar_plots(
        outdir=outdir,
        strategy_df=strategy_df,
        value_col="a_win_market_fill",
        z_title="a_win_market_fill",
        colorbar_title="a_win",
        filename_prefix="a_win_market_fill_3d",
        title_suffix="a_win market fill",
        hover_value_label="a_win",
    )


def write_3d_bar_plots(
    outdir: Path,
    strategy_df: pd.DataFrame,
    value_col: str,
    z_title: str,
    colorbar_title: str,
    filename_prefix: str,
    title_suffix: str,
    hover_value_label: str,
) -> None:
    if strategy_df.empty:
        return

    for pred in ["up", "down"]:
        df = strategy_df[strategy_df["prediction_side"].eq(pred)].copy()
        if df.empty:
            continue
        df = df.sort_values(["minute_from_start", "price_mid"])
        x = df["minute_from_start"].astype(float).to_numpy()
        y = df["price_mid"].astype(float).to_numpy()
        z = np.zeros(len(df))
        dz = df[value_col].astype(float).to_numpy()
        dx = np.full(len(df), 0.42)
        dy = np.full(len(df), 0.055)

        vertices_x: list[float] = []
        vertices_y: list[float] = []
        vertices_z: list[float] = []
        i: list[int] = []
        j: list[int] = []
        k: list[int] = []
        colors: list[float] = []

        faces = [
            (0, 1, 2), (0, 2, 3),
            (4, 6, 5), (4, 7, 6),
            (0, 4, 5), (0, 5, 1),
            (1, 5, 6), (1, 6, 2),
            (2, 6, 7), (2, 7, 3),
            (3, 7, 4), (3, 4, 0),
        ]

        for n, (cx, cy, height, width_x, width_y) in enumerate(zip(x, y, dz, dx, dy)):
            base = len(vertices_x)
            x0, x1 = cx - width_x / 2, cx + width_x / 2
            y0, y1 = cy - width_y / 2, cy + width_y / 2
            z0, z1 = (0.0, height) if height >= 0 else (height, 0.0)
            corners = [
                (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
            ]
            for vx, vy, vz in corners:
                vertices_x.append(float(vx))
                vertices_y.append(float(vy))
                vertices_z.append(float(vz))
                colors.append(float(height))
            for a, b, c in faces:
                i.append(base + a)
                j.append(base + b)
                k.append(base + c)

        fig = go.Figure(
            data=[
                go.Mesh3d(
                    x=vertices_x,
                    y=vertices_y,
                    z=vertices_z,
                    i=i,
                    j=j,
                    k=k,
                    intensity=colors,
                    colorscale="RdYlGn",
                    cmin=float(strategy_df[value_col].min()),
                    cmax=float(strategy_df[value_col].max()),
                    colorbar_title=colorbar_title,
                    flatshading=True,
                    hoverinfo="skip",
                ),
                go.Scatter3d(
                    x=x,
                    y=y,
                    z=dz,
                    mode="markers",
                    marker=dict(size=3, color=dz, colorscale="RdYlGn"),
                    text=[
                        f"{pred} minute={int(row.minute_from_start)}<br>"
                        f"price={row.price_band}<br>"
                        f"{hover_value_label}={getattr(row, value_col):.5f}<br>"
                        f"EV={row.ev_per_share_opportunity:.5f}<br>"
                        f"a_win={row.a_win_market_fill:.4f}<br>"
                        f"win_markets={int(row.win_fill_markets)}"
                        for row in df.itertuples(index=False)
                    ],
                    hovertemplate="%{text}<extra></extra>",
                    showlegend=False,
                ),
            ]
        )
        fig.update_layout(
            title=f"{pred.upper()} {title_suffix}",
            scene=dict(
                xaxis_title="Minute from start",
                yaxis_title="Price band midpoint",
                zaxis_title=z_title,
            ),
            margin=dict(l=0, r=0, t=45, b=0),
        )
        fig.write_html(outdir / f"{filename_prefix}_{pred}.html", include_plotlyjs="cdn")


def write_report(
    outdir: Path,
    dataset: dict[str, Any],
    band_df: pd.DataFrame,
    band_minute_df: pd.DataFrame,
    strategy_df: pd.DataFrame,
    q: float,
) -> None:
    sell_band = band_df[band_df["side"].eq("sell")].copy()
    top_realized = sell_band.sort_values("roi_on_cost", ascending=False).head(5)
    positive = strategy_df[strategy_df["ev_per_share_opportunity"] > 0].head(12)

    lines = [
        "# BTC 5m Clean Analysis Report",
        "",
        "## Data",
        "",
        f"- Raw trades: `{dataset['trades_csv']}`",
        f"- Trade rows: `{dataset['total_trade_rows']:,}`",
        f"- Duplicate rows by strict trade key: `{dataset['duplicate_dedupe_key_rows']:,}`",
        f"- Markets: `{dataset['markets_total']}` (`up={dataset['final_up_markets']}`, `down={dataset['final_down_markets']}`)",
        f"- Analysis window: minute `{dataset['max_minute']}` and earlier",
        "",
        "## Realized Passive-Buy Edge From Taker SELL Fills",
        "",
        "These rows answer: if a passive buy was filled by taker SELL in a price band, what happened ex post?",
        "",
        top_realized.to_markdown(index=False),
        "",
        f"## Model-Conditioned Strategy EV, q={q:.2f}",
        "",
        "This uses conservative b=1 loss handling: if the prediction is wrong, the passive bid is assumed to fill and settle to zero.",
        "`a_win_market_fill = win_fill_markets / final_markets_for_prediction_side`; only the profit side is discounted by observed taker SELL fill probability.",
        "",
        positive.to_markdown(index=False) if not positive.empty else "No positive EV candidates.",
        "",
        "## Recommended q=0.60 Policy",
        "",
        "- Do not run the low-price reversal strategy below 0.50; it is negative EV in this sample.",
        "- With q=0.60, only consider passive buys in strong market-confirmed bands, primarily 0.70-0.90.",
        "- Use minute 2-4 only; cancel all unfilled orders before the market rolls into post-window trading.",
        "- Prefer side=SELL fills for all fill-probability estimates because the strategy is a passive limit buy.",
        "- Keep per-market risk small because the backtest is only seven days and fill queue priority is not modeled.",
        "",
    ]
    (outdir / "ANALYSIS_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades-csv", type=Path, default=Path("out_btc5m_7d_full_strategy/trades_raw.csv"))
    parser.add_argument("--refs-csv", type=Path, default=Path("out_btc5m_7d_full_strategy/market_refs_resolved.csv"))
    parser.add_argument("--outdir", type=Path, default=Path("analysis_clean/btc5m_7d"))
    parser.add_argument("--min-minute", type=int, default=0)
    parser.add_argument("--max-minute", type=int, default=4)
    parser.add_argument("--q", type=float, default=0.60)
    parser.add_argument("--chunksize", type=int, default=250_000)
    args = parser.parse_args()
    analyze(args)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
Compute per-minute price / probability distributions for Polymarket BTC 5-minute Up/Down markets.

Input CSV schema example:
proxyWallet,side,asset,conditionId,size,price,timestamp,title,slug,...,
_market_start_ts,_market_start_dt_utc,_final_outcome,_up_token_id,_down_token_id,_outcome_norm,datetime_utc

Main idea:
- A Polymarket binary market has two outcome tokens: Up and Down.
- The traded token price is the implied probability of THAT token's outcome.
- To put both tokens on the same axis, convert everything into implied BTC-Up probability:
    If token is Up:   up_prob = price
    If token is Down: up_prob = 1 - price

Example:
- Up token trades at 0.62   => implied BTC-Up probability = 0.62
- Down token trades at 0.62 => implied BTC-Up probability = 1 - 0.62 = 0.38

Outputs:
- summary_by_minute.csv
- summary_by_minute_outcome.csv
- summary_by_minute_winloss.csv
- trades_with_minute_and_probs.csv
- dist_up_prob_by_minute.long.csv
- dist_up_prob_by_minute.pivot_count.csv
- dist_up_prob_by_minute.pivot_size.csv
- dist_token_price_by_minute_outcome.long.csv
- dist_token_price_by_minute_winloss.long.csv
- heatmap_up_prob_by_market_minute_size_weighted.png
- heatmap_up_prob_by_market_minute_count_weighted.png

Usage:
python btc5m_minute_price_distribution.py \
  --csv out_btc5m_7d_full_strategy/trades_raw.csv \
  --outdir out_btc5m_7d_full_strategy/minute_price_dist \
  --bin-width 0.01 \
  --max-minute 6

For strict in-market 5-minute window only:
python btc5m_minute_price_distribution.py --max-minute 4
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


def safe_lower_str(s: pd.Series) -> pd.Series:
    return s.astype("string").str.lower().str.strip()


def assign_price_bin(x: pd.Series, bin_width: float) -> pd.Series:
    """
    Convert a probability/price into left-closed bucket labels.

    Example:
    price=0.534, bin_width=0.01 -> 0.53
    price=0.999, bin_width=0.01 -> 0.99
    price=1.000, bin_width=0.01 -> 1.00
    """
    values = pd.to_numeric(x, errors="coerce").astype(float)
    b = np.floor(values / bin_width) * bin_width
    b = np.clip(b, 0.0, 1.0)
    return np.round(b, 6)


def resolve_existing_path(path_str: str) -> Path:
    """
    Make the script friendlier across Windows/macOS/Linux.
    The user may pass either out_dir/trades_raw.csv or out_dir\\trades_raw.csv.
    """
    p = Path(path_str)
    if p.exists():
        return p

    alt = Path(path_str.replace("\\", "/"))
    if alt.exists():
        return alt

    raise FileNotFoundError(
        f"CSV not found: {path_str}\n"
        f"Also tried: {alt}\n"
        "Please pass the correct path with --csv."
    )


def make_distribution(
    df: pd.DataFrame,
    value_col: str,
    group_cols: list[str],
    bin_width: float,
    out_prefix: Path,
    weight_col: str = "size",
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Create distribution tables.

    Long output columns:
      group columns + price_bin + n_trades + size_sum + prob_by_count + prob_by_size

    Pivot count:
      index = group columns, columns = price_bin, values = prob_by_count

    Pivot size:
      index = group columns, columns = price_bin, values = prob_by_size
    """
    d = df.copy()
    d["price_bin"] = assign_price_bin(d[value_col], bin_width)

    g = (
        d.groupby(group_cols + ["price_bin"], dropna=False)
        .agg(
            n_trades=(value_col, "size"),
            size_sum=(weight_col, "sum"),
            price_mean=(value_col, "mean"),
            price_median=(value_col, "median"),
        )
        .reset_index()
    )

    totals = (
        g.groupby(group_cols, dropna=False)
        .agg(
            total_trades=("n_trades", "sum"),
            total_size=("size_sum", "sum"),
        )
        .reset_index()
    )

    g = g.merge(totals, on=group_cols, how="left")
    g["prob_by_count"] = g["n_trades"] / g["total_trades"]
    g["prob_by_size"] = np.where(
        g["total_size"] > 0,
        g["size_sum"] / g["total_size"],
        np.nan,
    )

    long_path = out_prefix.with_suffix(".long.csv")
    g.to_csv(long_path, index=False)

    pivot_count = g.pivot_table(
        index=group_cols,
        columns="price_bin",
        values="prob_by_count",
        fill_value=0.0,
    ).reset_index()

    pivot_size = g.pivot_table(
        index=group_cols,
        columns="price_bin",
        values="prob_by_size",
        fill_value=0.0,
    ).reset_index()

    pivot_count_path = out_prefix.with_suffix(".pivot_count.csv")
    pivot_size_path = out_prefix.with_suffix(".pivot_size.csv")

    pivot_count.to_csv(pivot_count_path, index=False)
    pivot_size.to_csv(pivot_size_path, index=False)

    return g, pivot_count, pivot_size


def plot_heatmap_from_long(
    dist_long: pd.DataFrame,
    index_col: str,
    price_col: str,
    value_col: str,
    title: str,
    out_path: Path,
) -> None:
    """
    Plot a simple heatmap:
      x = price/probability bin
      y = minute_from_start
      color = prob_by_size or prob_by_count
    """
    mat = dist_long.pivot_table(
        index=index_col,
        columns=price_col,
        values=value_col,
        fill_value=0.0,
    )

    mat = mat.sort_index()
    mat = mat.reindex(sorted(mat.columns), axis=1)

    plt.figure(figsize=(14, 6))
    plt.imshow(mat.values, aspect="auto", origin="lower")
    plt.colorbar(label=value_col)

    plt.yticks(
        ticks=np.arange(len(mat.index)),
        labels=[str(x) for x in mat.index],
    )

    step = max(1, len(mat.columns) // 20)
    x_ticks = np.arange(0, len(mat.columns), step)
    x_labels = [f"{mat.columns[i]:.2f}" for i in x_ticks]
    plt.xticks(ticks=x_ticks, labels=x_labels, rotation=45)

    plt.xlabel("price / probability bin")
    plt.ylabel(index_col)
    plt.title(title)
    plt.tight_layout()
    plt.savefig(out_path, dpi=160)
    plt.close()


def validate_columns(df: pd.DataFrame, required_cols: Iterable[str]) -> None:
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv",
        default="out_btc5m_7d_full_strategy/trades_raw.csv",
        help="Path to trades_raw.csv",
    )
    parser.add_argument(
        "--outdir",
        default="out_btc5m_7d_full_strategy/minute_price_dist",
        help="Output directory",
    )
    parser.add_argument(
        "--bin-width",
        type=float,
        default=0.01,
        help="Price/probability bin width. 0.01 means 1-cent probability buckets.",
    )
    parser.add_argument(
        "--min-minute",
        type=int,
        default=0,
        help="Minimum minute_from_start to keep.",
    )
    parser.add_argument(
        "--max-minute",
        type=int,
        default=6,
        help=(
            "Maximum minute_from_start to keep. "
            "Use 4 for strict in-market 0-4 only. "
            "Default 6 keeps two late minutes because trade data may include post-window trades."
        ),
    )
    parser.add_argument(
        "--side-filter",
        choices=["all", "buy", "sell"],
        default="all",
        help="Keep only one taker side before computing distributions. Use sell to match passive limit-buy fill assumptions.",
    )
    args = parser.parse_args()

    if not (0 < args.bin_width <= 1):
        raise ValueError("--bin-width must be in (0, 1].")

    csv_path = resolve_existing_path(args.csv)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"Reading: {csv_path}")
    df = pd.read_csv(csv_path)

    validate_columns(
        df,
        required_cols=[
            "price",
            "size",
            "datetime_utc",
            "_market_start_dt_utc",
            "_final_outcome",
        ],
    )

    # -----------------------------
    # 1. Basic cleaning
    # -----------------------------
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["size"] = pd.to_numeric(df["size"], errors="coerce")

    df["datetime_utc"] = pd.to_datetime(df["datetime_utc"], utc=True, errors="coerce")
    df["_market_start_dt_utc"] = pd.to_datetime(
        df["_market_start_dt_utc"],
        utc=True,
        errors="coerce",
    )

    df = df.dropna(
        subset=[
            "price",
            "size",
            "datetime_utc",
            "_market_start_dt_utc",
        ]
    ).copy()

    df = df[(df["price"] >= 0) & (df["price"] <= 1)].copy()
    df = df[df["size"] > 0].copy()

    if "side" in df.columns:
        df["side"] = safe_lower_str(df["side"])

    if args.side_filter != "all":
        if "side" not in df.columns:
            raise ValueError("--side-filter requires side column.")
        before_side_filter = len(df)
        df = df[df["side"].eq(args.side_filter)].copy()
        print(f"Rows before side filter: {before_side_filter:,}")
        print(f"Rows after side filter : {len(df):,}")
        print(f"Keeping side = {args.side_filter}.")

    # -----------------------------
    # 2. Normalize outcome direction: up/down
    # -----------------------------
    if "_outcome_norm" in df.columns:
        df["outcome_norm"] = safe_lower_str(df["_outcome_norm"])
    elif "outcome" in df.columns:
        df["outcome_norm"] = safe_lower_str(df["outcome"])
    else:
        raise ValueError("Need either _outcome_norm or outcome column.")

    df["_final_outcome"] = safe_lower_str(df["_final_outcome"])

    # Fallback: if outcome_norm is missing, infer it from asset token id.
    if "_up_token_id" in df.columns and "_down_token_id" in df.columns and "asset" in df.columns:
        asset_str = df["asset"].astype("string")
        up_str = df["_up_token_id"].astype("string")
        down_str = df["_down_token_id"].astype("string")

        inferred = pd.Series(
            np.where(
                asset_str == up_str,
                "up",
                np.where(asset_str == down_str, "down", pd.NA),
            ),
            index=df.index,
            dtype="string",
        )

        df["outcome_norm"] = df["outcome_norm"].fillna(inferred)

    df = df[df["outcome_norm"].isin(["up", "down"])].copy()

    # -----------------------------
    # 3. Minute from market start
    # -----------------------------
    df["offset_sec"] = (
        df["datetime_utc"] - df["_market_start_dt_utc"]
    ).dt.total_seconds()

    df["minute_from_start"] = np.floor(df["offset_sec"] / 60).astype("int64")

    # Natural UTC clock minute, useful for debugging.
    df["clock_minute_utc"] = df["datetime_utc"].dt.floor("min")

    before_filter = len(df)
    df = df[
        (df["minute_from_start"] >= args.min_minute)
        & (df["minute_from_start"] <= args.max_minute)
    ].copy()
    after_filter = len(df)

    print(f"Rows before minute filter: {before_filter:,}")
    print(f"Rows after minute filter : {after_filter:,}")
    print(f"Keeping minute_from_start from {args.min_minute} to {args.max_minute}.")

    # -----------------------------
    # 4. Derived probability columns
    # -----------------------------
    # Raw traded token price.
    df["token_price"] = df["price"]

    # Unified implied BTC-Up probability.
    #
    # If the traded token is Up, the token price itself is the Up probability.
    # If the traded token is Down, the token price is the Down probability,
    # so the Up probability is 1 - Down probability.
    df["up_prob"] = np.where(
        df["outcome_norm"] == "up",
        df["price"],
        1.0 - df["price"],
    )
    df["up_prob"] = np.clip(df["up_prob"], 0.0, 1.0)

    # Winning / losing token price distribution.
    df["is_winning_token"] = df["outcome_norm"] == df["_final_outcome"]
    df["winloss"] = np.where(df["is_winning_token"], "winner_token", "loser_token")

    # -----------------------------
    # 5. Basic summaries
    # -----------------------------
    market_id_col = "_condition_id" if "_condition_id" in df.columns else "_market_start_dt_utc"

    summary_by_minute = (
        df.groupby("minute_from_start")
        .agg(
            n_trades=("price", "size"),
            size_sum=("size", "sum"),
            token_price_mean=("token_price", "mean"),
            token_price_median=("token_price", "median"),
            up_prob_mean=("up_prob", "mean"),
            up_prob_median=("up_prob", "median"),
            unique_markets=(market_id_col, "nunique"),
        )
        .reset_index()
        .sort_values("minute_from_start")
    )

    summary_path = outdir / "summary_by_minute.csv"
    summary_by_minute.to_csv(summary_path, index=False)

    summary_by_minute_outcome = (
        df.groupby(["minute_from_start", "outcome_norm"])
        .agg(
            n_trades=("price", "size"),
            size_sum=("size", "sum"),
            token_price_mean=("token_price", "mean"),
            token_price_median=("token_price", "median"),
            up_prob_mean=("up_prob", "mean"),
            up_prob_median=("up_prob", "median"),
        )
        .reset_index()
        .sort_values(["minute_from_start", "outcome_norm"])
    )

    summary_outcome_path = outdir / "summary_by_minute_outcome.csv"
    summary_by_minute_outcome.to_csv(summary_outcome_path, index=False)

    summary_by_minute_winloss = (
        df.groupby(["minute_from_start", "winloss"])
        .agg(
            n_trades=("price", "size"),
            size_sum=("size", "sum"),
            token_price_mean=("token_price", "mean"),
            token_price_median=("token_price", "median"),
        )
        .reset_index()
        .sort_values(["minute_from_start", "winloss"])
    )

    summary_winloss_path = outdir / "summary_by_minute_winloss.csv"
    summary_by_minute_winloss.to_csv(summary_winloss_path, index=False)

    enriched_path = outdir / "trades_with_minute_and_probs.csv"
    df.to_csv(enriched_path, index=False)

    # -----------------------------
    # 6. Distributions
    # -----------------------------
    dist_up_prob_long, _, _ = make_distribution(
        df=df,
        value_col="up_prob",
        group_cols=["minute_from_start"],
        bin_width=args.bin_width,
        out_prefix=outdir / "dist_up_prob_by_minute",
    )

    dist_token_outcome_long, _, _ = make_distribution(
        df=df,
        value_col="token_price",
        group_cols=["minute_from_start", "outcome_norm"],
        bin_width=args.bin_width,
        out_prefix=outdir / "dist_token_price_by_minute_outcome",
    )

    dist_token_winloss_long, _, _ = make_distribution(
        df=df,
        value_col="token_price",
        group_cols=["minute_from_start", "winloss"],
        bin_width=args.bin_width,
        out_prefix=outdir / "dist_token_price_by_minute_winloss",
    )

    # Optional natural-clock-minute distribution.
    make_distribution(
        df=df,
        value_col="up_prob",
        group_cols=["clock_minute_utc"],
        bin_width=args.bin_width,
        out_prefix=outdir / "dist_up_prob_by_clock_minute_utc",
    )

    # -----------------------------
    # 7. Plots
    # -----------------------------
    plot_heatmap_from_long(
        dist_long=dist_up_prob_long,
        index_col="minute_from_start",
        price_col="price_bin",
        value_col="prob_by_size",
        title="BTC Up Implied Probability Distribution by Market Minute, size-weighted",
        out_path=outdir / "heatmap_up_prob_by_market_minute_size_weighted.png",
    )

    plot_heatmap_from_long(
        dist_long=dist_up_prob_long,
        index_col="minute_from_start",
        price_col="price_bin",
        value_col="prob_by_count",
        title="BTC Up Implied Probability Distribution by Market Minute, count-weighted",
        out_path=outdir / "heatmap_up_prob_by_market_minute_count_weighted.png",
    )

    print("\nDone.")
    print(f"Output directory: {outdir.resolve()}")
    print("\nMain files:")
    for p in [
        summary_path,
        summary_outcome_path,
        summary_winloss_path,
        enriched_path,
        outdir / "dist_up_prob_by_minute.long.csv",
        outdir / "dist_up_prob_by_minute.pivot_count.csv",
        outdir / "dist_up_prob_by_minute.pivot_size.csv",
        outdir / "dist_token_price_by_minute_outcome.long.csv",
        outdir / "dist_token_price_by_minute_winloss.long.csv",
        outdir / "heatmap_up_prob_by_market_minute_size_weighted.png",
        outdir / "heatmap_up_prob_by_market_minute_count_weighted.png",
    ]:
        print(f"  {p}")

    # Keep variables referenced so linters do not think they are accidental.
    _ = dist_token_outcome_long, dist_token_winloss_long


if __name__ == "__main__":
    main()

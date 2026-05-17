#!/usr/bin/env python3
"""
Pull Polymarket BTC 5-minute Up/Down market trades for the last N days
and plot the trade price probability distribution.

Default interpretation:
- One row per taker-side fill from Data API, so each matched trade/fill is counted once.
- `price` is the traded price of that trade's own outcome token, e.g. Up at 0.62 or Down at 0.38.
- If you want a unified implied "Up probability", pass --canonical-up-prob:
    Up price => price
    Down price => 1 - price

Install:
    pip install requests pandas numpy matplotlib tqdm

Run:
    python polymarket_btc5m_trade_distribution.py --days 30 --bin-size 0.02 --outdir out_btc5m
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from matplotlib.ticker import PercentFormatter
from tqdm import tqdm


GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"


@dataclass
class MarketRef:
    slug: str
    condition_id: str
    event_id: Optional[str]
    market_id: Optional[str]
    title: Optional[str]
    start_ts: int


def floor_to_5m(ts: int) -> int:
    return ts - (ts % 300)


def generate_btc_5m_slugs(days: int, end_dt: Optional[datetime] = None) -> List[tuple[str, int]]:
    """Generate deterministic BTC 5m market slugs: btc-updown-5m-{UTC_start_timestamp}."""
    if end_dt is None:
        end_dt = datetime.now(timezone.utc)
    if end_dt.tzinfo is None:
        raise ValueError("end_dt must be timezone-aware")

    end_ts = floor_to_5m(int(end_dt.timestamp()))
    start_ts = floor_to_5m(int((end_dt - timedelta(days=days)).timestamp()))

    return [(f"btc-updown-5m-{ts}", ts) for ts in range(start_ts, end_ts + 1, 300)]


def chunks(xs: Sequence[Any], n: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def get_json(
    session: requests.Session,
    url: str,
    params: Optional[Union[Dict[str, Any], List[tuple[str, Any]]]] = None,
    max_retries: int = 6,
    base_sleep: float = 0.7,
) -> Any:
    """GET JSON with conservative retry/backoff for 429/5xx/transient network errors."""
    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = base_sleep * (2 ** attempt)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            wait = base_sleep * (2 ** attempt)
            time.sleep(wait)

    raise RuntimeError(f"GET failed after {max_retries} retries: {url} params={params}") from last_error


def extract_condition_id_from_event(event: Dict[str, Any]) -> Optional[str]:
    markets = event.get("markets") or []
    if not markets:
        return None
    # BTC Up/Down 5m is normally a single-market event.
    m = markets[0]
    return m.get("conditionId") or m.get("condition_id")


def discover_markets_by_slug(
    slugs_with_ts: Sequence[tuple[str, int]],
    slug_batch_size: int = 50,
    sleep_s: float = 0.05,
) -> tuple[List[MarketRef], List[str]]:
    """
    Use Gamma events?slug=slug1&slug=slug2... to resolve slugs to condition IDs.
    Falling back to markets/slug/{slug} is possible but intentionally omitted here
    to keep API calls low; missing slugs are reported.
    """
    session = requests.Session()
    refs: List[MarketRef] = []
    missing: List[str] = []
    ts_by_slug = dict(slugs_with_ts)
    found_slugs: set[str] = set()

    batches = list(chunks([s for s, _ in slugs_with_ts], slug_batch_size))
    for batch in tqdm(batches, desc="Discovering Gamma markets"):
        # Gamma API expects multiple slug parameters instead of comma-separated
        params: List[tuple[str, Any]] = [("slug", s) for s in batch]
        params.append(("limit", len(batch)))

        data = get_json(session, f"{GAMMA_API}/events", params=params)

        # Gamma returns a list for /events.
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected Gamma events response: {type(data)} {str(data)[:500]}")

        for ev in data:
            slug = ev.get("slug")
            if not slug:
                continue

            cid = extract_condition_id_from_event(ev)
            if not cid:
                continue

            markets = ev.get("markets") or [{}]
            m0 = markets[0] if markets else {}
            refs.append(
                MarketRef(
                    slug=slug,
                    condition_id=cid,
                    event_id=str(ev.get("id")) if ev.get("id") is not None else None,
                    market_id=str(m0.get("id")) if m0.get("id") is not None else None,
                    title=m0.get("question") or ev.get("title"),
                    start_ts=ts_by_slug.get(slug, -1),
                )
            )
            found_slugs.add(slug)

        for slug in batch:
            if slug not in found_slugs:
                missing.append(slug)

        if sleep_s > 0:
            time.sleep(sleep_s)

    # Deduplicate by condition id while preserving order.
    seen_cids: set[str] = set()
    deduped: List[MarketRef] = []
    for r in refs:
        if r.condition_id not in seen_cids:
            deduped.append(r)
            seen_cids.add(r.condition_id)

    return deduped, missing


def fetch_trades_for_market(
    session: requests.Session,
    condition_id: str,
    limit: int = 10000,
    taker_only: bool = True,
) -> tuple[List[Dict[str, Any]], bool]:
    """
    Pull trades for one market condition id.
    Returns (trades, maybe_truncated). maybe_truncated=True means the API returned
    a full page at the maximum offset boundary, so there may be more records than
    Data API offset pagination exposes.
    """
    all_rows: List[Dict[str, Any]] = []
    maybe_truncated = False

    # Data API docs: limit max 10000, offset max 10000.
    for offset in (0, 10000):
        params = {
            "market": condition_id,
            "limit": limit,
            "offset": offset,
            "takerOnly": str(taker_only).lower(),
        }
        rows = get_json(session, f"{DATA_API}/trades", params=params)
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected Data API /trades response: {type(rows)} {str(rows)[:500]}")

        all_rows.extend(rows)

        if len(rows) < limit:
            break
        if offset == 10000 and len(rows) >= limit:
            maybe_truncated = True

    return all_rows, maybe_truncated


def fetch_all_trades(
    markets: Sequence[MarketRef],
    sleep_s: float = 0.03,
    taker_only: bool = True,
) -> tuple[pd.DataFrame, List[str]]:
    session = requests.Session()
    rows: List[Dict[str, Any]] = []
    truncated_cids: List[str] = []

    ref_by_cid = {m.condition_id: m for m in markets}

    for m in tqdm(markets, desc="Fetching Data API trades"):
        trades, maybe_truncated = fetch_trades_for_market(
            session=session,
            condition_id=m.condition_id,
            taker_only=taker_only,
        )
        if maybe_truncated:
            truncated_cids.append(m.condition_id)

        for t in trades:
            t["_market_slug"] = m.slug
            t["_market_start_ts"] = m.start_ts
            t["_event_id"] = m.event_id
            t["_market_id"] = m.market_id
            t["_market_title"] = m.title
            rows.append(t)

        if sleep_s > 0:
            time.sleep(sleep_s)

    if not rows:
        return pd.DataFrame(), truncated_cids

    df = pd.DataFrame(rows)

    # Basic typing and dedupe. transactionHash can repeat across maker/taker rows or multi-fill txs;
    # keep all rows under takerOnly=True, but remove exact duplicates.
    if "price" in df.columns:
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
    if "size" in df.columns:
        df["size"] = pd.to_numeric(df["size"], errors="coerce")
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").astype("Int64")
        df["datetime_utc"] = pd.to_datetime(df["timestamp"], unit="s", utc=True, errors="coerce")

    # Exact duplicate protection.
    dedupe_cols = [c for c in ["transactionHash", "conditionId", "asset", "side", "outcome", "price", "size", "timestamp"] if c in df.columns]
    if dedupe_cols:
        df = df.drop_duplicates(subset=dedupe_cols).reset_index(drop=True)

    # Preserve useful market metadata.
    if "conditionId" in df.columns:
        df["_market_slug"] = df["conditionId"].map(lambda cid: ref_by_cid.get(cid).slug if cid in ref_by_cid else None).fillna(df.get("_market_slug"))

    return df, truncated_cids


def build_distribution(
    df: pd.DataFrame,
    bin_size: float,
    canonical_up_prob: bool = False,
) -> pd.DataFrame:
    if df.empty:
        raise ValueError("No trades to plot")

    prices = df["price"].astype(float).copy()

    if canonical_up_prob:
        if "outcome" not in df.columns:
            raise ValueError("--canonical-up-prob requires an outcome column")
        outcome = df["outcome"].astype(str).str.lower()
        prices = np.where(outcome.eq("down"), 1.0 - prices, prices)

    prices = pd.Series(prices).dropna()
    prices = prices[(prices >= 0) & (prices <= 1)]

    if prices.empty:
        raise ValueError("No valid prices in [0, 1]")

    # Include 1.00 in the last bin.
    edges = np.round(np.arange(0, 1 + bin_size + 1e-9, bin_size), 10)
    counts, edges = np.histogram(prices.to_numpy(), bins=edges)
    prob = counts / counts.sum()

    hist = pd.DataFrame(
        {
            "bin_left": edges[:-1],
            "bin_right": edges[1:],
            "count": counts,
            "probability": prob,
        }
    )
    hist["bin_label"] = hist.apply(lambda r: f"[{r.bin_left:.2f}, {r.bin_right:.2f})", axis=1)
    if len(hist) > 0:
        hist.loc[hist.index[-1], "bin_label"] = f"[{hist.iloc[-1].bin_left:.2f}, {hist.iloc[-1].bin_right:.2f}]"
    return hist


def plot_distribution(hist: pd.DataFrame, output_png: Path, bin_size: float, title_suffix: str = "") -> None:
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.bar(hist["bin_left"], hist["probability"], width=bin_size, align="edge", edgecolor="black", linewidth=0.4)
    ax.set_xlim(0, 1)
    ax.set_xlabel("Trade price / implied probability")
    ax.set_ylabel("Probability = trades in bin / total trades")
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=1.0))
    ax.set_title(f"Polymarket BTC 5m Up/Down trade price distribution{title_suffix}")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(output_png, dpi=180)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="Lookback window in calendar days")
    parser.add_argument("--bin-size", type=float, default=0.02, help="Histogram bin width")
    parser.add_argument("--outdir", type=Path, default=Path("out_btc5m"), help="Output directory")
    parser.add_argument("--slug-batch-size", type=int, default=150, help="Number of slugs per Gamma request")
    parser.add_argument("--sleep", type=float, default=0.03, help="Sleep seconds between Data API requests")
    parser.add_argument("--gamma-sleep", type=float, default=0.05, help="Sleep seconds between Gamma API requests")
    parser.add_argument("--canonical-up-prob", action="store_true", help="Convert Down trades to 1-price so x-axis is implied Up probability")
    parser.add_argument("--include-maker-legs", action="store_true", help="Set takerOnly=false; may double-count trade participant legs")
    args = parser.parse_args()

    if not (0 < args.bin_size <= 1):
        raise ValueError("--bin-size must be in (0, 1]")

    args.outdir.mkdir(parents=True, exist_ok=True)

    slugs = generate_btc_5m_slugs(days=args.days)
    print(f"Generated {len(slugs):,} BTC 5m slugs for last {args.days} days")

    markets, missing_slugs = discover_markets_by_slug(
        slugs,
        slug_batch_size=args.slug_batch_size,
        sleep_s=args.gamma_sleep,
    )
    print(f"Resolved {len(markets):,} markets; missing/unindexed slugs: {len(missing_slugs):,}")

    market_refs_path = args.outdir / "market_refs.csv"
    pd.DataFrame([m.__dict__ for m in markets]).to_csv(market_refs_path, index=False)

    if missing_slugs:
        (args.outdir / "missing_slugs.txt").write_text("\n".join(missing_slugs), encoding="utf-8")

    if not markets:
        raise SystemExit("No markets resolved; cannot fetch trades.")

    trades_df, truncated_cids = fetch_all_trades(
        markets,
        sleep_s=args.sleep,
        taker_only=not args.include_maker_legs,
    )
    print(f"Fetched {len(trades_df):,} trade rows")

    if truncated_cids:
        (args.outdir / "possibly_truncated_condition_ids.txt").write_text("\n".join(truncated_cids), encoding="utf-8")
        print(f"WARNING: {len(truncated_cids):,} markets may be truncated by Data API offset limits.")

    trades_csv = args.outdir / "btc5m_trades.csv"
    trades_df.to_csv(trades_csv, index=False)

    outcomes = trades_df["outcome"].unique() if "outcome" in trades_df.columns else ["default"]
    
    for outcome in outcomes:
        outcome_slug = str(outcome).lower().replace(" ", "_")
        sub_df = trades_df[trades_df["outcome"] == outcome] if outcome != "default" else trades_df
        
        if sub_df.empty:
            continue
            
        hist = build_distribution(sub_df, bin_size=args.bin_size, canonical_up_prob=False)
        hist_csv = args.outdir / f"btc5m_distribution_{outcome_slug}.csv"
        hist.to_csv(hist_csv, index=False)
        
        suffix = f" — {outcome}, last {args.days} days, bin={args.bin_size:.2f}, n={len(sub_df):,}"
        png = args.outdir / f"btc5m_distribution_{outcome_slug}.png"
        plot_distribution(hist, png, bin_size=args.bin_size, title_suffix=suffix)
        print(f"Generated distribution for {outcome}: {png}")

    if args.canonical_up_prob and "outcome" in trades_df.columns:
        hist_canonical = build_distribution(trades_df, bin_size=args.bin_size, canonical_up_prob=True)
        hist_csv = args.outdir / "btc5m_distribution_canonical_up.csv"
        hist_canonical.to_csv(hist_csv, index=False)
        
        suffix = f" — Canonical Up Prob, last {args.days} days, bin={args.bin_size:.2f}, n={len(trades_df):,}"
        png = args.outdir / "btc5m_distribution_canonical_up.png"
        plot_distribution(hist_canonical, png, bin_size=args.bin_size, title_suffix=suffix)
        print(f"Generated canonical Up distribution: {png}")

    meta = {
        "days": args.days,
        "bin_size": args.bin_size,
        "n_generated_slugs": len(slugs),
        "n_resolved_markets": len(markets),
        "n_missing_slugs": len(missing_slugs),
        "n_trade_rows": int(len(trades_df)),
        "n_hist_count": int(hist["count"].sum()),
        "canonical_up_prob": bool(args.canonical_up_prob),
        "include_maker_legs": bool(args.include_maker_legs),
        "possibly_truncated_markets": len(truncated_cids),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "outputs": {
            "market_refs": str(market_refs_path),
            "trades_csv": str(trades_csv),
            "hist_csv": str(hist_csv),
            "png": str(png),
        },
    }
    (args.outdir / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print("\nDone.")
    print(f"Market refs: {market_refs_path}")
    print(f"Trades CSV : {trades_csv}")
    print(f"Hist CSV   : {hist_csv}")
    print(f"Plot PNG   : {png}")


if __name__ == "__main__":
    main()

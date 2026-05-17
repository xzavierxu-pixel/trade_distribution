#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch Polymarket BTC 5m Up/Down market refs and taker-only trades.

This script is intentionally data-fetch only. Analysis lives in
analyze_btc5m_clean.py.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from tqdm import tqdm


GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"


@dataclass
class MarketRef:
    slug: str
    condition_id: str
    event_id: str | None
    market_id: str | None
    title: str | None
    start_ts: int
    start_dt_utc: str
    final_outcome: str | None
    up_token_id: str | None
    down_token_id: str | None


def floor_to_5m(ts: int) -> int:
    return ts - (ts % 300)


def parse_maybe_json(x: Any) -> Any:
    if isinstance(x, (list, dict)) or x is None:
        return x
    if isinstance(x, str):
        s = x.strip()
        if s.startswith("[") or s.startswith("{"):
            try:
                return json.loads(s)
            except Exception:
                return x
    return x


def norm_outcome(x: Any) -> str | None:
    s = str(x).strip().lower() if x is not None else ""
    if s in {"up", "yes", "higher", "above"}:
        return "up"
    if s in {"down", "no", "lower", "below"}:
        return "down"
    return None


def get_json(session: requests.Session, url: str, params: dict[str, Any] | None = None) -> Any:
    last_error: Exception | None = None
    for attempt in range(7):
        try:
            resp = session.get(url, params=params, timeout=35)
            if resp.status_code in {408, 425, 429, 500, 502, 503, 504}:
                time.sleep(0.75 * (2**attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.75 * (2**attempt))
    raise RuntimeError(f"GET failed: {url} params={params}") from last_error


def lookback_slugs(days: int, timezone_name: str, end_date: date | None) -> list[tuple[str, int]]:
    tz = ZoneInfo(timezone_name)
    if end_date is None:
        end_local = datetime.combine(datetime.now(tz).date(), dtime.min, tzinfo=tz)
    else:
        end_local = datetime.combine(end_date + timedelta(days=1), dtime.min, tzinfo=tz)
    start_local = end_local - timedelta(days=days)
    start_ts = floor_to_5m(int(start_local.astimezone(timezone.utc).timestamp()))
    end_ts = floor_to_5m(int(end_local.astimezone(timezone.utc).timestamp()))
    return [(f"btc-updown-5m-{ts}", ts) for ts in range(start_ts, end_ts, 300)]


def extract_token_ids(market: dict[str, Any]) -> tuple[str | None, str | None]:
    outcomes = parse_maybe_json(market.get("outcomes"))
    token_ids = parse_maybe_json(
        market.get("clobTokenIds")
        or market.get("clob_token_ids")
        or market.get("outcomeTokenIds")
        or market.get("tokens")
    )
    if isinstance(token_ids, list) and token_ids and isinstance(token_ids[0], dict):
        if not outcomes:
            outcomes = [t.get("outcome") or t.get("name") for t in token_ids]
        token_ids = [t.get("token_id") or t.get("tokenId") or t.get("id") or t.get("asset_id") for t in token_ids]
    up = down = None
    if isinstance(outcomes, list) and isinstance(token_ids, list):
        for outcome, token_id in zip(outcomes, token_ids):
            n = norm_outcome(outcome)
            if n == "up":
                up = str(token_id)
            elif n == "down":
                down = str(token_id)
    return up, down


def infer_final_outcome(market: dict[str, Any], event: dict[str, Any]) -> str | None:
    for obj in [market, event]:
        for key in ["winnerOutcome", "winningOutcome", "resolutionOutcome", "resolvedOutcome", "result", "winner"]:
            n = norm_outcome(obj.get(key))
            if n:
                return n
    outcomes = parse_maybe_json(market.get("outcomes"))
    prices = parse_maybe_json(market.get("outcomePrices") or market.get("outcome_prices"))
    if isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) == len(prices):
        vals = [float(x) for x in prices]
        imax = int(np.argmax(vals))
        if vals[imax] >= 0.90:
            return norm_outcome(outcomes[imax])
    return None


def parse_event(slug: str, start_ts: int, event: dict[str, Any]) -> MarketRef | None:
    markets = parse_maybe_json(event.get("markets") or [])
    if not isinstance(markets, list) or not markets:
        return None
    market = markets[0]
    condition_id = market.get("conditionId") or market.get("condition_id")
    if not condition_id:
        return None
    up_token, down_token = extract_token_ids(market)
    return MarketRef(
        slug=slug,
        condition_id=str(condition_id),
        event_id=str(event.get("id")) if event.get("id") is not None else None,
        market_id=str(market.get("id")) if market.get("id") is not None else None,
        title=market.get("question") or event.get("title") or event.get("question"),
        start_ts=start_ts,
        start_dt_utc=datetime.fromtimestamp(start_ts, timezone.utc).isoformat(),
        final_outcome=infer_final_outcome(market, event),
        up_token_id=up_token,
        down_token_id=down_token,
    )


def discover(slugs: list[tuple[str, int]], batch_size: int, sleep_s: float) -> tuple[list[MarketRef], list[str]]:
    session = requests.Session()
    ts_by_slug = dict(slugs)
    refs: dict[str, MarketRef] = {}
    for i in tqdm(range(0, len(slugs), batch_size), desc="Discover Gamma events"):
        batch = [s for s, _ in slugs[i : i + batch_size]]
        try:
            params: list[tuple[str, Any]] = [("slug", slug) for slug in batch]
            params.append(("limit", len(batch)))
            data = get_json(session, f"{GAMMA_API}/events", params)
        except Exception:
            data = []
        if isinstance(data, list):
            for event in data:
                slug = event.get("slug")
                if slug in ts_by_slug:
                    ref = parse_event(slug, ts_by_slug[slug], event)
                    if ref:
                        refs[slug] = ref
        time.sleep(sleep_s)
    missing = [slug for slug, _ in slugs if slug not in refs]
    return [refs[slug] for slug, _ in slugs if slug in refs], missing


def fetch_trades(refs: list[MarketRef], taker_only: bool, sleep_s: float) -> pd.DataFrame:
    session = requests.Session()
    rows: list[dict[str, Any]] = []
    for ref in tqdm(refs, desc="Fetch Data API trades"):
        for offset in (0, 10_000):
            params = {
                "market": ref.condition_id,
                "limit": 10_000,
                "offset": offset,
                "takerOnly": str(taker_only).lower(),
            }
            data = get_json(session, f"{DATA_API}/trades", params)
            if not isinstance(data, list):
                break
            for trade in data:
                row = dict(trade)
                row["_slug"] = ref.slug
                row["_condition_id"] = ref.condition_id
                row["_market_start_ts"] = ref.start_ts
                row["_market_start_dt_utc"] = ref.start_dt_utc
                row["_final_outcome"] = ref.final_outcome
                row["_up_token_id"] = ref.up_token_id
                row["_down_token_id"] = ref.down_token_id
                asset = str(row.get("asset") or row.get("token") or row.get("tokenId") or "")
                outcome = norm_outcome(row.get("outcome"))
                if outcome is None and asset == str(ref.up_token_id):
                    outcome = "up"
                if outcome is None and asset == str(ref.down_token_id):
                    outcome = "down"
                row["_outcome_norm"] = outcome
                rows.append(row)
            if len(data) < 10_000:
                break
        time.sleep(sleep_s)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
        df["datetime_utc"] = pd.to_datetime(df["timestamp"], unit="s", utc=True, errors="coerce")
        dedupe_cols = [c for c in ["transactionHash", "_condition_id", "asset", "_outcome_norm", "price", "size", "timestamp"] if c in df.columns]
        df = df.drop_duplicates(subset=dedupe_cols).reset_index(drop=True)
    return df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--timezone", type=str, default="Asia/Singapore")
    parser.add_argument("--outdir", type=Path, default=Path("data/btc5m_7d"))
    parser.add_argument("--include-maker-legs", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--sleep", type=float, default=0.03)
    args = parser.parse_args()

    end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else None
    args.outdir.mkdir(parents=True, exist_ok=True)
    slugs = lookback_slugs(args.days, args.timezone, end_date)
    refs, missing = discover(slugs, args.batch_size, args.sleep)
    refs_df = pd.DataFrame([asdict(r) for r in refs])
    refs_df.to_csv(args.outdir / "market_refs.csv", index=False)
    if refs_df.empty:
        if missing:
            (args.outdir / "missing_slugs.txt").write_text("\n".join(missing), encoding="utf-8")
        raise RuntimeError(f"No markets discovered for {len(slugs)} slugs")
    refs_resolved = refs_df[refs_df["final_outcome"].isin(["up", "down"])].copy()
    refs_resolved.to_csv(args.outdir / "market_refs_resolved.csv", index=False)
    if missing:
        (args.outdir / "missing_slugs.txt").write_text("\n".join(missing), encoding="utf-8")
    trades = fetch_trades(
        [MarketRef(**r) for r in refs_resolved.to_dict("records")],
        taker_only=not args.include_maker_legs,
        sleep_s=args.sleep,
    )
    trades.to_csv(args.outdir / "trades_raw.csv", index=False)
    print(f"Wrote {len(trades):,} trades to {args.outdir / 'trades_raw.csv'}")


if __name__ == "__main__":
    main()

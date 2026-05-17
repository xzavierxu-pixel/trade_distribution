#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch taker SELL trades for already-discovered BTC 5m resolved markets."""

from __future__ import annotations

import argparse
import csv
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from tqdm import tqdm

from fetch_btc5m_data import DATA_API, get_json, norm_outcome


def enrich_trade(trade: dict[str, Any], ref: pd.Series) -> dict[str, Any]:
    row = dict(trade)
    row["_slug"] = ref["slug"]
    row["_condition_id"] = ref["condition_id"]
    row["_market_start_ts"] = int(ref["start_ts"])
    row["_market_start_dt_utc"] = ref["start_dt_utc"]
    row["_final_outcome"] = ref["final_outcome"]
    row["_up_token_id"] = str(ref["up_token_id"])
    row["_down_token_id"] = str(ref["down_token_id"])
    asset = str(row.get("asset") or row.get("token") or row.get("tokenId") or "")
    outcome = norm_outcome(row.get("outcome"))
    if outcome is None and asset == str(ref["up_token_id"]):
        outcome = "up"
    if outcome is None and asset == str(ref["down_token_id"]):
        outcome = "down"
    row["_outcome_norm"] = outcome
    return row


def fetch_one(ref_dict: dict[str, Any], limit: int, sleep_s: float) -> tuple[str, list[dict[str, Any]]]:
    ref = pd.Series(ref_dict)
    session = requests.Session()
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "market": ref["condition_id"],
            "limit": limit,
            "offset": offset,
            "takerOnly": "true",
            "side": "SELL",
        }
        data = get_json(session, f"{DATA_API}/trades", params)
        if not isinstance(data, list):
            break
        for trade in data:
            if str(trade.get("side", "")).upper() == "SELL":
                rows.append(enrich_trade(trade, ref))
        if len(data) < limit:
            break
        offset += limit
        if sleep_s:
            time.sleep(sleep_s)
    return str(ref["slug"]), rows


def read_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def append_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None) -> list[str] | None:
    if not rows:
        return fieldnames
    if fieldnames is None:
        fieldnames = list(rows[0].keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)
    return fieldnames


def dedupe_csv(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    df = pd.read_csv(path)
    if df.empty:
        return
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df["datetime_utc"] = pd.to_datetime(df["timestamp"], unit="s", utc=True, errors="coerce")
    dedupe_cols = [
        c
        for c in ["transactionHash", "_condition_id", "asset", "_outcome_norm", "price", "size", "timestamp"]
        if c in df.columns
    ]
    df = df.drop_duplicates(subset=dedupe_cols).reset_index(drop=True)
    df.to_csv(path, index=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refs-csv", type=Path, required=True)
    parser.add_argument("--out-csv", type=Path, required=True)
    parser.add_argument("--progress-file", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--sleep", type=float, default=0.0)
    args = parser.parse_args()

    progress_file = args.progress_file or args.out_csv.with_suffix(".done_slugs.txt")
    refs = pd.read_csv(args.refs_csv)
    refs = refs[refs["final_outcome"].isin(["up", "down"])].copy()
    done = read_done(progress_file)
    refs = refs[~refs["slug"].isin(done)].copy()

    fieldnames: list[str] | None = None
    if args.out_csv.exists() and args.out_csv.stat().st_size > 0:
        fieldnames = list(pd.read_csv(args.out_csv, nrows=0).columns)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(fetch_one, row, args.limit, args.sleep) for row in refs.to_dict("records")]
        for future in tqdm(as_completed(futures), total=len(futures), desc="Fetch SELL trades"):
            slug, rows = future.result()
            fieldnames = append_rows(args.out_csv, rows, fieldnames)
            with progress_file.open("a", encoding="utf-8") as f:
                f.write(f"{slug}\n")

    dedupe_csv(args.out_csv)
    final_rows = len(pd.read_csv(args.out_csv)) if args.out_csv.exists() and args.out_csv.stat().st_size > 0 else 0
    print(f"Wrote {final_rows:,} SELL trades to {args.out_csv}")


if __name__ == "__main__":
    main()

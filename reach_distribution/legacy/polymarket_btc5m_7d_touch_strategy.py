#!/usr/bin/env python3
"""
Polymarket BTC 5m: last-N-days winner-only touch curves + EV ladder strategy.

Main changes in this version
----------------------------
1) Default lookback is 7 calendar days.
2) Plots can cover the full price range, default 0.01 to 0.99.
3) Strategy can use a narrower price range, default 0.01 to 0.50.
4) Total strategy budget defaults to 10 USDC.
5) Each single price level can use at most 20% of total budget by default.

Strategy model
--------------
Prediction Up:
    buy Up token, use final-Up markets' Up-token winner-only touch curve.

Prediction Down:
    buy Down token, use final-Down markets' Down-token winner-only touch curve.

Wrong prediction:
    b = 1, assume every submitted order is filled and expires worthless.

For one minimum-order-size chunk:

    EV_s(p,k) = q_s * a_s(p,k) * mos * (1-p) - (1-q_s) * mos * p

where:
    s             = up or down
    p             = limit buy price
    k             = chunk index
    mos           = minimum order size in shares
    a_s(p,k)      = winner-only touch/capacity probability
    q_s           = model accuracy conditional on predicting side s

Touch/capacity probability
--------------------------
For side='up':

    a_up(p,k) = P(final-Up market's Up token cumulative traded volume at price <= p >= k * mos)

For side='down':

    a_down(p,k) = P(final-Down market's Down token cumulative traded volume at price <= p >= k * mos)

Install
-------
    pip install requests pandas numpy matplotlib tqdm

Run
---
    python polymarket_btc5m_7d_touch_strategy.py --outdir out_btc5m_7d

Useful variants
---------------
Use 30 days:
    python polymarket_btc5m_7d_touch_strategy.py --days 30 --outdir out_btc5m_30d

Use full strategy price range up to 0.99:
    python polymarket_btc5m_7d_touch_strategy.py --strategy-max-price 0.99 --outdir out_btc5m_7d_full_strategy

More conservative:
    python polymarket_btc5m_7d_touch_strategy.py --a-haircut 0.7 --min-touched 5 --outdir out_btc5m_7d_conservative
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from matplotlib import pyplot as plt
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
    start_dt_utc: str
    final_outcome: Optional[str]          # "up" or "down"
    up_token_id: Optional[str]
    down_token_id: Optional[str]
    raw_outcomes: Optional[str]
    raw_outcome_prices: Optional[str]


def floor_to_5m(ts: int) -> int:
    return ts - (ts % 300)


def parse_maybe_json(x: Any) -> Any:
    if x is None:
        return None
    if isinstance(x, (list, dict)):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return None
        if s.startswith("[") or s.startswith("{"):
            try:
                return json.loads(s)
            except Exception:
                return x
    return x


def norm_outcome(x: Any) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip().lower()
    if s in {"up", "yes", "higher", "above"}:
        return "up"
    if s in {"down", "no", "lower", "below"}:
        return "down"
    return None


def chunks(xs: Sequence[Any], n: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def get_json(
    session: requests.Session,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    max_retries: int = 7,
    base_sleep: float = 0.75,
) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, timeout=35)
            if resp.status_code in (408, 425, 429, 500, 502, 503, 504):
                time.sleep(base_sleep * (2 ** attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(base_sleep * (2 ** attempt))
    raise RuntimeError(f"GET failed: {url} params={params}") from last_error


def lookback_window_to_slugs(days: int, timezone_name: str, end_date: Optional[date] = None) -> List[Tuple[str, int]]:
    """
    Generate BTC 5m slugs for the last `days` complete calendar days in timezone_name.

    If end_date is None:
        end boundary = today 00:00 in timezone_name
        start boundary = end - days

    If end_date is provided:
        end boundary = end_date + 1 day 00:00 in timezone_name
        start boundary = end - days

    Example:
        --days 7 on May 14 means May 7 00:00 through May 14 00:00 local time.
    """
    tz = ZoneInfo(timezone_name)

    if end_date is None:
        end_local = datetime.combine(datetime.now(tz).date(), dtime.min, tzinfo=tz)
    else:
        end_local = datetime.combine(end_date + timedelta(days=1), dtime.min, tzinfo=tz)

    start_local = end_local - timedelta(days=days)

    start_ts = floor_to_5m(int(start_local.astimezone(timezone.utc).timestamp()))
    end_ts_exclusive = floor_to_5m(int(end_local.astimezone(timezone.utc).timestamp()))

    return [(f"btc-updown-5m-{ts}", ts) for ts in range(start_ts, end_ts_exclusive, 300)]


def extract_market_from_event(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    markets = ev.get("markets") or []
    markets = parse_maybe_json(markets)
    if isinstance(markets, list) and markets:
        # BTC 5m event should normally have one market.
        return markets[0]
    return None


def infer_final_outcome(market: Dict[str, Any], event: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Prefer explicit winner fields. Fallback to outcomePrices close to 1/0.
    """
    candidates: List[Any] = []
    for obj in [market, event or {}]:
        if not isinstance(obj, dict):
            continue
        for key in [
            "winnerOutcome",
            "winningOutcome",
            "resolutionOutcome",
            "resolvedOutcome",
            "result",
            "winner",
            "outcome",
        ]:
            if key in obj:
                candidates.append(obj.get(key))

    for c in candidates:
        n = norm_outcome(c)
        if n:
            return n

    outcomes = parse_maybe_json(market.get("outcomes"))
    prices = parse_maybe_json(market.get("outcomePrices") or market.get("outcome_prices"))

    if isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) == len(prices) and len(prices) >= 2:
        try:
            price_f = [float(x) for x in prices]
            imax = int(np.argmax(price_f))
            # For resolved markets, winner price should be near 1.
            if price_f[imax] >= 0.90:
                return norm_outcome(outcomes[imax])
        except Exception:
            pass

    return None


def extract_token_ids(market: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    outcomes = parse_maybe_json(market.get("outcomes"))
    token_ids = parse_maybe_json(
        market.get("clobTokenIds")
        or market.get("clob_token_ids")
        or market.get("tokens")
        or market.get("outcomeTokenIds")
    )

    # Some Gamma payloads use list of token dicts.
    if isinstance(token_ids, list) and token_ids and isinstance(token_ids[0], dict):
        maybe_ids = []
        maybe_outcomes = []
        for t in token_ids:
            maybe_ids.append(str(t.get("token_id") or t.get("tokenId") or t.get("id") or t.get("asset_id") or ""))
            maybe_outcomes.append(str(t.get("outcome") or t.get("name") or ""))
        token_ids = maybe_ids
        if not outcomes:
            outcomes = maybe_outcomes

    up_token = down_token = None
    raw_outcomes = json.dumps(outcomes, ensure_ascii=False) if outcomes is not None else None
    raw_token_ids = json.dumps(token_ids, ensure_ascii=False) if token_ids is not None else None

    if isinstance(outcomes, list) and isinstance(token_ids, list) and len(outcomes) == len(token_ids):
        for out, tid in zip(outcomes, token_ids):
            n = norm_outcome(out)
            if n == "up":
                up_token = str(tid)
            elif n == "down":
                down_token = str(tid)

    return up_token, down_token, raw_outcomes, raw_token_ids


def parse_market_ref_from_event(slug: str, start_ts: int, ev: Dict[str, Any]) -> Optional[MarketRef]:
    m = extract_market_from_event(ev)
    if not m:
        return None

    cid = m.get("conditionId") or m.get("condition_id")
    if not cid:
        return None

    up_token, down_token, raw_outcomes, _raw_token_ids = extract_token_ids(m)
    outcome_prices = parse_maybe_json(m.get("outcomePrices") or m.get("outcome_prices"))
    final_outcome = infer_final_outcome(m, ev)

    return MarketRef(
        slug=slug,
        condition_id=str(cid),
        event_id=str(ev.get("id")) if ev.get("id") is not None else None,
        market_id=str(m.get("id")) if m.get("id") is not None else None,
        title=m.get("question") or ev.get("title") or ev.get("question"),
        start_ts=start_ts,
        start_dt_utc=datetime.fromtimestamp(start_ts, timezone.utc).isoformat(),
        final_outcome=final_outcome,
        up_token_id=up_token,
        down_token_id=down_token,
        raw_outcomes=raw_outcomes,
        raw_outcome_prices=json.dumps(outcome_prices, ensure_ascii=False) if outcome_prices is not None else None,
    )


def discover_markets(
    slugs_with_ts: Sequence[Tuple[str, int]],
    batch_size: int = 100,
    sleep_s: float = 0.05,
) -> Tuple[List[MarketRef], List[str]]:
    session = requests.Session()
    refs_by_slug: Dict[str, MarketRef] = {}
    ts_by_slug = dict(slugs_with_ts)

    # Batch query first.
    for batch in tqdm(list(chunks([s for s, _ in slugs_with_ts], batch_size)), desc="Discover Gamma events"):
        params = {"slug": ",".join(batch), "limit": len(batch)}
        try:
            data = get_json(session, f"{GAMMA_API}/events", params=params)
        except Exception:
            data = []

        if isinstance(data, list):
            for ev in data:
                slug = ev.get("slug")
                if not slug or slug not in ts_by_slug:
                    continue
                ref = parse_market_ref_from_event(slug, ts_by_slug[slug], ev)
                if ref:
                    refs_by_slug[slug] = ref

        if sleep_s:
            time.sleep(sleep_s)

    missing = [slug for slug, _ in slugs_with_ts if slug not in refs_by_slug]

    # Fallback: one slug per request.
    for slug in tqdm(missing, desc="Fallback one-by-one events"):
        try:
            ev = get_json(session, f"{GAMMA_API}/events/slug/{slug}")
            if isinstance(ev, dict):
                ref = parse_market_ref_from_event(slug, ts_by_slug[slug], ev)
                if ref:
                    refs_by_slug[slug] = ref
        except Exception:
            pass

        if sleep_s:
            time.sleep(sleep_s)

    refs = [refs_by_slug[s] for s, _ in slugs_with_ts if s in refs_by_slug]
    still_missing = [s for s, _ in slugs_with_ts if s not in refs_by_slug]
    return refs, still_missing


def fetch_trades_for_market(
    session: requests.Session,
    ref: MarketRef,
    limit: int = 10000,
    taker_only: bool = True,
    sleep_s: float = 0.02,
) -> Tuple[List[Dict[str, Any]], bool]:
    rows_all: List[Dict[str, Any]] = []
    maybe_truncated = False

    for offset in (0, 10000):
        params = {
            "market": ref.condition_id,
            "limit": limit,
            "offset": offset,
            "takerOnly": str(taker_only).lower(),
        }
        rows = get_json(session, f"{DATA_API}/trades", params=params)
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected /trades response for {ref.condition_id}: {type(rows)}")

        for t in rows:
            r = dict(t)
            r["_slug"] = ref.slug
            r["_condition_id"] = ref.condition_id
            r["_market_start_ts"] = ref.start_ts
            r["_market_start_dt_utc"] = ref.start_dt_utc
            r["_final_outcome"] = ref.final_outcome
            r["_up_token_id"] = ref.up_token_id
            r["_down_token_id"] = ref.down_token_id

            outcome = norm_outcome(r.get("outcome"))
            asset = str(r.get("asset") or r.get("token") or r.get("tokenId") or r.get("assetId") or "")
            if outcome is None:
                if ref.up_token_id and asset == str(ref.up_token_id):
                    outcome = "up"
                elif ref.down_token_id and asset == str(ref.down_token_id):
                    outcome = "down"
            r["_outcome_norm"] = outcome

            rows_all.append(r)

        if len(rows) < limit:
            break
        if offset == 10000 and len(rows) >= limit:
            maybe_truncated = True

        if sleep_s:
            time.sleep(sleep_s)

    return rows_all, maybe_truncated


def fetch_all_trades(
    refs: Sequence[MarketRef],
    sleep_s: float = 0.02,
    taker_only: bool = True,
) -> Tuple[pd.DataFrame, List[str]]:
    session = requests.Session()
    rows: List[Dict[str, Any]] = []
    truncated: List[str] = []

    for ref in tqdm(refs, desc="Fetch Data API trades"):
        try:
            rs, trunc = fetch_trades_for_market(session, ref, taker_only=taker_only, sleep_s=sleep_s)
            rows.extend(rs)
            if trunc:
                truncated.append(ref.condition_id)
        except Exception as exc:
            print(f"WARNING: failed trades for {ref.slug} {ref.condition_id}: {exc}")

        if sleep_s:
            time.sleep(sleep_s)

    if not rows:
        return pd.DataFrame(), truncated

    df = pd.DataFrame(rows)

    if "price" in df.columns:
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
    if "size" in df.columns:
        df["size"] = pd.to_numeric(df["size"], errors="coerce")
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
        df["datetime_utc"] = pd.to_datetime(df["timestamp"], unit="s", utc=True, errors="coerce")

    dedupe_cols = [
        c for c in ["transactionHash", "_condition_id", "asset", "_outcome_norm", "price", "size", "timestamp"]
        if c in df.columns
    ]
    if dedupe_cols:
        df = df.drop_duplicates(subset=dedupe_cols).reset_index(drop=True)

    return df, truncated


def filter_entry_delay(trades: pd.DataFrame, entry_delay_sec: int) -> pd.DataFrame:
    if entry_delay_sec <= 0:
        return trades.copy()
    if "timestamp" not in trades.columns:
        raise ValueError("entry_delay_sec requires timestamp column")
    df = trades.copy()
    df["_market_start_ts"] = pd.to_numeric(df["_market_start_ts"], errors="coerce")
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    return df[df["timestamp"] >= df["_market_start_ts"] + entry_delay_sec].copy()


def compute_touch_curve_for_side(
    trades: pd.DataFrame,
    market_refs: pd.DataFrame,
    side: str,
    touch_taker_side: str,
    mos: float,
    mts: float,
    max_chunks_budget: int,
    min_price: float,
    max_price: float,
) -> pd.DataFrame:
    """
    side='up': denominator is all final-up markets; numerator uses Up token trades in those markets.
    side='down': denominator is all final-down markets; numerator uses Down token trades in those markets.
    touch_taker_side='sell' estimates passive limit-buy fills from taker sells.
    """
    side = side.lower()
    assert side in {"up", "down"}
    touch_taker_side = touch_taker_side.lower()
    assert touch_taker_side in {"sell", "buy", "all"}

    obs_markets = market_refs[market_refs["final_outcome"].astype(str).str.lower().eq(side)]["condition_id"].astype(str).unique()
    obs_markets = sorted(obs_markets)
    n_obs = len(obs_markets)

    if n_obs == 0:
        raise ValueError(f"No final-{side} markets; cannot estimate a_{side}")

    df = trades.copy()
    if df.empty:
        df = pd.DataFrame(columns=["_condition_id", "_outcome_norm", "price", "size"])

    df["_condition_id"] = df["_condition_id"].astype(str)
    df["_outcome_norm"] = df["_outcome_norm"].astype(str).str.lower()
    df = df[df["_condition_id"].isin(obs_markets) & df["_outcome_norm"].eq(side)].copy()

    if touch_taker_side != "all":
        if "side" not in df.columns:
            raise ValueError("touch_taker_side requires side column in trades")
        df["side"] = df["side"].astype(str).str.lower().str.strip()
        df = df[df["side"].eq(touch_taker_side)].copy()

    if not df.empty:
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["size"] = pd.to_numeric(df["size"], errors="coerce")
        df = df.dropna(subset=["price", "size"])
        df = df[(df["price"] >= 0) & (df["price"] <= 1) & (df["size"] > 0)].copy()
        df["price_tick"] = (np.round(df["price"] / mts) * mts).round(10)
        vol = (
            df.groupby(["_condition_id", "price_tick"], as_index=False)["size"]
            .sum()
            .rename(columns={"size": "volume_shares"})
        )
        wide = vol.pivot_table(
            index="_condition_id",
            columns="price_tick",
            values="volume_shares",
            aggfunc="sum",
            fill_value=0.0,
        )
    else:
        wide = pd.DataFrame(index=obs_markets)

    wide = wide.reindex(index=obs_markets, fill_value=0.0)

    price_grid = np.round(np.arange(min_price, max_price + 0.5 * mts, mts), 10)

    for p in price_grid:
        if p not in wide.columns:
            wide[p] = 0.0

    wide = wide[sorted(wide.columns)]
    cumsum = wide.cumsum(axis=1)

    rows: List[Dict[str, Any]] = []
    for p in price_grid:
        cum_vol = cumsum[p].astype(float) if p in cumsum.columns else pd.Series(0.0, index=cumsum.index)

        for k in range(1, max_chunks_budget + 1):
            required = k * mos
            touched = int((cum_vol >= required).sum())
            a = touched / n_obs
            rows.append(
                {
                    "side": side,
                    "price": float(p),
                    "chunk_index": int(k),
                    "required_shares": float(required),
                    "a_touch_capacity": float(a),
                    "n_observations": int(n_obs),
                    "n_touched": touched,
                }
            )

    return pd.DataFrame(rows)


def add_ev_columns(
    a_curve: pd.DataFrame,
    q: float,
    mos: float,
    a_haircut: float = 1.0,
    min_touched: int = 0,
) -> pd.DataFrame:
    df = a_curve.copy()
    df["q"] = q

    df["a_raw"] = df["a_touch_capacity"].astype(float)
    df["a_used"] = (df["a_raw"] * a_haircut).clip(0, 1)

    if min_touched > 0:
        df.loc[df["n_touched"].astype(int) < min_touched, "a_used"] = 0.0

    p = df["price"].astype(float)
    a = df["a_used"].astype(float)
    df["breakeven_a"] = ((1.0 - q) * p) / (q * (1.0 - p))
    df["notional"] = p * mos
    df["notional_cents"] = (df["notional"] * 100).round().astype(int)
    df["expected_profit"] = q * a * mos * (1.0 - p) - (1.0 - q) * mos * p
    df["edge_per_share"] = q * a * (1.0 - p) - (1.0 - q) * p
    df["positive_ev"] = df["expected_profit"] > 0
    return df


def solve_ladder(
    ev_curve: pd.DataFrame,
    budget: float,
    mos: float,
    max_notional_per_price: float,
    positive_ev_only: bool = True,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Group knapsack by price. Each price group can choose n=0..K prefix chunks.
    This enforces: cannot select chunk k unless chunks 1..k at same price are selected.

    Also enforces:
        notional selected at each price <= max_notional_per_price
    """
    df = ev_curve.copy()
    budget_cents = int(round(budget * 100))
    max_price_cents = int(round(max_notional_per_price * 100))

    groups: List[Tuple[float, List[Dict[str, Any]]]] = []

    for price, g in df.groupby("price", sort=True):
        g = g.sort_values("chunk_index")
        choices = [{"n": 0, "cost_cents": 0, "value": 0.0, "row_indices": []}]

        cum_cost = 0
        cum_value = 0.0
        idxs: List[int] = []

        for idx, r in g.iterrows():
            ev = float(r["expected_profit"])
            chunk_cost = int(r["notional_cents"])

            if positive_ev_only and ev <= 0:
                # a(p,k) is non-increasing in k, so later chunks should not recover under the same p.
                break

            cum_cost += chunk_cost
            cum_value += ev
            idxs.append(idx)

            if cum_cost <= budget_cents and cum_cost <= max_price_cents:
                choices.append(
                    {
                        "n": int(r["chunk_index"]),
                        "cost_cents": int(cum_cost),
                        "value": float(cum_value),
                        "row_indices": list(idxs),
                    }
                )
            else:
                break

        groups.append((float(price), choices))

    # Dynamic programming state: total cost -> (value, selected row indices)
    states: Dict[int, Tuple[float, List[int]]] = {0: (0.0, [])}

    for _price, choices in groups:
        new_states = dict(states)
        for current_cost, (current_value, current_rows) in states.items():
            for choice in choices[1:]:
                new_cost = current_cost + int(choice["cost_cents"])
                if new_cost > budget_cents:
                    continue
                new_value = current_value + float(choice["value"])
                if new_cost not in new_states or new_value > new_states[new_cost][0]:
                    new_states[new_cost] = (new_value, current_rows + list(choice["row_indices"]))
        states = new_states

    best_cost, (best_value, best_rows) = max(states.items(), key=lambda kv: kv[1][0])
    selected_chunks = df.loc[best_rows].copy() if best_rows else df.iloc[0:0].copy()

    if selected_chunks.empty:
        ladder = pd.DataFrame(
            columns=[
                "side", "price", "shares", "notional", "avg_a", "min_a", "max_chunk_index",
                "expected_profit", "breakeven_a", "wrong_case_loss", "win_profit_if_all_filled",
            ]
        )
        return selected_chunks, ladder

    ladder = (
        selected_chunks.groupby(["side", "price"], as_index=False)
        .agg(
            shares=("chunk_index", lambda x: float(len(x) * mos)),
            notional=("notional", "sum"),
            avg_a=("a_used", "mean"),
            min_a=("a_used", "min"),
            raw_avg_a=("a_raw", "mean"),
            raw_min_a=("a_raw", "min"),
            min_n_touched=("n_touched", "min"),
            max_chunk_index=("chunk_index", "max"),
            expected_profit=("expected_profit", "sum"),
            breakeven_a=("breakeven_a", "mean"),
        )
        .sort_values("price")
    )

    ladder["wrong_case_loss"] = ladder["notional"]
    ladder["win_profit_if_all_filled"] = ladder["shares"] * (1.0 - ladder["price"])

    ladder = ladder[
        [
            "side", "price", "shares", "notional",
            "avg_a", "min_a", "raw_avg_a", "raw_min_a", "min_n_touched",
            "breakeven_a", "max_chunk_index", "expected_profit",
            "wrong_case_loss", "win_profit_if_all_filled",
        ]
    ]

    return selected_chunks, ladder


def plot_touch_curve(curve: pd.DataFrame, side: str, out_png: Path, max_plot_chunks: int, plot_min_price: float, plot_max_price: float) -> None:
    fig, ax = plt.subplots(figsize=(12, 6))
    side_df = curve[
        curve["side"].eq(side)
        & (curve["price"] >= plot_min_price)
        & (curve["price"] <= plot_max_price)
    ].copy()

    for k in range(1, max_plot_chunks + 1):
        g = side_df[side_df["chunk_index"].eq(k)].sort_values("price")
        if g.empty:
            continue
        ax.plot(g["price"], g["a_touch_capacity"], marker="o", markersize=2, linewidth=1.2, label=f"k={k}, {int(g['required_shares'].iloc[0])} shares")

    ax.set_title(f"BTC 5m winner-only touch probability — {side.title()}")
    ax.set_xlabel("Limit buy price p")
    ax.set_ylabel("a(p,k): cumulative winner volume <= p reaches k*mos")
    ax.set_xlim(plot_min_price, plot_max_price)
    ax.set_ylim(0, 1)
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=1.0))
    ax.grid(axis="y", alpha=0.25)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png, dpi=180)
    plt.close(fig)


def plot_ev_curve(ev_curve: pd.DataFrame, side: str, out_png: Path, max_plot_chunks: int, plot_min_price: float, plot_max_price: float) -> None:
    fig, ax = plt.subplots(figsize=(12, 6))
    side_df = ev_curve[
        ev_curve["side"].eq(side)
        & (ev_curve["price"] >= plot_min_price)
        & (ev_curve["price"] <= plot_max_price)
    ].copy()

    for k in range(1, max_plot_chunks + 1):
        g = side_df[side_df["chunk_index"].eq(k)].sort_values("price")
        if g.empty:
            continue
        ax.plot(g["price"], g["expected_profit"], marker="o", markersize=2, linewidth=1.2, label=f"k={k}")

    ax.axhline(0, linewidth=1)
    ax.set_title(f"BTC 5m expected profit per {side.title()} mos chunk, b=1")
    ax.set_xlabel("Limit buy price p")
    ax.set_ylabel("Expected profit per mos chunk, USDC")
    ax.set_xlim(plot_min_price, plot_max_price)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png, dpi=180)
    plt.close(fig)


def plot_ladder(ladder: pd.DataFrame, side: str, out_png: Path, budget: float, max_notional_per_price: float) -> None:
    fig, ax = plt.subplots(figsize=(12, 5))
    if ladder.empty:
        ax.set_title(f"No positive-EV ladder selected — {side.title()}")
        ax.set_xlabel("Limit buy price p")
        ax.set_ylabel("Notional USDC")
    else:
        ax.bar(ladder["price"], ladder["notional"], width=0.008, align="center", edgecolor="black", linewidth=0.4)
        ax.axhline(max_notional_per_price, linewidth=1, linestyle="--", label=f"per-price cap = {max_notional_per_price:g}")
        ax.set_title(f"Optimized {side.title()} ladder, budget <= {budget:g} USDC")
        ax.set_xlabel("Limit buy price p")
        ax.set_ylabel("Notional USDC")
        ax.grid(axis="y", alpha=0.25)
        ax.legend()
    fig.tight_layout()
    fig.savefig(out_png, dpi=180)
    plt.close(fig)


def plot_selected_chunks(selected: pd.DataFrame, side: str, out_png: Path) -> None:
    fig, ax = plt.subplots(figsize=(12, 6))
    if selected.empty:
        ax.set_title(f"No selected chunks — {side.title()}")
        fig.tight_layout()
        fig.savefig(out_png, dpi=180)
        plt.close(fig)
        return

    for price, g in selected.groupby("price", sort=True):
        g = g.sort_values("chunk_index")
        ax.plot(g["chunk_index"], g["expected_profit"], marker="o", markersize=2, linewidth=1.0, label=f"p={price:.2f}")

    ax.axhline(0, linewidth=1)
    ax.set_title(f"Selected chunks EV by k — {side.title()}")
    ax.set_xlabel("chunk index k")
    ax.set_ylabel("EV per mos chunk, USDC")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png, dpi=180)
    plt.close(fig)


def write_summary(
    out_path: Path,
    params: Dict[str, Any],
    market_refs: pd.DataFrame,
    trades: pd.DataFrame,
    ladder_up: pd.DataFrame,
    ladder_down: pd.DataFrame,
) -> None:
    def ladder_block(name: str, ladder: pd.DataFrame) -> str:
        if ladder.empty:
            return f"\n{name}: no positive-EV ladder selected.\n"
        total_notional = ladder["notional"].sum()
        total_ev = ladder["expected_profit"].sum()
        txt = [
            f"\n{name}:",
            f"  total_notional = {total_notional:.4f} USDC",
            f"  total_expected_profit = {total_ev:.4f} USDC",
            f"  expected_roi_on_budget_cap = {total_ev / params['budget']:.4%}",
            f"  wrong_case_loss_b_eq_1 = {total_notional:.4f} USDC",
            "",
            ladder.to_string(index=False),
            "",
        ]
        return "\n".join(txt)

    lines = [
        "Polymarket BTC 5m winner-only touch-probability strategy summary",
        "=" * 72,
        json.dumps(params, indent=2, ensure_ascii=False),
        "",
        f"Resolved markets: {len(market_refs)}",
        f"Final Up markets: {(market_refs['final_outcome'] == 'up').sum() if not market_refs.empty else 0}",
        f"Final Down markets: {(market_refs['final_outcome'] == 'down').sum() if not market_refs.empty else 0}",
        f"Trade rows used for touch: {len(trades)}",
        "",
        "EV formula:",
        "  EV_s(p,k) = q_s * a_s(p,k) * mos * (1-p) - (1-q_s) * mos * p",
        "  b is fixed at 1 in the loss term.",
        "",
        "Key constraints:",
        f"  total budget <= {params['budget']} USDC",
        f"  per price notional <= {params['max_notional_per_price']} USDC",
        f"  strategy price range = [{params['strategy_min_price']}, {params['strategy_max_price']}]",
        f"  plot price range = [{params['plot_min_price']}, {params['plot_max_price']}]",
        "",
        "Use ladder_up.csv if your model predicts Up; use ladder_down.csv if it predicts Down.",
        ladder_block("UP ladder", ladder_up),
        ladder_block("DOWN ladder", ladder_down),
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument("--days", type=int, default=7, help="Number of complete calendar days to pull.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional last calendar date included, YYYY-MM-DD, in --timezone.")
    parser.add_argument("--timezone", type=str, default="Asia/Singapore", help="Timezone for calendar-day boundaries.")
    parser.add_argument("--outdir", type=Path, default=Path("out_btc5m_7d_touch_strategy"))

    parser.add_argument("--budget", type=float, default=10.0)
    parser.add_argument("--max-notional-per-price-frac", type=float, default=0.20, help="Default 20%% of total budget per price.")
    parser.add_argument("--max-notional-per-price", type=float, default=None, help="Absolute cap; overrides fraction if set.")

    parser.add_argument("--mos", type=float, default=5.0)
    parser.add_argument("--mts", type=float, default=0.01)

    parser.add_argument("--strategy-min-price", type=float, default=0.01)
    parser.add_argument("--strategy-max-price", type=float, default=0.50)
    parser.add_argument("--plot-min-price", type=float, default=0.01)
    parser.add_argument("--plot-max-price", type=float, default=0.99)

    parser.add_argument("--q-up", type=float, default=0.60)
    parser.add_argument("--q-down", type=float, default=0.60)
    parser.add_argument("--a-haircut", type=float, default=1.0)
    parser.add_argument("--min-touched", type=int, default=0, help="Set a_used=0 for chunks touched by fewer than this many observations.")
    parser.add_argument("--entry-delay-sec", type=int, default=0)
    parser.add_argument(
        "--touch-taker-side",
        choices=["sell", "buy", "all"],
        default="sell",
        help="Trades used for touch/capacity. sell estimates passive limit-buy fills; all reproduces the old BUY+SELL touch curve.",
    )

    parser.add_argument("--max-plot-chunks", type=int, default=20)
    parser.add_argument("--gamma-batch-size", type=int, default=100)
    parser.add_argument("--sleep", type=float, default=0.03)
    parser.add_argument("--include-maker-legs", action="store_true", help="Use takerOnly=false. Default takerOnly=true counts fills once.")
    parser.add_argument("--allow-negative-ev", action="store_true", help="Allow negative-EV chunks in optimizer; usually not recommended.")

    parser.add_argument("--input-trades-csv", type=Path, default=None, help="Path to existing trades_raw.csv to skip fetching.")
    parser.add_argument("--input-refs-csv", type=Path, default=None, help="Path to existing market_refs_resolved.csv to skip discovery.")

    args = parser.parse_args()

    if args.days <= 0:
        raise ValueError("--days must be positive")
    if args.end_date:
        end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    else:
        end_date = None

    if args.budget <= 0:
        raise ValueError("--budget must be positive")
    if args.max_notional_per_price is not None:
        max_notional_per_price = args.max_notional_per_price
    else:
        max_notional_per_price = args.budget * args.max_notional_per_price_frac

    if max_notional_per_price <= 0 or max_notional_per_price > args.budget:
        raise ValueError("Invalid per-price cap")

    if args.mos <= 0 or args.mts <= 0:
        raise ValueError("--mos and --mts must be positive")
    if not (0 < args.q_up < 1 and 0 < args.q_down < 1):
        raise ValueError("--q-up and --q-down must be in (0,1)")
    if not (0 <= args.a_haircut <= 1):
        raise ValueError("--a-haircut must be in [0,1]")

    for name in ["strategy_min_price", "strategy_max_price", "plot_min_price", "plot_max_price"]:
        val = getattr(args, name)
        if val <= 0 or val >= 1:
            raise ValueError(f"--{name.replace('_', '-')} must be in (0,1)")

    if args.strategy_min_price > args.strategy_max_price:
        raise ValueError("Invalid strategy price range")
    if args.plot_min_price > args.plot_max_price:
        raise ValueError("Invalid plot price range")

    args.outdir.mkdir(parents=True, exist_ok=True)
    trades_raw_path = args.outdir / "trades_raw.csv"

    if args.input_trades_csv and args.input_refs_csv:
        print(f"Loading local data from {args.input_trades_csv} and {args.input_refs_csv}")
        trades = pd.read_csv(args.input_trades_csv)
        refs_resolved_df = pd.read_csv(args.input_refs_csv)
        # Ensure correct types for merging/grouping
        if "price" in trades.columns:
            trades["price"] = pd.to_numeric(trades["price"], errors="coerce")
        if "size" in trades.columns:
            trades["size"] = pd.to_numeric(trades["size"], errors="coerce")
        if "_condition_id" in trades.columns:
            trades["_condition_id"] = trades["_condition_id"].astype(str)
        if "condition_id" in refs_resolved_df.columns:
            refs_resolved_df["condition_id"] = refs_resolved_df["condition_id"].astype(str)
        
        slugs = [] # Not used in strategy calculation if data is loaded
    else:
        slugs = lookback_window_to_slugs(args.days, args.timezone, end_date=end_date)
        print(f"Lookback days: {args.days} complete calendar days in {args.timezone}")
        print(f"Generated {len(slugs)} BTC 5m slugs")

        refs, missing = discover_markets(slugs, batch_size=args.gamma_batch_size, sleep_s=args.sleep)
        refs_df = pd.DataFrame([asdict(r) for r in refs])
        refs_df.to_csv(args.outdir / "market_refs.csv", index=False)

        if missing:
            (args.outdir / "missing_slugs.txt").write_text("\n".join(missing), encoding="utf-8")
            print(f"WARNING: missing slugs: {len(missing)}")

        refs_resolved = [r for r in refs if r.final_outcome in {"up", "down"}]
        if not refs_resolved:
            raise SystemExit("No resolved Up/Down markets found. Try a previous --end-date or inspect market_refs.csv")

        refs_resolved_df = pd.DataFrame([asdict(r) for r in refs_resolved])
        refs_resolved_df.to_csv(args.outdir / "market_refs_resolved.csv", index=False)

        trades, truncated = fetch_all_trades(
            refs_resolved,
            sleep_s=args.sleep,
            taker_only=not args.include_maker_legs,
        )
        if truncated:
            (args.outdir / "possibly_truncated_condition_ids.txt").write_text("\n".join(truncated), encoding="utf-8")

        trades_raw_path = args.outdir / "trades_raw.csv"
        trades.to_csv(trades_raw_path, index=False)

    trades_for_touch = filter_entry_delay(trades, args.entry_delay_sec)
    trades_for_touch.to_csv(args.outdir / "trades_for_touch.csv", index=False)

    # For plots, compute full range and enough k to cover the per-price cap at the smallest strategy price.
    max_chunks_for_cap = int(math.floor(max_notional_per_price / (args.strategy_min_price * args.mos)))
    max_chunks_for_cap = max(1, max_chunks_for_cap)
    max_chunks_for_plot = max(max_chunks_for_cap, args.max_plot_chunks)

    a_up_full = compute_touch_curve_for_side(
        trades_for_touch, refs_resolved_df, side="up",
        touch_taker_side=args.touch_taker_side,
        mos=args.mos, mts=args.mts, max_chunks_budget=max_chunks_for_plot,
        min_price=args.plot_min_price, max_price=args.plot_max_price,
    )
    a_down_full = compute_touch_curve_for_side(
        trades_for_touch, refs_resolved_df, side="down",
        touch_taker_side=args.touch_taker_side,
        mos=args.mos, mts=args.mts, max_chunks_budget=max_chunks_for_plot,
        min_price=args.plot_min_price, max_price=args.plot_max_price,
    )

    a_curve_full = pd.concat([a_up_full, a_down_full], ignore_index=True)
    a_curve_full.to_csv(args.outdir / "touch_probability_curve_full_plot_range.csv", index=False)

    # Strategy curves are filtered from the full curve.
    strategy_mask = (
        (a_curve_full["price"] >= args.strategy_min_price)
        & (a_curve_full["price"] <= args.strategy_max_price)
    )
    a_curve_strategy = a_curve_full[strategy_mask].copy()
    a_curve_strategy.to_csv(args.outdir / "touch_probability_curve_strategy_range.csv", index=False)

    a_up_strategy = a_curve_strategy[a_curve_strategy["side"].eq("up")].copy()
    a_down_strategy = a_curve_strategy[a_curve_strategy["side"].eq("down")].copy()

    ev_up = add_ev_columns(
        a_up_strategy, q=args.q_up, mos=args.mos,
        a_haircut=args.a_haircut, min_touched=args.min_touched,
    )
    ev_down = add_ev_columns(
        a_down_strategy, q=args.q_down, mos=args.mos,
        a_haircut=args.a_haircut, min_touched=args.min_touched,
    )
    ev_curve_strategy = pd.concat([ev_up, ev_down], ignore_index=True)
    ev_curve_strategy.to_csv(args.outdir / "ev_curve_strategy_range.csv", index=False)

    # EV for full plot range, separately so the picture is complete.
    ev_up_full_plot = add_ev_columns(
        a_up_full, q=args.q_up, mos=args.mos,
        a_haircut=args.a_haircut, min_touched=args.min_touched,
    )
    ev_down_full_plot = add_ev_columns(
        a_down_full, q=args.q_down, mos=args.mos,
        a_haircut=args.a_haircut, min_touched=args.min_touched,
    )
    ev_curve_full_plot = pd.concat([ev_up_full_plot, ev_down_full_plot], ignore_index=True)
    ev_curve_full_plot.to_csv(args.outdir / "ev_curve_full_plot_range.csv", index=False)

    selected_up, ladder_up = solve_ladder(
        ev_up,
        budget=args.budget,
        mos=args.mos,
        max_notional_per_price=max_notional_per_price,
        positive_ev_only=not args.allow_negative_ev,
    )
    selected_down, ladder_down = solve_ladder(
        ev_down,
        budget=args.budget,
        mos=args.mos,
        max_notional_per_price=max_notional_per_price,
        positive_ev_only=not args.allow_negative_ev,
    )

    selected_up.to_csv(args.outdir / "selected_chunks_up.csv", index=False)
    selected_down.to_csv(args.outdir / "selected_chunks_down.csv", index=False)
    ladder_up.to_csv(args.outdir / "ladder_up.csv", index=False)
    ladder_down.to_csv(args.outdir / "ladder_down.csv", index=False)

    plot_touch_curve(
        a_curve_full, "up", args.outdir / "touch_probability_up_full.png",
        args.max_plot_chunks, args.plot_min_price, args.plot_max_price,
    )
    plot_touch_curve(
        a_curve_full, "down", args.outdir / "touch_probability_down_full.png",
        args.max_plot_chunks, args.plot_min_price, args.plot_max_price,
    )
    plot_ev_curve(
        ev_curve_full_plot, "up", args.outdir / "ev_curve_up_full.png",
        args.max_plot_chunks, args.plot_min_price, args.plot_max_price,
    )
    plot_ev_curve(
        ev_curve_full_plot, "down", args.outdir / "ev_curve_down_full.png",
        args.max_plot_chunks, args.plot_min_price, args.plot_max_price,
    )
    plot_ladder(ladder_up, "up", args.outdir / "ladder_up.png", args.budget, max_notional_per_price)
    plot_ladder(ladder_down, "down", args.outdir / "ladder_down.png", args.budget, max_notional_per_price)
    plot_selected_chunks(selected_up, "up", args.outdir / "selected_chunks_ev_by_k_up.png")
    plot_selected_chunks(selected_down, "down", args.outdir / "selected_chunks_ev_by_k_down.png")

    params = {
        "days": args.days,
        "end_date": str(end_date) if end_date else None,
        "timezone": args.timezone,
        "budget": args.budget,
        "max_notional_per_price": max_notional_per_price,
        "max_notional_per_price_frac": args.max_notional_per_price_frac if args.max_notional_per_price is None else None,
        "mos": args.mos,
        "mts": args.mts,
        "strategy_min_price": args.strategy_min_price,
        "strategy_max_price": args.strategy_max_price,
        "plot_min_price": args.plot_min_price,
        "plot_max_price": args.plot_max_price,
        "q_up": args.q_up,
        "q_down": args.q_down,
        "b": 1.0,
        "a_haircut": args.a_haircut,
        "min_touched": args.min_touched,
        "entry_delay_sec": args.entry_delay_sec,
        "touch_taker_side": args.touch_taker_side,
        "taker_only": not args.include_maker_legs,
        "n_slugs": len(slugs),
        "n_resolved_markets": len(refs_resolved_df),
        "n_trades_raw": int(len(trades)),
        "n_trades_for_touch": int(len(trades_for_touch)),
        "n_final_up_markets": int((refs_resolved_df["final_outcome"] == "up").sum()),
        "n_final_down_markets": int((refs_resolved_df["final_outcome"] == "down").sum()),
    }
    (args.outdir / "run_meta.json").write_text(json.dumps(params, indent=2, ensure_ascii=False), encoding="utf-8")
    write_summary(args.outdir / "strategy_summary.txt", params, refs_resolved_df, trades_for_touch, ladder_up, ladder_down)

    print("\nDone.")
    print(f"Output dir: {args.outdir}")
    print(f"Market refs: {args.outdir / 'market_refs_resolved.csv'}")
    print(f"Trades: {trades_raw_path}")
    print(f"Touch full plot range: {args.outdir / 'touch_probability_curve_full_plot_range.csv'}")
    print(f"Touch strategy range: {args.outdir / 'touch_probability_curve_strategy_range.csv'}")
    print(f"EV strategy range: {args.outdir / 'ev_curve_strategy_range.csv'}")
    print(f"Up ladder: {args.outdir / 'ladder_up.csv'}")
    print(f"Down ladder: {args.outdir / 'ladder_down.csv'}")
    print(f"Summary: {args.outdir / 'strategy_summary.txt'}")


if __name__ == "__main__":
    main()

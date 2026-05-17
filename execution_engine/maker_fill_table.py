from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from execution_engine.maker_order_plan import bucket_price, bucket_second


DEFAULT_LIMIT_PRICES = [round(x / 100, 2) for x in range(45, 71, 5)]
DEFAULT_ORDER_DELAYS = [120, 150, 180, 240]


def generate_maker_fill_table(
    trades_csv: Path,
    out_path: Path,
    decision_second: int = 120,
    limit_prices: list[float] | None = None,
    order_delays: list[int] | None = None,
    min_bucket_markets: int = 30,
) -> pd.DataFrame:
    trades = _load_trade_subset(trades_csv)
    trades = trades.dropna(subset=["condition_id", "market_start_ts", "timestamp", "price", "size"]).copy()
    trades["second_from_start"] = trades["timestamp"] - trades["market_start_ts"]
    trades = trades[(trades["second_from_start"] >= 0) & trades["price"].between(0, 1)].copy()
    trades["side"] = trades.get("side", pd.Series("", index=trades.index)).astype(str).str.lower()
    sell = trades[trades["side"].eq("sell")].copy()
    if sell.empty:
        sell = trades.copy()
    markets = trades[["condition_id", "market_start_ts", "final_outcome"]].drop_duplicates("condition_id")
    rows = []
    limit_prices = limit_prices or DEFAULT_LIMIT_PRICES
    order_delays = order_delays or DEFAULT_ORDER_DELAYS
    for prediction_side in ["up", "down"]:
        current_prices = _current_prices(trades, prediction_side, decision_second)
        base = markets.merge(current_prices, on="condition_id", how="left")
        base["current_price"] = base["current_price"].fillna(0.5)
        base["decision_second_bucket"] = bucket_second(decision_second)
        base["current_price_bucket"] = base["current_price"].map(bucket_price)
        side_sell = sell[sell["outcome_norm"].eq(prediction_side)]
        min_price_by_delay = {
            delay: side_sell[side_sell["second_from_start"] >= delay].groupby("condition_id")["price"].min()
            for delay in order_delays
        }
        for order_delay in order_delays:
            min_price = min_price_by_delay[order_delay]
            for limit_price in limit_prices:
                rows.extend(_rows_for_candidate(base, prediction_side, order_delay, limit_price, min_price, min_bucket_markets))
    df = pd.DataFrame(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".parquet":
        df.to_parquet(out_path, index=False)
    else:
        df.to_csv(out_path, index=False)
    return df


def _load_trade_subset(path: Path) -> pd.DataFrame:
    rename = {
        "conditionId": "condition_id",
        "_condition_id": "condition_id",
        "_market_start_ts": "market_start_ts",
        "_final_outcome": "final_outcome",
        "_outcome_norm": "outcome_norm",
    }
    wanted = {
        "conditionId",
        "_condition_id",
        "condition_id",
        "_market_start_ts",
        "market_start_ts",
        "timestamp",
        "price",
        "size",
        "side",
        "_final_outcome",
        "final_outcome",
        "_outcome_norm",
        "outcome_norm",
    }
    df = pd.read_csv(path, usecols=lambda c: c in wanted)
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    df = _coalesce_duplicate_columns(df)
    for col in ["timestamp", "market_start_ts", "price", "size"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["condition_id", "side", "final_outcome", "outcome_norm"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.lower().str.strip()
    return df


def _coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if not df.columns.has_duplicates:
        return df
    out = pd.DataFrame(index=df.index)
    for col in dict.fromkeys(df.columns):
        same = df.loc[:, df.columns == col]
        out[col] = same.bfill(axis=1).iloc[:, 0] if same.shape[1] > 1 else same.iloc[:, 0]
    return out


def _rows_for_candidate(
    base_template: pd.DataFrame,
    prediction_side: str,
    order_delay: int,
    limit_price: float,
    min_price_by_market: pd.Series,
    min_bucket_markets: int,
) -> list[dict[str, object]]:
    base = base_template.copy()
    fill_markets = set(min_price_by_market[min_price_by_market <= limit_price].index.astype(str))
    base["filled"] = base["condition_id"].isin(fill_markets)
    fine_rows = []
    for (decision_bucket, price_bucket), part in base.groupby(["decision_second_bucket", "current_price_bucket"], dropna=False):
        fine_rows.append(_aggregate(part, prediction_side, str(decision_bucket), str(price_bucket), order_delay, limit_price, "fine"))
    coarse = _aggregate(base, prediction_side, "any", "any", order_delay, limit_price, "side_delay_price")
    out = []
    for row in fine_rows:
        total = int(row["win_market_count"]) + int(row["lose_market_count"])
        out.append(row if total >= min_bucket_markets else dict(coarse, decision_second_bucket=row["decision_second_bucket"], current_price_bucket=row["current_price_bucket"], fallback_level="side_delay_price"))
    out.append(coarse)
    return out


def _current_prices(trades: pd.DataFrame, prediction_side: str, decision_second: int) -> pd.DataFrame:
    side_trades = trades[(trades["outcome_norm"].eq(prediction_side)) & (trades["second_from_start"] <= decision_second)].copy()
    if side_trades.empty:
        return pd.DataFrame(columns=["condition_id", "current_price"])
    side_trades = side_trades.sort_values(["condition_id", "second_from_start"])
    return side_trades.groupby("condition_id").tail(1)[["condition_id", "price"]].rename(columns={"price": "current_price"})


def _aggregate(part: pd.DataFrame, prediction_side: str, decision_bucket: str, price_bucket: str, order_delay: int, limit_price: float, fallback_level: str) -> dict[str, object]:
    win = part[part["final_outcome"].eq(prediction_side)]
    lose = part[~part["final_outcome"].eq(prediction_side)]
    win_count = int(len(win))
    lose_count = int(len(lose))
    win_fill = int(win["filled"].sum()) if win_count else 0
    lose_fill = int(lose["filled"].sum()) if lose_count else 0
    return {
        "prediction_side": prediction_side,
        "decision_second_bucket": decision_bucket,
        "current_price_bucket": price_bucket,
        "order_delay_seconds": int(order_delay),
        "limit_price": float(limit_price),
        "win_market_count": win_count,
        "win_fill_market_count": win_fill,
        "a_win_market_fill": float(win_fill / win_count) if win_count else 0.0,
        "lose_market_count": lose_count,
        "lose_fill_market_count": lose_fill,
        "a_lose_market_fill": float(lose_fill / lose_count) if lose_count else 0.0,
        "fallback_level": fallback_level,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades-csv", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--decision-second", type=int, default=120)
    args = parser.parse_args()
    generate_maker_fill_table(args.trades_csv, args.out, args.decision_second)


if __name__ == "__main__":
    main()

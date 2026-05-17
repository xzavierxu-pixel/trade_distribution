# Polymarket BTC 5m Maker Limit Buy Method

This document defines the offline and runtime method used by `fortune_bot` for
BTC 5 minute Up/Down markets. The strategy only creates maker limit buy plans.
It does not assume that every signal should trade.

## Decision Inputs

The model first chooses a direction:

```text
side in {up, down}
```

The directional win probability is `q`. It must not be hard-coded. Runtime uses
the first available source in this order:

1. calibrated real-time `p_side`
2. accepted accuracy from the matching validation probability bucket
3. overall validation `accepted_sample_accuracy` from the manifest

For an Up signal, `p_side = p_up`. For a Down signal,
`p_side = 1 - p_up`.

## Candidate Orders

A candidate maker order is:

```text
j = (t_j, p_j)
```

where:

- `t_j` is the order delay in seconds from market start
- `p_j` is the limit buy price after tick-size rounding
- `B_j` is the USDC budget assigned to that candidate

All runtime orders must be maker limit buys. Market orders and taker orders are
out of scope for this strategy.

## Offline Fill Probability

For every candidate, the offline pipeline estimates:

```text
a_j = P(fill_j | win, context)
```

The fill proxy is historical taker `SELL` activity in the predicted token. A
taker sell at or below our bid is treated as evidence that a passive buy could
have filled.

The fill table includes at least these dimensions:

- `prediction_side`
- `decision_second_bucket`
- `current_price_bucket`
- `order_delay_seconds`
- `limit_price`

It records:

- `win_market_count`
- `win_fill_market_count`
- `a_win_market_fill`
- `lose_market_count`
- `lose_fill_market_count`
- `a_lose_market_fill`
- `fallback_level`

If a fine bucket has too few markets, the row falls back to a coarser
side/delay/price estimate and marks that fallback in `fallback_level`.

## Edge Formula

The conservative wrong-side fill assumption is:

```text
b = P(fill | lose) = 1
```

Expected return per 1 USDC of order budget is:

```text
edge_j = q * a_j * (1 / p_j - 1) - (1 - q) * b
```

Only orders with:

```text
edge_j > 0
```

are eligible for budget allocation.

## Order Constraints

Every selected order must satisfy:

```text
sum(B_j) <= 7 USDC
B_j <= 4 USDC
B_j / p_j >= 5 shares
edge_j > 0
```

The runtime also enforces:

- price rounded down to market tick size
- price within Polymarket min/max bounds
- no duplicate `(market, side, order_delay_seconds, limit_price)` order key
- no live submission unless config enables orders and the manifest is
  `live_eligible=true`

## Budget Allocation

The planner:

1. computes `edge_j` for all candidates
2. keeps only positive EV candidates
3. enumerates combinations of 1 to 3 orders
4. assigns each order its minimum budget `min_B_j = 5 * p_j`
5. allocates remaining budget to higher-edge orders up to `B_j <= 4`
6. selects the combination with maximum `sum(B_j * edge_j)`

The planner does not spend unused budget unless the extra spend has positive
expected value.

## Runtime Audit Fields

Every paper/live summary should record:

- UTC timestamp
- model version and artifact hash
- market condition id and decision window
- feature source and feature count
- `p_up`, `p_down`, thresholds, decision
- `q` and `q_source`
- maker fill table bucket and fallback level
- candidate edge values
- selected order prices, shares, budgets, and expected value
- paper/live mode and Polymarket response status

Secrets, private keys, CLOB passphrases, and full deposit wallet addresses must
not be written to logs or artifacts.

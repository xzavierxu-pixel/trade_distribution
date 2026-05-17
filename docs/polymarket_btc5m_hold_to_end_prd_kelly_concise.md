# Polymarket BTC 5m Maker Limit Buy PRD — Kelly Hold-to-End

## 1. Final Strategy

Runtime uses static maker limit buy orders:

```text
submit immediately after valid model decision
hold order until market end
no submit_second_anchor
no order_delay_seconds
no taker / market orders
```

Final price-selection rule:

```text
select limit prices by f_kelly descending
```

---

## 2. Core Assumptions

```text
p = limit_price
q = q_used = target token win probability lower bound
a = a_win_LCB = Wilson lower bound of P(fill | win)
a_lose = P(fill | lose) = 1
R = (1 - p) / p
```

`a_lose = 1` is fixed by design: if the selected side loses, assume the order fills and loses the full order budget.

Do not use fixed global `q = 0.70` for production ranking.

Default conservative q:

```text
q_market = win_market_count / (win_market_count + lose_market_count)
q_market_LCB = Wilson lower bound of q_market
q_used = q_market_LCB
```

If model calibration is available:

```text
q_used = q_model_lower_bound
```

only when:

```text
q_model_lower_bound > q_market_LCB + min_alpha_margin
```

---

## 3. Target-Token Coordinate System

Main fill table may merge Up and Down, but all fields must be converted into target-token view:

```text
target_token = token selected by model
target_current_price = price of target_token at decision time
target_win = 1 if target_token resolves to 1 else 0
```

The following must use the same target-token coordinate system:

```text
current_price_bucket
limit_price_anchor
fill proxy
win/lose label
```

---

## 4. Valid Maker Candidate Rule

A candidate is valid only if:

```text
limit_price_anchor < target_current_price_at_decision
```

Runtime must enforce:

```text
limit_price_anchor < current_price
```

If only buckets are available offline, use:

```text
limit_price_anchor < current_price_bucket_start
```

Rows violating this are invalid crossing-order artifacts and must not be used, even if `a_win_market_fill = 1`.

---

## 5. Fill Table Design

### Context keys

```text
decision_time_regime
current_price_bucket
```

### Action key

```text
limit_price_anchor
```

### Removed dimensions

```text
prediction_side
submit_second_anchor
order_delay_seconds
```

### Buckets

```text
decision_time_regime: 30s buckets from [0,30) ... [270,300)
current_price_bucket: 0.05 buckets from [0.00,0.05) ... [0.95,1.00]
limit_price_anchor: 0.05, 0.10, ..., 0.95
```

---

## 6. Fill Definition

For each historical decision snapshot and candidate limit price:

```text
fill_j = 1
```

if from decision time to market end there is a taker SELL in the target token with:

```text
trade_price <= limit_price_anchor
```

Otherwise:

```text
fill_j = 0
```

This is a touch/fill proxy, not an order-size-aware queue simulation.

---

## 7. Required Metrics

For each row:

```text
win_market_count
win_fill_market_count
a_win_market_fill
q_market_LCB
a_win_LCB
lose_market_count
lose_fill_market_count
a_lose_market_fill
q_market
q_used_default
kelly_a_win_used
q_required
q_margin
R_payoff
edge_market_q
f_kelly_raw
f_kelly
fallback_level
is_reliable
is_valid_maker_candidate
is_positive_ev
```

Optional diagnostics:

```text
kelly_growth
invalid_maker_candidate_rows.csv
maker_fill_side_diagnostics.csv
```

---

## 8. Kelly Formula

Outcome distribution per 1 USDC order budget:

| Outcome | Probability | Return |
|---|---:|---:|
| target wins and order fills | `q * a` | `+R` |
| target wins but order does not fill | `q * (1-a)` | `0` |
| target loses | `1-q` | `-1` |

Kelly objective:

```text
G(f) = q*a*log(1 + f*R) + (1-q)*log(1-f)
```

Production Kelly uses Wilson lower bounds for small-sample adjustment:

```text
q = q_market_LCB
a = a_win_LCB
```

Optimal Kelly fraction:

```text
f_kelly_raw = [q*a*R - (1-q)] / [R*(q*a + 1-q)]
f_kelly = max(0, f_kelly_raw)
```

Equivalent price form:

```text
f_kelly_raw =
[q*a*(1-p) - (1-q)*p]
/
[(1-p)*(q*a + 1-q)]
```


---

## 9. Positive EV and Safety Margin

Raw edge per 1 USDC order budget:

```text
edge = q*a*R - (1-q)
```

Positive EV condition:

```text
edge > 0
```

Required q:

```text
q_required = p / [p + a*(1-p)]
```

Safety margin:

```text
q_margin = q_used - q_required
```



---

## 10. Final Ranking Rule

Tradeable candidate filters:

```text
is_valid_maker_candidate = true
is_reliable = true
fallback_level in allowed_fallback_levels
limit_price_anchor < current_price
win_market_count+lose_market_count >= min_market_count
```
min_market_count=20

Final ranking:

```text
sort by f_kelly descending
```

Do not rank by raw edge, because raw edge over-ranks low-price orders via the `1/p` payoff term.

---

## 11. Fallback Rules

Fallback only relaxes context dimensions:

```text
level_0: decision_time_regime_30s + current_price_bucket_0.05
level_1: decision_time_regime_60s + current_price_bucket_0.05
level_2: decision_time_regime_30s + current_price_bucket_0.10
level_3: decision_time_regime_60s + current_price_bucket_0.10
level_4: current_price_bucket_0.10 only
level_5: global
```

Live default:

```text
allowed_fallback_levels = {level_0, level_1}
```

`level_4` and `level_5` are paper-only unless explicitly enabled.

All fallback rows must still satisfy:

```text
is_valid_maker_candidate = true
```

---

## 12. Runtime Logic

1. Model selects side: `up` or `down`.
2. Get target-token `current_price`,or mid price or best ask.
3. Map to `decision_time_regime` and `current_price_bucket`.
4. Query all candidate `limit_price_anchor` rows.
5. Apply tradeable filters.
6. Sort by `f_kelly` descending.
7. Allocate budget:

```text
raw_order_budget = bankroll * fractional_kelly * f_kelly
order_budget = min(raw_order_budget, single_order_budget_cap)
shares = order_budget / limit_price_anchor
sharesmust be integer
```

8. Keep only if:

```text
shares >= min_shares
order_budget <= min_order_budget
```

9. Submit maker limit buy immediately.
10. Hold until market end or cancellation/rejection.

---

## 13. Budget Constraints

Default:

```text
total_budget <= 10 USDC
single_order_budget <= 4 USDC
min_shares >= 5
fractional_kelly = 0.25
```

Minimum order budget:

```text
min_budget = 5 * limit_price_anchor
```

If multiple price levels pass filters:

```text
1. sort by f_kelly descending
2. allocate by fractional Kelly
3. cap each order by single_order_budget
4. stop when total_budget is exhausted
```

Unused budget should remain unused unless the next order has positive Kelly-adjusted value.

---

## 14. Recommended Live Filters

Initial live filters:

```text
fallback_level in {level_0, level_1}
is_valid_maker_candidate = true
is_reliable = true
limit_price_anchor < current_price
win_market_count + lose_market_count >= 20
q_margin >= 0.02
f_kelly >= 0.005
fractional_kelly = 0.25
```

Optional conservative filters:

```text
current_price_bucket between 0.40 and 0.80
q_market between 0.35 and 0.85
a_win_market_fill >= 0.03
q_used * a_win_market_fill >= 0.01
```

---

## 15. Audit Fields

Log every paper/live decision:

```text
utc_timestamp
market_condition_id
market_start_time
side
target_token_id
current_price
decision_second
decision_time_regime
current_price_bucket
q_market
q_model
q_used
q_required
q_margin
a_win_market_fill
a_lose_assumption
R_payoff
edge
f_kelly_raw
f_kelly
fractional_kelly
candidate_limit_prices
selected_limit_prices
selected_budget
selected_shares
fallback_level
win_market_count
lose_market_count
is_reliable
is_valid_maker_candidate
rejection_reason
paper_or_live
polymarket_response_status
```

Do not log:

```text
private keys
CLOB secrets
passphrases
full wallet addresses
```

---

## 16. Final Acceptance Criteria

The strategy is valid when:

```text
1. q_used defaults to q_market_LCB, not fixed 0.70
2. all candidates are valid maker orders: limit_price < current_price
3. invalid crossing-order rows are excluded
4. candidate ranking uses f_kelly, not raw edge
5. runtime submits immediately and holds to market end
6. a_lose remains fixed at 1
```

# Polymarket BTC 5m Maker Limit Buy Fill Surface Method

本文档定义 `fortune_bot` 在 Polymarket BTC 5 分钟 Up/Down 市场中，如何离线估计 maker limit buy 的成交概率，并在运行时选择正 EV 的挂单计划。

核心目标不是简单判断：

```text
q > p
```

而是判断：

```text
q * P(fill | win, context, action) * win_payout
>
P(lose) * P(fill | lose, context, action) * loss
```

在本文档的保守设定下：

```text
P(fill | lose) = 1
```

所以策略只选择：

```text
edge > 0
```

的 maker limit buy 候选订单。

---

## 1. Strategy Scope

本策略只做：

```text
maker limit buy
```

不做：

```text
market order
taker buy
taker sell
manual chasing
```

策略适用市场：

```text
Polymarket BTC 5 minute Up/Down market
```

每个市场有两个 outcome token：

```text
Up token
Down token
```

模型先决定方向：

```text
prediction_side in {up, down}
```

然后只考虑买入预测方向对应的 token。

---

## 2. Key Idea

maker limit buy 的问题不是单纯价格是否便宜。

真实问题是：

```text
预测对的时候，低价单可能买不到；
预测错的时候，低价单反而很容易成交。
```

所以需要估计：

```text
a_win = P(fill | win, context, action)
```

并保守假设：

```text
b = P(fill | lose, context, action) = 1
```

因此每个候选订单的 per-1-USDC expected return 为：

```text
edge = q * a_win * (1 / p - 1) - (1 - q) * b
```

由于：

```text
b = 1
```

所以：

```text
edge = q * a_win * (1 / p - 1) - (1 - q)
```

只有：

```text
edge > 0
```

的订单才允许进入预算分配。

---

## 3. Context and Action Design

fill table 不应该把所有字段都切成很细的 bucket。

更实际的设计是：

```text
context = 市场状态
 action = 策略动作
```

其中：

```text
context:
  prediction_side        必须保留
  decision_time_regime   粗 bin，但覆盖全市场
  current_price_bucket   粗 bin，但覆盖全市场

action:
  submit_second_anchor   固定候选时间点
  limit_price_anchor     固定候选价格点
```

---

## 4. Context Columns

### 4.1 prediction_side

字段：

```text
prediction_side in {up, down}
```

这个字段必须保留。

原因：

1. Up 和 Down token 的流动性可能不同；
2. 不同方向的市场参与行为可能不同；
3. BTC 5m 市场中 Up/Down 在不同时间段的价格路径可能不完全对称；
4. 运行时模型明确选择了方向，所以离线统计也必须保留方向。

对于 Up signal：

```text
predicted_token = Up token
```

对于 Down signal：

```text
predicted_token = Down token
```

所有 fill 统计都基于 predicted token。

---

### 4.2 decision_time_regime

字段：

```text
decision_time_regime
```

含义：

```text
模型在市场开始后的第几秒做出交易决策
```

该字段使用 30 秒步长，覆盖整个 5 分钟市场。

推荐定义：

```text
[0, 30)
[30, 60)
[60, 90)
[90, 120)
[120, 150)
[150, 180)
[180, 210)
[210, 240)
[240, 270)
[270, 300]
```

示例：

```text
decision_second = 124
=> decision_time_regime = [120, 150)
```

为什么需要这个字段：

同样的当前价格，在不同市场时间出现，含义不同。

例如：

```text
第 30 秒 token price = 0.60
第 240 秒 token price = 0.60
```

这两个状态不等价。

第 240 秒离结算更近，价格包含更多信息，价格波动和成交行为也更极端。

尤其在 BTC 5m 市场里，你已经观察到：

```text
前两分钟 win token 很少出现 0.5 以下成交价格；
最后两分钟价格波动明显更剧烈。
```

所以必须显式建模 decision time。

---

### 4.3 current_price_bucket

字段：

```text
current_price_bucket
```

含义：

```text
模型做出决策时，predicted token 的当前市场价格区间
```

该字段使用 0.05 步长，覆盖全价格区间。

推荐定义：

```text
[0.00, 0.05)
[0.05, 0.10)
[0.10, 0.15)
[0.15, 0.20)
[0.20, 0.25)
[0.25, 0.30)
[0.30, 0.35)
[0.35, 0.40)
[0.40, 0.45)
[0.45, 0.50)
[0.50, 0.55)
[0.55, 0.60)
[0.60, 0.65)
[0.65, 0.70)
[0.70, 0.75)
[0.75, 0.80)
[0.80, 0.85)
[0.85, 0.90)
[0.90, 0.95)
[0.95, 1.00]
```

示例：

```text
current_price = 0.623
=> current_price_bucket = [0.60, 0.65)
```

为什么需要这个字段：

同样挂单价格，在不同当前价格下含义不同。

例如：

```text
current_price = 0.58, limit_price = 0.50
```

这是一个距离市场价不远的 bid。

但：

```text
current_price = 0.78, limit_price = 0.50
```

这是一个很深的低价 bid。

两者的 `P(fill | win)` 完全不同。

---

## 5. Action Grid

动作维度不应该使用普通 bucket，而应该使用固定候选点。

原因：

```text
runtime 最后必须下一个明确时间和明确价格的订单。
```

所以 action 是可执行网格：

```text
submit_second_anchor × limit_price_anchor
```

---

### 5.1 submit_second_anchor

字段：

```text
submit_second_anchor
```

含义：

```text
计划在市场开始后的第几秒提交 maker limit buy
```

使用 30 秒步长，覆盖整个市场。

推荐候选值：

```text
0
30
60
90
120
150
180
210
240
270
```

一般不建议使用 300 秒，因为临近 resolve 时，订单提交和撮合存在不稳定性。

如果运行时决策发生在 `decision_second`，则只允许：

```text
submit_second_anchor >= decision_second
```

或者更严格地：

```text
submit_second_anchor >= ceil_to_next_anchor(decision_second)
```

示例：

```text
decision_second = 124
valid submit_second_anchor = 150, 180, 210, 240, 270
```

为什么用固定点而不是 bucket：

因为 runtime 的动作必须是明确的：

```text
在 150 秒挂单
在 180 秒挂单
在 210 秒挂单
```

而不是：

```text
在 150-180 秒之间随便某个时间挂单
```

---

### 5.2 limit_price_anchor

字段：

```text
limit_price_anchor
```

含义：

```text
计划提交的 maker limit buy 价格
```

使用 0.05 步长，覆盖全价格区间。

推荐候选值：

```text
0.05
0.10
0.15
0.20
0.25
0.30
0.35
0.40
0.45
0.50
0.55
0.60
0.65
0.70
0.75
0.80
0.85
0.90
0.95
```

一般不建议使用：

```text
0.00
1.00
```

因为：

```text
0.00 不可成交或无意义；
1.00 没有收益空间。
```

运行时还需要根据 Polymarket tick size 做最终价格修正：

```text
rounded_price = floor_to_tick(limit_price_anchor)
```

并确保：

```text
min_price <= rounded_price <= max_price
```

---

## 6. Fill Definition

对于每个历史市场、每个 context、每个 action，定义反事实订单：

```text
在 submit_second_anchor 时刻，对 predicted token 挂 limit_price_anchor 的 maker limit buy，订单一直有效到市场结束。
```

成交 proxy：

```text
如果 submit_second_anchor 之后，predicted token 出现 taker SELL price <= limit_price_anchor，
则认为该 maker buy 可以成交。
```

即：

```text
fill = 1 if exists taker_sell_price <= limit_price_anchor after submit_second_anchor
fill = 0 otherwise
```

这里使用 taker SELL 是因为：

```text
maker buy 会被主动卖出方成交。
```

如果别人主动卖 predicted token，并且成交价低于或等于我们的 bid，则说明我们的被动买单理论上可能被打到。

---

## 7. Win and Lose Split

每个市场最终只有一个 winning token。

对于 predicted token：

```text
win = 1 if predicted token is final winner
win = 0 otherwise
```

离线统计至少需要分别记录：

```text
win_market_count
win_fill_market_count
a_win_market_fill

lose_market_count
lose_fill_market_count
a_lose_market_fill
```

其中：

```text
a_win_market_fill = win_fill_market_count / win_market_count
```

```text
a_lose_market_fill = lose_fill_market_count / lose_market_count
```

但是运行时 edge 计算采用保守假设：

```text
b = P(fill | lose) = 1
```

也就是说，即使历史估计得到：

```text
a_lose_market_fill < 1
```

主策略仍然使用：

```text
b = 1
```

历史 `a_lose_market_fill` 只用于 audit、diagnostic 和后续研究。

---

## 8. Fill Surface Table Schema

推荐离线输出表名：

```text
btc5m_maker_fill_surface
```

每一行对应：

```text
context + action
```

即：

```text
prediction_side
+ decision_time_regime
+ current_price_bucket
+ submit_second_anchor
+ limit_price_anchor
```

推荐 schema：

```text
prediction_side              STRING   -- up / down

decision_time_bucket_start   INT      -- e.g. 120
decision_time_bucket_end     INT      -- e.g. 150
decision_time_regime         STRING   -- e.g. [120,150)

current_price_bucket_start   DOUBLE   -- e.g. 0.60
current_price_bucket_end     DOUBLE   -- e.g. 0.65
current_price_bucket         STRING   -- e.g. [0.60,0.65)

submit_second_anchor         INT      -- e.g. 180
limit_price_anchor           DOUBLE   -- e.g. 0.55

win_market_count             INT
win_fill_market_count        INT
a_win_market_fill            DOUBLE

lose_market_count            INT
lose_fill_market_count       INT
a_lose_market_fill           DOUBLE

sample_market_count          INT      -- win_market_count + lose_market_count
fallback_level               STRING
is_reliable                  BOOLEAN

created_at_utc               TIMESTAMP
data_start_utc               TIMESTAMP
data_end_utc                 TIMESTAMP
```

---

## 9. Recommended Grid Size

### Context grid

```text
prediction_side: 2 values

decision_time_regime: 10 buckets
current_price_bucket: 20 buckets
```

Context combinations:

```text
2 × 10 × 20 = 400
```

### Action grid

```text
submit_second_anchor: 10 values
limit_price_anchor: 19 values
```

Action combinations:

```text
10 × 19 = 190
```

Total possible rows:

```text
400 × 190 = 76,000 rows
```

这个规模是可以接受的。

更重要的是，动作网格是反事实生成的，不依赖历史真实订单刚好出现在这些点上。

真正决定样本量的是：

```text
prediction_side × decision_time_regime × current_price_bucket
```

而不是 action grid 本身。

---

## 10. Offline Construction Logic

### Step 1: Build historical market universe

每个市场需要包含：

```text
condition_id
market_start_ts
market_end_ts
up_token_id
down_token_id
final_winner
```

---

### Step 2: Build decision snapshots

对每个市场、每个 prediction_side、每个 decision_time_regime，生成一个 decision snapshot。

示例：

```text
market_id = X
prediction_side = up
decision_second = 124
current_price = 0.623
```

映射到：

```text
decision_time_regime = [120,150)
current_price_bucket = [0.60,0.65)
```

注意：

如果某个时间点没有精确价格，可以使用该时间点之前最近一笔有效价格，或者 mid price / last traded price。

需要在 artifact 中记录使用的 price source：

```text
current_price_source in {last_trade, best_bid_ask_mid, last_valid_trade, interpolated}
```

---

### Step 3: Generate action grid

对每个 decision snapshot，枚举：

```text
submit_second_anchor in {0,30,60,90,120,150,180,210,240,270}
limit_price_anchor in {0.05,0.10,...,0.95}
```

然后过滤：

```text
submit_second_anchor >= ceil_to_next_anchor(decision_second)
```

避免生成不可能执行的过去订单。

---

### Step 4: Compute counterfactual fill

对每个 action，检查：

```text
是否存在 predicted token 的 taker SELL，满足：
trade_second >= submit_second_anchor
trade_price <= limit_price_anchor
```

如果存在：

```text
fill = 1
```

否则：

```text
fill = 0
```

---

### Step 5: Aggregate by context + action

group by：

```text
prediction_side
decision_time_regime
current_price_bucket
submit_second_anchor
limit_price_anchor
win
```

得到：

```text
market_count
fill_market_count
fill_rate
```

再 pivot 成：

```text
win_market_count
win_fill_market_count
a_win_market_fill
lose_market_count
lose_fill_market_count
a_lose_market_fill
```

---

## 11. Reliability and Fallback

由于 context grid 覆盖全市场，有些格子样本会很少。

因此必须有 fallback。

### 11.1 Reliability threshold

建议以 market 数作为样本量，而不是 trade 数。

原因：

```text
策略关心的是一个 5m market 是否触达某个 action，
不是某个 market 内有多少笔 trade。
```

推荐门槛：

```text
win_market_count >= 30    minimum usable
win_market_count >= 50    preferred
win_market_count >= 100   strong
```

第一版建议：

```text
min_win_market_count = 30
```

如果低于 30，则 fallback。

---

### 11.2 Fallback order

因为 `prediction_side` 必须保留，所以 fallback 不合并 side。

推荐 fallback 顺序：

```text
Level 0:
  prediction_side
  + decision_time_regime 30s
  + current_price_bucket 0.05

Level 1:
  prediction_side
  + decision_time_regime 60s
  + current_price_bucket 0.05

Level 2:
  prediction_side
  + decision_time_regime 30s
  + current_price_bucket 0.10

Level 3:
  prediction_side
  + decision_time_regime 60s
  + current_price_bucket 0.10

Level 4:
  prediction_side
  + current_price_bucket 0.10

Level 5:
  prediction_side
  + decision_time_regime 60s

Level 6:
  prediction_side global
```

解释：

- 优先保留 `prediction_side`；
- 尽量保留时间特征，因为 BTC 5m 市场有明显时间结构；
- 尽量保留当前价格状态，因为 limit price 的意义依赖 current price；
- 动作维度 `submit_second_anchor` 和 `limit_price_anchor` 不优先合并，因为它们是最终要优化的二维平面。

最终每行必须记录：

```text
fallback_level
```

示例：

```text
L0_side_time30_price005
L1_side_time60_price005
L2_side_time30_price010
L3_side_time60_price010
L4_side_price010
L5_side_time60
L6_side_global
```

---

## 12. Monotonicity Checks

fill surface 应该满足一些基本单调性。

### 12.1 Price monotonicity

同一个 context、同一个 submit_second_anchor 下：

```text
limit_price 越高，fill 概率不应该更低。
```

即：

```text
a_win(t, p=0.55) >= a_win(t, p=0.50)
```

如果历史估计违反该关系，通常是样本噪声。

可以做：

```text
price direction cumulative max smoothing
```

---

### 12.2 Time monotonicity

如果定义为：

```text
submit 后订单一直挂到市场结束
```

那么同一个 context、同一个 limit_price_anchor 下：

```text
submit 越早，fill 概率不应该更低。
```

即：

```text
a_win(submit=120, p) >= a_win(submit=150, p) >= a_win(submit=180, p)
```

注意：

这不等于说早挂一定更赚钱。

早挂只是更容易成交。

最终是否值得挂，还要看：

```text
edge = q * a_win * (1 / p - 1) - (1 - q)
```

以及预算分配结果。

---

## 13. Runtime Inputs

运行时，每个市场需要得到：

```text
market_id
condition_id
market_start_ts
current_second
prediction_side
p_up
p_down
q
q_source
current_predicted_token_price
```

其中：

```text
if prediction_side == up:
    q = p_up
else:
    q = p_down = 1 - p_up
```

但 q 不应该硬编码。

推荐优先级：

```text
1. calibrated real-time p_side
2. validation bucket accepted accuracy
3. manifest overall accepted_sample_accuracy
```

---

## 14. Runtime Bucket Mapping

运行时将当前状态映射到 context：

```text
prediction_side
current_second -> decision_time_regime
current_predicted_token_price -> current_price_bucket
```

示例：

```text
prediction_side = up
current_second = 124
current_price = 0.623
```

映射为：

```text
prediction_side = up
decision_time_regime = [120,150)
current_price_bucket = [0.60,0.65)
```

然后从 fill surface 中读取该 context 对应的所有 action：

```text
submit_second_anchor >= 150
limit_price_anchor in {0.05,0.10,...,0.95}
```

---

## 15. Runtime Edge Calculation

对每个 candidate action：

```text
a_win = fill_surface.a_win_market_fill
p = limit_price_anchor
b = 1
```

计算：

```text
edge = q * a_win * (1 / p - 1) - (1 - q) * b
```

即：

```text
edge = q * a_win * (1 / p - 1) - (1 - q)
```

只保留：

```text
edge > 0
is_reliable = true
submit_second_anchor >= next_valid_submit_time
```

---

## 16. Required Fill Probability Threshold

也可以反推出某个价格下需要的最低 `a_win`：

```text
edge > 0
```

等价于：

```text
q * a_win * (1 / p - 1) > 1 - q
```

所以：

```text
a_win > (1 - q) / [q * (1 / p - 1)]
```

这个阈值非常重要。

示例：

```text
q = 0.70
p = 0.55
```

则：

```text
1 / p - 1 = 0.8182
```

```text
a_win > 0.30 / (0.70 * 0.8182)
a_win > 0.5238
```

也就是说：

```text
如果 q=0.70，limit price=0.55，
那么 win 时成交概率必须超过 52.38%，这个订单才是正 EV。
```

---

## 17. Budget Constraints

运行时预算约束：

```text
sum(B_j) <= 7 USDC
B_j <= 4 USDC
B_j / p_j >= 5 shares
edge_j > 0
```

其中最小订单预算：

```text
min_B_j = 5 * p_j
```

例如：

```text
p_j = 0.55
min_B_j = 5 * 0.55 = 2.75 USDC
```

如果：

```text
min_B_j > 4 USDC
```

则该订单不可选。

---

## 18. Budget Allocation

推荐 planner：

1. 读取当前 context 下所有 candidate action；
2. 计算每个 action 的 edge；
3. 过滤：

```text
edge > 0
is_reliable = true
submit_second_anchor valid
limit_price valid
```

4. 枚举 1 到 3 个订单组合；
5. 每个订单先分配最小预算：

```text
min_B_j = 5 * p_j
```

6. 剩余预算分给 edge 更高的订单；
7. 每个订单预算不超过 4 USDC；
8. 总预算不超过 7 USDC；
9. 选择最大化：

```text
sum(B_j * edge_j)
```

的组合。

注意：

```text
planner 不需要花完全部预算。
```

只有当额外预算仍然有正 EV 时，才继续分配。

---

## 19. Interpreting the Fill Surface

对每个 context，应该生成两张二维图。

### 19.1 a_win surface

横轴：

```text
submit_second_anchor
```

纵轴：

```text
limit_price_anchor
```

数值：

```text
a_win_market_fill
```

这张图回答：

```text
如果最终预测方向赢，
我在某个时间、某个价格挂单，历史上有多大概率成交？
```

---

### 19.2 edge surface

横轴：

```text
submit_second_anchor
```

纵轴：

```text
limit_price_anchor
```

数值：

```text
edge
```

这张图回答：

```text
在当前 q 下，哪些时间-价格点是正 EV？
```

真正可交易区域是：

```text
edge > 0
```

并且：

```text
is_reliable = true
```

---

## 20. Practical Interpretation

根据你目前的观察，fill surface 很可能呈现以下结构。

### 20.1 前两分钟

```text
submit_second_anchor <= 120
limit_price_anchor < 0.50
```

可能出现：

```text
a_win 很低
```

原因：

```text
win token 在前两分钟很少有 0.5 以下成交价格。
```

这类订单看起来赔率好，但实际问题是：

```text
预测对时买不到，预测错时买得到。
```

所以 edge 可能为负。

---

### 20.2 后两分钟

```text
submit_second_anchor >= 180
```

价格波动更剧烈，`a_win` 可能明显上升。

但同时逆向选择也更强。

因此不能只看成交概率，而必须看：

```text
edge = q * a_win * (1 / p - 1) - (1 - q)
```

---

### 20.3 核心可交易区域

实际更可能有价值的区域是：

```text
submit_second_anchor in {180, 210, 240}
limit_price_anchor in {0.45, 0.50, 0.55, 0.60}
```

但这不应该硬编码。

最终由 fill surface + edge surface 自动决定。

---

## 21. Runtime Safety Rules

运行时必须检查：

```text
manifest.live_eligible = true
config.enable_live_orders = true
```

否则只能 paper mode。

每个订单必须检查：

```text
maker only
price rounded to tick size
price within min/max bounds
shares >= 5
budget <= 4 USDC
total budget <= 7 USDC
no duplicate order key
```

订单唯一键：

```text
market_condition_id
prediction_side
submit_second_anchor
limit_price_anchor
```

如果同一个 key 已经提交过，则不重复提交。

---

## 22. Runtime Audit Fields

每次 paper/live summary 必须记录：

```text
utc_timestamp
model_version
artifact_hash
manifest_live_eligible
market_condition_id
market_slug
market_start_ts
current_second
prediction_side
p_up
p_down
q
q_source
current_predicted_token_price
decision_time_regime
current_price_bucket
fallback_level
candidate_actions
candidate_a_win
candidate_edges
selected_orders
selected_prices
selected_submit_seconds
selected_budgets
selected_shares
selected_expected_value
paper_or_live_mode
polymarket_response_status
```

禁止写入日志或 artifact：

```text
private key
CLOB secret
CLOB passphrase
full deposit wallet address
full API key
```

地址如需记录，只允许记录 masked version：

```text
0x1234...abcd
```

---

## 23. Recommended Output Artifacts

离线 pipeline 推荐输出：

```text
fill_surface.parquet
fill_surface_summary.csv
fill_surface_metadata.json
win_fill_heatmaps/
edge_heatmaps/
```

其中 metadata 至少包含：

```json
{
  "market_type": "BTC 5m Up/Down",
  "decision_time_step_seconds": 30,
  "current_price_bucket_size": 0.05,
  "submit_second_step_seconds": 30,
  "limit_price_step": 0.05,
  "min_win_market_count": 30,
  "fill_proxy": "taker_sell_price_lte_limit_price_after_submit_second",
  "lose_fill_assumption_for_runtime": 1.0,
  "order_valid_until": "market_end",
  "created_at_utc": "...",
  "data_start_utc": "...",
  "data_end_utc": "..."
}
```

---

## 24. Final Method Summary

最终方法可以总结为：

```text
1. 用 prediction_side + decision_time_regime + current_price_bucket 定义市场状态；
2. 用 submit_second_anchor + limit_price_anchor 定义可执行动作；
3. 对每个 context/action，用历史 taker SELL 反事实估计 a_win；
4. 保守设定 lose 时 b=1；
5. 用 edge = q * a_win * (1/p - 1) - (1-q) 计算每个动作的 EV；
6. 只保留 edge > 0 且样本可靠的动作；
7. 在预算、最小 shares、单订单上限约束下，选择最大化 sum(B_j * edge_j) 的订单组合；
8. 不强制花完预算；
9. 所有下单和不下单都必须记录 audit 信息。
```

这套方法的本质是：

```text
在 BTC 5m 市场的 时间 × 价格 二维平面上，
找到“预测对时有足够概率成交、预测错时保守仍然正 EV”的 maker buy 区域。
```

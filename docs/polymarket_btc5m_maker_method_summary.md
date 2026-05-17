# Polymarket BTC 5m Maker Limit Buy 方法说明

本文说明 `fortune_bot` 在 Polymarket BTC 5 分钟 Up/Down 市场中的
maker limit buy 策略。策略只生成被动限价买单计划；模型给出方向信号后，
仍必须经过概率、填单率、edge、预算和幂等约束过滤。

## 决策输入

模型先决定交易方向：

```text
side in {up, down}
```

方向胜率估计记为 `q`。`q` 不能固定写死为某个常数，运行时按以下优先级取值：

1. 如果当前模型 artifact 带有校准器，使用实时 `p_side`。
2. 否则使用 `probability_reference.json` 中匹配概率 bucket 的实测 accepted accuracy。
3. 如果 bucket 无法匹配，再使用 manifest 里的整体 `accepted_sample_accuracy` 作为保守 fallback。

当方向为 Up 时，`p_side = p_up`；当方向为 Down 时，
`p_side = 1 - p_up`。

## 候选订单

一个候选 maker 订单记为：

```text
j = (t_j, p_j)
```

其中：

- `t_j` 是从市场开始后第几秒挂单。
- `p_j` 是按 tick size 向下取整后的限价买入价格。
- `B_j` 是分配给该候选订单的 USDC 预算。

所有运行时订单都必须是 maker limit buy。市价单、taker 单和卖单不属于本策略。

## 离线填单概率 a_j

每个候选订单需要一张离线表估计：

```text
a_j = P(fill_j | win, context)
```

这里的 `fill_j` 表示“如果我们在 `t_j` 秒以 `p_j` 挂被动买单，历史成交流是否显示该单有机会成交”。当前实现使用历史 taker `SELL` 成交作为 proxy：如果预测 token 在挂单时间之后出现过成交价小于或等于我方 bid 的 sell 成交，则认为这个市场里被动买单可能成交。

`maker_fill_table.csv` 至少包含这些维度：

- `prediction_side`
- `decision_second_bucket`
- `current_price_bucket`
- `order_delay_seconds`
- `limit_price`

并记录这些指标：

- `win_market_count`
- `win_fill_market_count`
- `a_win_market_fill`
- `lose_market_count`
- `lose_fill_market_count`
- `a_lose_market_fill`
- `fallback_level`

运行时 edge 计算使用 `a_win_market_fill` 作为 `a_j`。该字段必须等于：

```text
win_fill_market_count / win_market_count
```

如果精细 bucket 样本数不足，表会回退到更粗粒度的 side/delay/price 统计，
并在 `fallback_level` 中标记，例如 `side_delay_price`。运行日志需要记录使用到的 bucket 和 fallback level，便于复盘。

## Edge 公式

错误方向下采用保守填单假设：

```text
b = P(fill | lose) = 1
```

单位资金期望收益为：

```text
edge_j = q * a_j * (1 / p_j - 1) - (1 - q) * b
```

只有满足：

```text
edge_j > 0
```

的候选订单才允许进入预算分配。

## 订单约束

每个被选中的订单必须满足：

```text
sum(B_j) <= 7 USDC
B_j <= 4 USDC
B_j / p_j >= 5 shares
edge_j > 0
```

运行时还必须执行以下约束：

- 价格按市场 tick size 向下取整。
- 价格在 Polymarket 允许的 min/max 范围内。
- 同一 `(market, side, order_delay_seconds, limit_price)` order key 不能重复提交。
- live 模式必须同时满足 `orders.enabled=true` 和 `artifact_manifest.live_eligible=true`。

## 预算分配

planner 的步骤：

1. 对全部候选订单计算 `edge_j`。
2. 只保留正 EV 候选。
3. 枚举 1 到 3 个订单的组合。
4. 每单先分配最低预算 `min_B_j = 5 * p_j`。
5. 剩余预算按 `edge_j` 从高到低补到单笔上限 `B_j <= 4`。
6. 选择 `sum(B_j * edge_j)` 最大的组合。

如果正 EV 订单不足，planner 不会为了花满 7 USDC 而强行下单。

## 审计日志字段

paper/live summary 至少应记录：

- UTC timestamp
- model version 和 artifact hash
- market slug、condition id、token id
- decision window 和 feature source
- `p_up`、`p_down`、阈值和最终 decision
- `q` 和 `q_source`
- maker fill table bucket 和 fallback level
- candidate edge
- selected order price、shares、budget 和 expected value
- paper/live 模式
- Polymarket response status 或明确的 skip/rejected reason

日志、artifact 和文档中不得写入 private key、CLOB secret、CLOB passphrase、
完整 deposit wallet 地址或完整 API key。

# 当前下单规划逻辑

本文解释当前 execution engine 的下单过滤逻辑、价格确定逻辑和数量确定逻辑。代码入口主要在：

```text
execution_engine/run_once.py
execution_engine/maker_order_plan.py
config.example.yaml
```

## 1. 输入与决策入口

每次运行 `run_once` 时，流程先加载 deploy artifact：

```text
execution_engine/deploy/<model_version>/artifact_manifest.json
```

再加载：

```text
model/model.pkl
model/feature_columns.json
fill_surface/maker_fill_table.csv
```

模型输出 `p_up`，再用 manifest 里的阈值决定方向：

```text
decision = up / down / abstain
```

如果模型 abstain，则不会进入下单规划。

如果模型给出 `up` 或 `down`，runtime 取对应目标 token 的当前价格：

```text
current_price = <side>_token_last_price
```

当前实现默认 `decision_second=120`。

## 2. 当前默认下单策略

当前默认 planner 是：

```yaml
orders:
  planner: best_bid_ladder
  kelly_fallback_enabled: false
```

也就是说，默认不使用 Kelly 下单。Kelly 策略仍保留在线上 fallback 位置，但只有显式设置：

```yaml
orders:
  kelly_fallback_enabled: true
```

并且默认 best-bid ladder 没有生成订单时，才会 fallback 到 Kelly。

默认 best-bid ladder 下两个 maker limit buy 订单：

```text
order 1 price = min(best_bid, 0.80)
order 1 shares = 5

order 2 price = min(best_bid, 0.80) - 0.10
order 2 shares = 5
```

默认参数：

```yaml
best_bid_ladder_max_price: 0.80
best_bid_ladder_second_offset: 0.10
best_bid_ladder_shares: 5.0
tick_size: 0.01
min_price: 0.01
```

价格会按 `tick_size` 向下取整。如果第二档价格低于 `min_price`，该档会被跳过。

## 3. best bid 来源

live 模式下，runtime 用目标 side 的 token id 查询 Polymarket CLOB order book，取 bids 里的最高价格：

```text
best_bid = max(order_book.bids.price)
```

paper / validation snapshot 模式下，如果特征行里没有 best bid 字段，当前用对应 side 的 last price 作为测试用 best bid 近似值，避免 paper 自测依赖实时 order book。

支持的 best bid 字段名：

```text
<side>_token_best_bid
<side>_best_bid
_<side>_token_best_bid
```

其中 `<side>` 是 `up` 或 `down`。

## 4. 默认策略的过滤逻辑

默认 best-bid ladder 的过滤很少：

```text
best_bid 必须存在
price >= min_price
shares > 0
```

默认策略不查 fill table，不使用 `q_market_LCB`、`a_win_LCB`、`f_kelly` 做入场过滤。

默认策略生成的订单字段：

```text
planner = best_bid_ladder
limit_price_anchor = ladder price
shares = 5
budget_usdc = shares * limit_price_anchor
order_type = limit_buy
maker_only = true
```

## 5. 默认策略的价格确定逻辑

第一档：

```text
limit_price_anchor = floor_to_tick(min(best_bid, best_bid_ladder_max_price), tick_size)
```

第二档：

```text
limit_price_anchor = floor_to_tick(min(best_bid, best_bid_ladder_max_price) - best_bid_ladder_second_offset, tick_size)
```

当前默认等价于：

```text
price_1 = floor_to_tick(min(best_bid, 0.80), 0.01)
price_2 = floor_to_tick(min(best_bid, 0.80) - 0.10, 0.01)
```

## 6. 默认策略的数量确定逻辑

默认每档固定：

```text
shares = 5
```

实际预算：

```text
budget_usdc = shares * limit_price_anchor
```

在最高价格情况下：

```text
order 1: 5 * 0.80 = 4.00 USDC
order 2: 5 * 0.70 = 3.50 USDC
total = 7.50 USDC
```

## 7. Kelly fallback 候选行查找

planner 会把 runtime 状态映射成两个 bucket：

```text
decision_time_regime: 30s bucket，例如 [120,150)
current_price_bucket: 0.05 price bucket，例如 [0.55,0.60)
```

然后在 `maker_fill_table.csv` 里查：

```text
decision_time_regime + current_price_bucket
```

如果精确上下文有行，就使用这些行。否则只允许从配置里的 fallback level 查找：

```yaml
allowed_fallback_levels:
  - level_0
  - level_1
```

每个候选行代表一个可选的：

```text
limit_price_anchor
```

## 8. Kelly fallback 下单过滤逻辑

每个候选 `limit_price_anchor` 依次经过以下过滤。

### 3.1 价格范围过滤

先将 `limit_price_anchor` 按 tick 向下取整：

```text
price = floor_to_tick(limit_price_anchor, tick_size)
```

默认：

```yaml
tick_size: 0.01
min_price: 0.01
max_price: 0.99
max_limit_price: 0.80
```

候选价格必须满足：

```text
min_price <= price <= min(max_price, max_limit_price)
```

### 3.2 maker 非穿价过滤

候选必须是真 maker 买单，不允许 crossing order：

```text
price < current_price
```

并且 fill table 行本身也必须标记：

```text
is_valid_maker_candidate = true
```

### 3.3 样本数与可靠性过滤

样本数定义为：

```text
market_count = win_market_count + lose_market_count
```

默认要求：

```yaml
min_market_count: 20
```

所以必须满足：

```text
market_count >= 20
is_reliable = true
```

### 3.4 Kelly 输入值

当前不直接用 raw `q_market` 和 raw `a_win_market_fill` 进入 Kelly。

默认使用 Wilson 置信区间下界：

```text
q_used = q_market_LCB
a_win_used = a_win_LCB
```

如果未来 artifact 有 calibrated model lower bound，并且：

```text
q_model_lower_bound > q_market_LCB + min_alpha_margin
```

才会用 model lower bound 替代 `q_market_LCB`。

当前配置默认：

```yaml
min_alpha_margin: 0.02
```

### 3.5 Kelly/EV 过滤

用以下参数计算：

```text
p = limit_price_anchor
R_payoff = (1 - p) / p
edge = q_used * a_win_used * R_payoff - (1 - q_used)
q_required = p / [p + a_win_used * (1 - p)]
q_margin = q_used - q_required
f_kelly_raw = edge / [R_payoff * (q_used * a_win_used + 1 - q_used)]
f_kelly = max(0, f_kelly_raw)
```

候选必须满足：

```text
q_margin >= min_q_margin
f_kelly >= min_f_kelly
edge > 0
```

默认：

```yaml
min_q_margin: 0.02
min_f_kelly: 0.005
```

### 3.6 最小订单预算过滤

最小 shares 默认：

```yaml
min_shares: 5
```

因此候选价格对应的最小预算为：

```text
min_budget = min_shares * limit_price_anchor
```

若：

```text
min_budget > max_order_budget_usdc
```

则过滤掉。

默认：

```yaml
max_order_budget_usdc: 4.0
```

## 9. Kelly fallback 价格确定逻辑

最终下单价格不是模型直接生成的价格，而是 fill table 候选行里的：

```text
limit_price_anchor
```

处理顺序是：

```text
1. 从 maker_fill_table.csv 取 limit_price_anchor
2. 按 tick_size 向下取整
3. 检查 price < current_price
4. 通过过滤后作为实际 limit buy price
```

当前代码中下单请求使用：

```text
price = selected_order["limit_price_anchor"]
```

`limit_price` 字段已删除，避免和 `limit_price_anchor` 重复。

## 10. Kelly fallback 排序逻辑

所有通过过滤的候选不会按 raw edge 排序。

当前排序规则是：

```text
f_kelly descending
```

也就是 Kelly 仓位比例越高，优先级越高。

## 11. Kelly fallback 数量与预算确定逻辑

预算分配在 `allocate_budget` 中完成。

默认总约束：

```yaml
max_total_budget_usdc: 10.0
max_order_budget_usdc: 4.0
max_orders_per_window: 3
fractional_kelly: 0.25
min_shares: 5
```

对每个已排序候选，先计算 Kelly 建议预算：

```text
raw_budget = max_total_budget_usdc * fractional_kelly * f_kelly
```

再做上限约束：

```text
capped_budget = min(raw_budget, max_order_budget_usdc, remaining_total_budget)
```

然后计算整数 shares：

```text
shares = floor(capped_budget / limit_price_anchor)
```

如果：

```text
shares < min_shares
```

则跳过该候选。

最终实际预算是：

```text
budget_usdc = shares * limit_price_anchor
```

所以实际预算可能小于 `capped_budget`，因为 shares 必须向下取整为整数。

每选中一个订单后：

```text
remaining_total_budget -= budget_usdc
```

达到以下任一条件就停止：

```text
selected_order_count >= max_orders_per_window
remaining_total_budget <= 0
没有更多可交易候选
```

## 12. 实际提交逻辑

paper 模式不会提交真实订单，只记录 selected orders。

live 模式只有在同时满足以下条件时才会提交：

```text
runtime mode = live
orders.enabled = true
artifact_manifest.live_eligible = true
selected_orders 非空
目标 side token_id 存在
```

提交订单类型是：

```text
maker limit buy
```

订单字段：

```text
token_id = selected side token id
price = limit_price_anchor
shares = integer shares
order_key = idempotency key
```

## 13. 当前默认配置摘要

```yaml
orders:
  planner: best_bid_ladder
  kelly_fallback_enabled: false
  best_bid_ladder_max_price: 0.80
  best_bid_ladder_second_offset: 0.10
  best_bid_ladder_shares: 5.0
  fractional_kelly: 0.25
  min_alpha_margin: 0.02
  min_q_margin: 0.02
  min_f_kelly: 0.005
  max_total_budget_usdc: 10.0
  max_order_budget_usdc: 4.0
  min_shares: 5.0
  max_orders_per_window: 3
  maker_only: true
  tick_size: 0.01
  min_price: 0.01
  max_price: 0.99
  max_limit_price: 0.80
  min_market_count: 20
  allowed_fallback_levels:
    - level_0
    - level_1
```

## 14. 交易秘钥配置位置

代码不会在配置文件里写死秘钥，只读取环境变量。默认变量名在 `config.example.yaml` / `config.polymarket_live.example.yaml` 的 `polymarket` 段：

```yaml
polymarket:
  private_key_env: POLYMARKET_PRIVATE_KEY
  api_key_env: CLOB_API_KEY
  api_secret_env: CLOB_SECRET
  api_passphrase_env: CLOB_PASS_PHRASE
  funder_env: DEPOSIT_WALLET_ADDRESS
```

本地直接运行时，可以在当前 shell 设置这些环境变量。

部署到 version3/systemd 时，service 读取：

```text
~/fortune_bot/execution_engine/secrets.env
```

对应仓库里的 service 配置是：

```text
deploy/fortune-bot.service
EnvironmentFile=-%h/fortune_bot/execution_engine/secrets.env
```

`secrets.env` 应该放类似下面的变量名，不要提交到 git：

```text
POLYMARKET_PRIVATE_KEY=...
CLOB_API_KEY=...
CLOB_SECRET=...
CLOB_PASS_PHRASE=...
DEPOSIT_WALLET_ADDRESS=...
```

部署脚本会尝试设置权限：

```text
chmod 600 execution_engine/secrets.env
```

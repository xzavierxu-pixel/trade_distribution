# PRD: BTC 5m Early Trade Label Prediction

## 1. Background

当前仓库已有 Polymarket BTC 5-minute Up/Down 市场的 resolved trade 数据，以及一个目标格式样例 `evaluation.json`。本项目要用每个 5 分钟市场开盘后前 2 分钟内的 trade 数据构建特征，预测该市场最终 resolve label，即 `up` 或 `down`。

本 PRD 只定义离线训练、验证、评估和产物生成流程；不要求在当前步骤实际执行训练。后续实现应按本文规划补齐脚本，并能够一键生成类似 `evaluation.json` 的 output 文件。

## 2. Goal

核心目标：

- 输入：resolved BTC 5m Polymarket trade 数据。
- 特征窗口：只允许使用 `_market_start_ts <= timestamp < _market_start_ts + 120` 的 trade 数据。
- 标签：每个 market 的 `_final_outcome` 或 refs 文件中的 `final_outcome`，二分类 `up/down`。
- 输出：`evaluation.json` 风格的模型评估文件。
- 主要优化目标：在 validation coverage `>= 0.70` 的前提下，让 validation `selection_score` 最高。

非目标：

- 不做实时交易执行。
- 不接入外部行情源作为必需输入。
- 不使用开盘 2 分钟之后的任何 trade 信息构建特征。
- 不以训练集 selection_score 作为最终选择标准。

## 3. Existing Inputs

当前仓库中相关文件：

- `out_btc5m_30d_limit_sell_resolved/trades_sell_resolved.csv`
- `out_btc5m_30d_limit_sell_resolved/market_refs_resolved.csv`
- `out_btc5m_7d_full_strategy/trades_raw.csv`
- `out_btc5m_7d_full_strategy/market_refs_resolved.csv`
- `evaluation.json`

推荐优先数据源：

1. 首选 `out_btc5m_30d_limit_sell_resolved/*`，样本天数更长，适合模型验证。
2. 若需要 buy/sell 两侧 microstructure 特征，用 `out_btc5m_7d_full_strategy/trades_raw.csv` 做补充实验，但不可与 30d sell-only 数据混在一个默认 benchmark 中。

关键 trade 字段：

- market id: `_condition_id` 或 `conditionId`
- market start: `_market_start_ts`
- trade time: `timestamp`
- traded outcome token: `_outcome_norm`，取值 `up/down`
- final label: `_final_outcome`
- side: `side`
- price: `price`
- size: `size`
- transaction id: `transactionHash`
- slug: `_slug` 或 `slug`

## 4. Output Contract

主输出文件：

- `models/early_trade_label_v1/evaluation.json`

格式应兼容现有 `evaluation.json` 的阅读习惯，至少包含：

```json
{
  "project": "btc-polymarket-early-trade-v1",
  "market": "BTC/USDT",
  "exchange": "polymarket",
  "horizon": "5m",
  "objective": "weighted_binary_selective_direction",
  "feature_window_seconds": 120,
  "feature_count": 0,
  "feature_columns": [],
  "label_column": "final_outcome",
  "prediction_column": "p_up",
  "decision_policy": {
    "coverage_constraint": 0.7,
    "selected_t_up": 0.0,
    "selected_t_down": 0.0
  },
  "train_window": {},
  "validation_window": {},
  "train_metrics": {},
  "validation_metrics": {},
  "probability_summary": {},
  "artifacts": {}
}
```

附属输出：

- `features_train.parquet`
- `features_validation.parquet`
- `predictions_train.csv`
- `predictions_validation.csv`
- `feature_importance.csv`
- `probability_deciles.csv`
- `threshold_search.csv`
- `regime_slices.csv`
- `false_up_slices.csv`
- `false_down_slices.csv`
- `probability_reference.json`

## 5. Directory Structure

新增目录建议：

```text
early_trade_label/
  __init__.py
  config.py
  schema.py
  build_dataset.py
  features.py
  train_model.py
  evaluate.py
  threshold_search.py
  report.py
  cli.py
  README.md

configs/
  early_trade_label_v1.yaml

models/
  early_trade_label_v1/
    evaluation.json
    model.pkl
    feature_columns.json
    feature_importance.csv
    probability_reference.json
    threshold_search.csv
    predictions_train.csv
    predictions_validation.csv

data_processed/
  early_trade_label_v1/
    market_dataset.parquet
    features_train.parquet
    features_validation.parquet
    dataset_quality.json
```

说明：

- `early_trade_label/` 放可复用代码。
- `configs/` 固化实验参数，保证可复现。
- `data_processed/` 放中间特征数据。
- `models/` 放最终模型和评估产物。
- 不修改现有 raw csv。

## 6. Script Requirements

### 6.1 `build_dataset.py`

职责：

- 读取 trades csv 和 refs csv。
- 统一字段命名。
- 按 market 聚合，只保留前 120 秒 trade。
- 生成 market-level 训练样本，一行一个 market。
- 合并最终 label。
- 输出 `market_dataset.parquet` 和 `dataset_quality.json`。

CLI：

```bash
python early_trade_label/build_dataset.py \
  --trades-csv out_btc5m_30d_limit_sell_resolved/trades_sell_resolved.csv \
  --refs-csv out_btc5m_30d_limit_sell_resolved/market_refs_resolved.csv \
  --outdir data_processed/early_trade_label_v1 \
  --feature-window-seconds 120
```

关键校验：

- 每个 market 只能有一个最终 label。
- 丢弃 label 缺失、时间字段无效、价格不在 `[0, 1]`、size `<= 0` 的行。
- 如果一个 market 前 2 分钟没有 trade，仍保留样本，但所有 trade 特征填 0，并增加 `has_early_trade=0`。
- `timestamp >= start_ts + 120` 的行绝不能进入特征。

### 6.2 `features.py`

职责：

- 从 market-level early trade events 生成数值特征。
- 输出稳定排序后的 feature columns。
- 对 train/validation 使用同一套 feature schema。

### 6.3 `train_model.py`

职责：

- 按时间切分 train/validation。
- 训练候选模型。
- 保存最佳模型、feature columns、train/validation prediction。

默认模型优先级：

1. `sklearn.ensemble.HistGradientBoostingClassifier`
2. `sklearn.ensemble.RandomForestClassifier`
3. 可选 `lightgbm.LGBMClassifier`，仅当环境已安装时启用
4. 可选 `catboost.CatBoostClassifier`，仅当环境已安装时启用

默认先使用 sklearn，避免新增依赖阻塞。

### 6.4 `threshold_search.py`

职责：

- 输入 validation `p_up`。
- 枚举 `selected_t_up` 和 `selected_t_down`。
- 只保留 coverage `>= 0.70` 的阈值组合。
- 在可行集合中选择 validation `selection_score` 最大的组合。

决策规则：

- 若 `p_up >= selected_t_up`，预测 `up`。
- 若 `p_up <= selected_t_down`，预测 `down`。
- 中间区域为 abstain。
- 必须满足 `selected_t_down < selected_t_up`。

当 coverage 约束不可满足时：

- 退化为覆盖率最高的阈值组合。
- `evaluation.json` 中标记 `"coverage_constraint_satisfied": false`。
- 不允许静默输出低 coverage 结果。

### 6.5 `evaluate.py`

职责：

- 计算 train/validation metrics。
- 生成 `evaluation.json`。
- 输出辅助诊断 csv。

必需 metrics：

- `sample_count`
- `coverage`
- `precision_up`
- `precision_down`
- `balanced_precision`
- `all_sample_accuracy`
- `accepted_sample_accuracy`
- `utility`
- `downside_risk`
- `selection_score`
- `share_up_predictions`
- `share_down_predictions`
- `accepted_count`
- `up_prediction_count`
- `down_prediction_count`
- `roc_auc`
- `brier_score`
- `log_loss`

建议 metric 定义：

- `coverage = accepted_count / sample_count`
- `precision_up = correct_up_predictions / up_prediction_count`
- `precision_down = correct_down_predictions / down_prediction_count`
- `balanced_precision = mean(precision_up, precision_down)`
- `accepted_sample_accuracy = correct_accepted / accepted_count`
- `all_sample_accuracy = correct_argmax_predictions / sample_count`
- `utility = accepted_sample_accuracy - baseline_accuracy`
- `downside_risk = max(1e-9, 1 - accepted_sample_accuracy)`
- `selection_score = utility / downside_risk`

`baseline_accuracy` 默认取 validation 中多数类比例；也可以在配置中固定为 `0.5`。PRD 推荐默认使用多数类比例，因为 BTC 5m up/down 在短窗口内可能轻微不平衡。

### 6.6 `report.py`

职责：

- 生成诊断文件。
- 输出概率 decile 表、错误样本切片、regime slice、feature importance。

## 7. Feature Engineering Plan

所有特征只基于前 120 秒 trade。

### 7.1 Basic Market Features

- `early_trade_count`
- `early_size_sum`
- `early_notional_sum`
- `early_avg_price_size_weighted`
- `early_first_trade_second`
- `early_last_trade_second`
- `early_active_seconds`
- `early_has_trade`
- `early_unique_wallet_count`
- `early_unique_tx_count`

### 7.2 Outcome Token Features

分别对 `up` 和 `down` token 计算：

- `{side}_trade_count`
- `{side}_size_sum`
- `{side}_notional_sum`
- `{side}_vwap`
- `{side}_first_price`
- `{side}_last_price`
- `{side}_max_price`
- `{side}_min_price`
- `{side}_price_range`
- `{side}_price_return_first_last`
- `{side}_price_slope_time`
- `{side}_size_weighted_last_30s_price`
- `{side}_large_trade_count`
- `{side}_large_trade_size_sum`

其中 `{side}` 指 token outcome：`up_token`、`down_token`，不是 taker side。

### 7.3 Pairwise Relative Features

- `up_minus_down_vwap`
- `up_div_down_vwap`
- `up_minus_down_trade_count`
- `up_share_trade_count`
- `up_share_size`
- `up_share_notional`
- `up_last_price_minus_down_last_price`
- `up_price_momentum_minus_down_price_momentum`
- `market_implied_up_prob_last`
- `market_implied_up_prob_vwap`

当只观察到单侧 token trade：

- 缺失侧价格用 neutral value `0.5` 或该侧训练集 median。
- 同时加入 missing indicator，例如 `down_token_missing_early=1`。

### 7.4 Time Bucket Features

使用固定 bucket：

- `0-15s`
- `15-30s`
- `30-60s`
- `60-90s`
- `90-120s`

每个 bucket 生成：

- trade count
- size sum
- notional sum
- up size share
- up trade share
- up/down vwap
- last price
- price change from previous bucket

### 7.5 Side Features

如果数据中存在 `BUY/SELL` 两侧：

- `buy_trade_count`
- `sell_trade_count`
- `buy_size_sum`
- `sell_size_sum`
- `buy_sell_count_imbalance`
- `buy_sell_size_imbalance`
- `up_sell_pressure`
- `down_sell_pressure`

如果使用 sell-only 30d 数据：

- 保留 schema，但对应 buy 特征填 0。
- 增加 `source_is_sell_only=1`，避免模型误读。

### 7.6 Distribution Shape Features

- price quantiles: p10, p25, p50, p75, p90
- size quantiles: p50, p90, p95
- price std
- size std
- entropy of outcome token volume share
- concentration: top 1 trade size share, top 3 trade size share
- whale indicator: max trade size / total size

### 7.7 Calendar Features

只使用 market start time，不使用未来信息：

- hour UTC
- day of week UTC
- minute of hour
- session flags: US market hours, Asia hours, Europe hours

## 8. Label Definition

每个 market label：

- `label = 1` if final outcome is `up`
- `label = 0` if final outcome is `down`

字段来源优先级：

1. refs csv `final_outcome`
2. trades csv `_final_outcome`

若同一 market 出现冲突 label：

- 该 market 从训练集中剔除。
- `dataset_quality.json` 记录冲突数量和 slug。

## 9. Time Split

默认采用时间顺序切分：

- 按 `_market_start_ts` 排序。
- 前 70% market 为 train。
- 后 30% market 为 validation。

可配置：

- `train_end_ts`
- `validation_start_ts`
- `validation_end_ts`
- `purge_minutes`

推荐保留 5 至 30 分钟 purge gap，降低相邻市场高度相关导致的泄漏风险。

## 10. Model Selection Strategy

训练流程：

1. 构建基础 feature set。
2. 训练 baseline logistic regression。
3. 训练 tree model。
4. 使用 time split validation 评估。
5. 对每个模型输出 `p_up`。
6. 对 validation 进行 threshold search。
7. 筛选 coverage `>= 0.70` 的方案。
8. 选择 validation selection_score 最高者。

模型不应以 train score 做选择。若多个模型 validation score 接近，选择：

1. coverage 更高者
2. calibration 更好者，即 brier/log_loss 更低
3. feature_count 更少者

## 11. Threshold Search Details

默认 grid：

- `selected_t_up`: `0.50` 到 `0.75`，步长 `0.005`
- `selected_t_down`: `0.25` 到 `0.50`，步长 `0.005`

为了满足 coverage `>=0.70`，搜索范围必须允许接近全覆盖：

- 最宽松组合：`selected_t_up=0.50`, `selected_t_down=0.50`
- 该组合约等于不 abstain，coverage 接近 1.0

如果要避免 `t_up == t_down` 的冲突，可定义：

- `p_up >= 0.5` 预测 up
- `p_up < 0.5` 预测 down

并将该策略作为 full-coverage baseline 写入 `threshold_search.csv`。

## 12. Validation Risk Controls

必须防止以下泄漏：

- 使用 market resolve 后的 trade。
- 使用 `_final_outcome` 派生任何特征。
- 用完整市场价格路径构建早期特征。
- 对全量数据 fit imputer/scaler 后再切分。
- 在 validation 上反复人工调参后只报告最好一次。

实现要求：

- 特征生成阶段只读取 label 用于最终 join，不允许 label 进入 feature dataframe。
- scaler/imputer 只 fit train，再 transform validation。
- `feature_columns.json` 必须明确列出模型输入列。
- `dataset_quality.json` 记录 early-window trade 占比和空窗口 market 数。

## 13. Evaluation Artifacts

### 13.1 `probability_deciles.csv`

按 validation `p_up` 分 10 桶，输出：

- decile
- row_count
- p_up_min
- p_up_max
- p_up_mean
- actual_up_rate
- accepted_rate
- accuracy

### 13.2 `feature_importance.csv`

输出：

- feature
- importance
- rank
- direction 或 permutation_delta_score，若可用

### 13.3 `false_up_slices.csv`

针对预测 `up` 但实际 `down` 的 accepted samples，按特征分桶找高错误率区域。

### 13.4 `false_down_slices.csv`

针对预测 `down` 但实际 `up` 的 accepted samples，按特征分桶找高错误率区域。

### 13.5 `regime_slices.csv`

按 market context 分组：

- hour UTC
- early liquidity tercile
- early volatility tercile
- up/down volume imbalance tercile
- missing-side flag

输出每组 coverage、accuracy、selection_score。

## 14. Config Schema

`configs/early_trade_label_v1.yaml`：

```yaml
project: btc-polymarket-early-trade-v1
trades_csv: out_btc5m_30d_limit_sell_resolved/trades_sell_resolved.csv
refs_csv: out_btc5m_30d_limit_sell_resolved/market_refs_resolved.csv
outdir: models/early_trade_label_v1
processed_dir: data_processed/early_trade_label_v1
feature_window_seconds: 120
label_positive: up
time_split:
  train_fraction: 0.70
  purge_minutes: 10
model:
  candidates:
    - hist_gradient_boosting
    - random_forest
    - logistic_regression
threshold_search:
  min_coverage: 0.70
  t_up_min: 0.50
  t_up_max: 0.75
  t_down_min: 0.25
  t_down_max: 0.50
  step: 0.005
metrics:
  baseline_accuracy: majority_class
```

## 15. One-Command Pipeline

最终应支持：

```bash
python -m early_trade_label.cli run-all \
  --config configs/early_trade_label_v1.yaml
```

该命令顺序执行：

1. build dataset
2. generate features
3. train models
4. search thresholds
5. evaluate
6. write reports

## 16. Acceptance Criteria

功能验收：

- 能从当前仓库 csv 生成一行一个 market 的 dataset。
- 所有特征严格来自前 120 秒。
- 能生成 `models/early_trade_label_v1/evaluation.json`。
- `evaluation.json` 包含 train 和 validation metrics。
- `threshold_search.csv` 中存在 coverage `>=0.70` 的候选行，除非数据本身异常。
- 选择的 validation policy 必须满足 coverage `>=0.70`。

质量验收：

- `dataset_quality.json` 记录样本数量、label 分布、空 early window 数、丢弃行原因。
- 脚本可重复执行，覆盖同名输出前先写临时文件再原子替换。
- 任意随机模型设置固定 `random_state=42`。
- 所有输出路径由 config 控制。
- 训练和验证切分按时间，不允许随机打散作为默认模式。

## 17. Implementation Order

推荐实现顺序：

1. 创建 `early_trade_label/schema.py` 和 `config.py`。
2. 实现 `build_dataset.py`，先确保无泄漏地得到 market-level raw aggregates。
3. 实现 `features.py`，生成第一版 100 到 300 个稳定特征。
4. 实现 `train_model.py`，先跑 sklearn baseline。
5. 实现 `threshold_search.py`，强制 coverage `>=0.70`。
6. 实现 `evaluate.py`，对齐 `evaluation.json` 风格。
7. 实现 `report.py` 和 `cli.py`。
8. 用 7d full 数据做 sanity check，用 30d sell-only 数据做主验证。

## 18. Known Risks

- 前 2 分钟可能交易稀疏，部分 market 特征接近空值；必须保留 missing indicators。
- 30d 数据目前看起来是 sell-only，缺少 buy-side microstructure；模型要显式识别数据源约束。
- BTC 5m 相邻市场高度相关，普通随机 split 会明显高估 validation。
- validation coverage `>=0.70` 会降低 precision，上限可能低于低 coverage 策略。
- `selection_score` 对 `downside_risk` 定义敏感，必须在 `evaluation.json` 中写清楚公式。

## 19. Recommended First Benchmark

第一版 benchmark：

- 数据：`out_btc5m_30d_limit_sell_resolved/trades_sell_resolved.csv`
- refs：`out_btc5m_30d_limit_sell_resolved/market_refs_resolved.csv`
- feature window：120 秒
- split：时间顺序 70/30，purge 10 分钟
- model：HistGradientBoostingClassifier
- threshold：coverage `>=0.70` grid search
- selection：最大 validation `selection_score`

若第一版 validation selection_score 不稳定，优先改进：

1. probability calibration
2. time bucket features
3. missing-side handling
4. regime-specific threshold
5. monotonic or simpler model to reduce overfit

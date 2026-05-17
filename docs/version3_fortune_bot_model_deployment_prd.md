# PRD: version3 fortune_bot 离线模型调优与 Polymarket BTC 5m Maker 部署

## 1. 背景

当前 `trade_distribution` 仓库已经产出 BTC 5m Polymarket 方向预测模型与评估文件，主要产物位于：

- `models/early_trade_label_v1/model.pkl`
- `models/early_trade_label_v1/evaluation.json`
- `models/early_trade_label_v1/feature_columns.json`
- `models/early_trade_label_v1/probability_reference.json`
- `models/early_trade_label_v1/threshold_search.csv`

交易执行部分参考：

- `C:\Users\ROG\Desktop\crypto_engine\execution_engine`

现有执行引擎已经包含生产部署、paper/live 模式、artifact manifest、Polymarket CLOB v2 下单、systemd timer、日志和 smoke test 的基础能力。新需求是把本仓库训练出的模型部署到 `version3` server 的 `fortune_bot` 目录，并把 `docs\polymarket_btc5m_maker_method_summary.md` 中的 maker limit buy 方法落成可执行策略。

本 PRD 定义从离线调优、artifact 打包、server 清空与部署、paper 验证、live 开关到回滚的完整产品与工程要求。

## 2. 目标

核心目标：

- 在代码实现完成后，先对离线模型进行调优。
- 离线验证必须满足 `validation coverage >= 0.70`。
- 离线验证必须满足 `accepted_sample_accuracy > 0.80`。
- 只有同时满足上述两个指标，才允许进入 live 部署候选。
- 将合格模型、阈值、校准器、特征 schema、交易参数和部署脚本打包部署到 `version3` server 的 `fortune_bot`。
- 部署前必须清空 `version3` 上既有 `fortune_bot` 文件夹内容，但必须先做可回滚备份并停掉相关服务。
- 实现 Polymarket BTC 5m maker limit buy 方法，包括离线 `a_j = P(fill | win)` 表、实时 edge 计算、预算分配和 Polymarket CLOB 下单。

非目标：

- 不在 PRD 阶段执行真实清空服务器目录。
- 不在未达离线指标时启用 live 下单。
- 不把私钥、CLOB API key、deposit wallet 地址或 `secrets.env` 内容写入仓库、日志、PRD 或部署包。
- 不硬编码交易阈值、模型概率阈值、token id、市场 slug 或 Polymarket 凭证。

## 3. 当前基线

本仓库当前 `models/early_trade_label_v1/evaluation.json` 指标：

- `validation coverage = 0.7009671179883946`
- `validation accepted_sample_accuracy = 0.7875275938189845`
- `validation selected_t_up = 0.645`
- `validation selected_t_down = 0.345`

参考执行引擎 `execution_engine/deploy/baseline/metrics.json` 指标：

- `validation coverage = 0.7661107404936625`
- `validation accepted_sample_accuracy = 0.8086032741205155`
- `validation selected_t_up = 0.595`
- `validation selected_t_down = 0.37`

结论：

- 两套当前基线都满足 coverage 下限。
- 本仓库当前基线未满足 `accepted_sample_accuracy > 0.80`。
- 参考执行引擎 baseline 满足 `accepted_sample_accuracy > 0.80`，但不等同于本仓库当前 deploy artifact 已达标。
- 因此 live 部署必须以“本仓库当前 deploy artifact 调优后重新验证通过”为前置条件；当前模型最多只能进入 paper / dry-run 验证。

## 4. 用户与使用场景

主要用户：

- 策略开发者：训练、调优、验证、导出模型。
- 运维/部署者：清空并初始化 `version3/fortune_bot`，安装依赖，启动服务。
- 交易操作者：审核 paper 结果，确认风险参数，决定是否开启 live。

关键场景：

- 离线训练完成后，一键生成可部署 artifact。
- server 上停止旧服务、备份旧目录、清空 `fortune_bot` 并部署新版本。
- 每 5 分钟 BTC 市场窗口，在约第 120 秒附近生成方向信号。
- 如果信号被阈值接受，计算 maker limit buy 候选订单。
- 只有正 EV 且满足预算/最小份额/风控约束的订单才会进入 paper 或 live 提交流程。

## 5. 成功指标

离线模型硬门槛：

- `validation_metrics.coverage >= 0.70`
- `validation_metrics.accepted_sample_accuracy > 0.80`
- `validation_metrics.accepted_count >= 1000`，除非验证集规模不足且 PRD 变更另行批准。
- `validation_metrics.up_prediction_count >= 200`
- `validation_metrics.down_prediction_count >= 200`
- `decision_policy.coverage_constraint_satisfied = true`
- `evaluation.json` 必须记录所有阈值、切分窗口、样本数、模型版本和 artifact hash。

部署硬门槛：

- `fortune_bot` 旧目录备份存在。
- 新部署目录内不包含任何明文 secret。
- `paper` 模式 smoke test 成功。
- `run_once --mode paper` 产生 summary JSON。
- live 模式默认关闭，必须显式修改配置才能启用。

交易硬门槛：

- 只做 maker limit buy。
- 单轮总预算 `sum(B_j) <= 7 USDC`。
- 单笔预算 `B_j <= 4 USDC`。
- 单笔至少 5 shares，即 `B_j / p_j >= 5`。
- 只提交 `edge_j > 0` 的候选订单。
- 订单价格必须按市场 tick size 向下取整，并满足 Polymarket min/max price。

## 6. 数据与训练范围

默认数据：

- `out_btc5m_30d_limit_sell_resolved/trades_sell_resolved.csv`
- `out_btc5m_30d_limit_sell_resolved/market_refs_resolved.csv`

可选补充：

- `out_btc5m_7d_full_strategy/trades_raw.csv`
- `out_btc5m_7d_full_strategy/market_refs_resolved.csv`

训练原则：

- 预测模型只能使用决策时刻可获得的信息。
- 如果部署决策发生在开盘后约 120 秒，特征必须严格对齐到该时点或更早。
- 不允许使用 resolve 后信息、未来 trade、未来 Binance candle 或人工回填的未来状态。
- 默认采用时间顺序切分，禁止随机切分作为正式报告。
- 调参必须保留 holdout 或 walk-forward 验证，不能只报告在同一 validation 上反复挑选后的最优结果。

## 7. 离线调优要求

调优流程：

1. 固定数据版本与配置 hash。
2. 生成训练/验证/最终 holdout 特征。
3. 训练候选模型：LightGBM、CatBoost、Logistic/Platt calibration、可选 blending。
4. 对每个候选模型做概率校准。
5. 在 validation 上搜索 `t_up` / `t_down`。
6. 只保留 coverage >= 0.70 的候选。
7. 在候选中优先最大化 `accepted_sample_accuracy`，其次看 `balanced_precision`、`brier_score`、`log_loss` 和信号两侧分布。
8. 对最优候选执行 walk-forward 或最近窗口 holdout 复核。
9. 只有复核仍满足 `coverage >= 0.70` 且 `accepted_sample_accuracy > 0.80`，才能导出 deploy artifact。

调优输出：

- `models/<model_version>/evaluation.json`
- `models/<model_version>/model.pkl`
- `models/<model_version>/calibrator.pkl`，如使用校准器
- `models/<model_version>/feature_columns.json`
- `models/<model_version>/threshold_search.csv`
- `models/<model_version>/probability_deciles.csv`
- `models/<model_version>/regime_slices.csv`
- `models/<model_version>/false_up_slices.csv`
- `models/<model_version>/false_down_slices.csv`
- `models/<model_version>/artifact_manifest.json`

失败处理：

- 如果 `coverage >= 0.70` 时无法达到 `accepted_sample_accuracy > 0.80`，不得开启 live。
- 可以导出 paper-only artifact，但 manifest 必须标记 `live_eligible: false`。
- PRD 验收状态应标记为“模型指标未达标，部署阻塞”。

## 8. Maker 方法实现要求

必须把 `docs\polymarket_btc5m_maker_method_summary.md` 的方法转成工程实现，并修复/重写该文档，使其成为可读、可复现的策略说明。

核心定义：

- 模型先决定交易方向 `side in {up, down}`。
- `q` 表示该方向在当前阈值/分桶下的胜率估计，不能固定写死为 0.7。
- 默认 `q` 来源优先级：
  1. 经过校准的实时 `p_side`。
  2. validation probability bucket 的实测 accepted accuracy。
  3. manifest 中记录的整体 `accepted_sample_accuracy`，仅作为保守 fallback。
- 候选订单 `j = (t_j, p_j)`。
- `a_j = P(fill_j | win, context)`，由离线历史成交机会估计。
- 保守错误场景 `b = P(fill | lose) = 1`。
- 单位资金期望收益：

```text
edge_j = q * a_j * (1 / p_j - 1) - (1 - q) * b
```

候选订单过滤：

- `edge_j > 0`
- `p_j` 在市场允许价格范围内。
- `B_j >= 5 * p_j`
- `B_j <= 4`
- `sum(B_j) <= 7`
- 同一 market/window/order key 不允许重复提交。

预算分配：

1. 对所有正 EV 候选按 `edge_j` 降序排序。
2. 枚举或贪心选择 1 到 3 个订单。
3. 每单先分配 `min_B_j = 5 * p_j`。
4. 剩余预算按 `edge_j` 从高到低补足。
5. 选择 `sum(B_j * edge_j)` 最大的组合。
6. 如果正 EV 订单不足，不强行花满 7 USDC。

离线 `a_j` 表：

- 输出文件：`deploy/<model_version>/maker_fill_table.parquet` 或 `.csv`。
- 维度至少包含：`prediction_side`、`decision_second_bucket`、`current_price_bucket`、`order_delay_seconds`、`limit_price`。
- 指标至少包含：`win_market_count`、`win_fill_market_count`、`a_win_market_fill`、`lose_market_count`、`lose_fill_market_count`、`a_lose_market_fill`。
- 样本数不足的 bucket 必须回退到更粗粒度 bucket，并在输出中标记 fallback level。

## 9. 执行引擎改造范围

参考 `C:\Users\ROG\Desktop\crypto_engine\execution_engine`，新 `fortune_bot` 应复用以下模式：

- `config.example.yaml` / `config.yaml` 分离。
- `execution_engine/deploy/<version>` 管理 artifact。
- `artifact_manifest.json` 作为模型、校准器、阈值、特征和策略表的唯一来源。
- `run_once.py` 支持 `paper` 与 `live`。
- `prewarm.py` 提前缓存 Binance/Polymarket 数据。
- `artifacts/logs/...` 写 JSONL 审计日志。
- `artifacts/state/...` 写 idempotency 和缓存。
- systemd timer 每 5 分钟触发一次。

需要新增或修改的模块：

- `maker_fill_table.py`：离线生成 `a_j` 表。
- `maker_order_plan.py`：根据 `q`、`a_j`、价格和预算生成 maker limit buy 订单。
- `artifact_export.py`：从训练目录导出 deploy artifact。
- `deploy/version3_deploy.ps1` 或 `deploy/version3_deploy.sh`：执行 server 部署。
- `docs/polymarket_btc5m_maker_method_summary.md`：重写为可读中文方法说明。
- `docs/version3_fortune_bot_model_deployment_prd.md`：本 PRD。

对现有 `order_plan.py` 的要求：

- 当前参考实现是固定两单 limit plan。
- 新实现必须支持 EV 驱动的可变候选订单集合。
- 保留固定两单模式作为 fallback 或 paper 对照，但 live 默认必须使用 maker EV plan。

## 10. Artifact Contract

部署目录示例：

```text
fortune_bot/
  execution_engine/
    deploy/
      <model_version>/
        artifact_manifest.json
        model.pkl
        calibrator.pkl
        feature_columns.json
        probability_reference.json
        threshold_search.json
        maker_fill_table.parquet
        evaluation.json
        metrics.json
```

`artifact_manifest.json` 必须包含：

```json
{
  "project": "btc-polymarket-5m-maker",
  "model_version": "string",
  "created_at_utc": "string",
  "live_eligible": false,
  "model_file": "model.pkl",
  "calibrator_file": "calibrator.pkl",
  "feature_columns_file": "feature_columns.json",
  "maker_fill_table_file": "maker_fill_table.parquet",
  "thresholds": {
    "t_up": 0.0,
    "t_down": 0.0,
    "min_coverage": 0.7,
    "min_accepted_sample_accuracy": 0.80
  },
  "validation_metrics": {
    "coverage": 0.0,
    "accepted_sample_accuracy": 0.0,
    "accepted_count": 0
  },
  "trading": {
    "max_total_budget_usdc": 7.0,
    "max_order_budget_usdc": 4.0,
    "min_shares": 5.0,
    "maker_only": true
  }
}
```

## 11. version3 部署流程

假设：

- SSH alias 或 host 为 `version3`。
- 远端目标目录为 `~/fortune_bot`，实际执行前必须通过 `pwd` 和 `ls` 确认。
- 远端运行用户拥有该目录和 systemd user 或 system service 操作权限。

部署步骤：

1. 本地确认模型指标达标。
2. 本地生成 deploy bundle，例如 `dist/fortune_bot_<model_version>.tar.gz`。
3. SSH 到 `version3`。
4. 停止旧服务：

```bash
sudo systemctl stop fortune-bot.timer || true
sudo systemctl stop fortune-bot.service || true
```

5. 备份旧目录：

```bash
ts=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p ~/fortune_bot_backups
if [ -d ~/fortune_bot ]; then
  tar -czf ~/fortune_bot_backups/fortune_bot_$ts.tar.gz -C ~ fortune_bot
fi
```

6. 清空旧目录，但不删除目录本身：

```bash
mkdir -p ~/fortune_bot
find ~/fortune_bot -mindepth 1 -maxdepth 1 -exec rm -rf {} +
```

7. 解压新 bundle 到 `~/fortune_bot`。
8. 创建 venv 并安装依赖。
9. 在 server 本地创建或保留 `execution_engine/secrets.env`，权限必须为 `600`。
10. 复制 `config.example.yaml` 为 `config.yaml`，默认 `runtime.mode=paper`、`orders.enabled=false`。
11. 运行 prewarm。
12. 运行 paper `run_once`。
13. 安装或刷新 systemd unit。
14. 启动 timer，仍保持 paper。
15. 观察至少 3 个 market cycle 后再评估是否开启 live。

安全要求：

- 清空目录前必须确认当前路径等于目标 `fortune_bot`。
- 不允许对空变量执行 `rm -rf "$TARGET"`。
- 不允许在 PRD 或日志中打印 `secrets.env` 内容。
- Polymarket CLOB v2 runtime 使用 `POLYMARKET_SIGNATURE_TYPE=1`；如后续切换到其他 wallet/signature flow，必须先更新配置、文档和 smoke test。
- 优先使用 `DEPOSIT_WALLET_ADDRESS`，不要依赖 UI 猜测 funder。

## 12. 配置要求

示例配置：

```yaml
baseline:
  artifact_dir: execution_engine/deploy/<model_version>
  manifest_file: artifact_manifest.json

runtime:
  mode: paper
  audit_log: artifacts/logs/execution_engine/live.jsonl
  summary_dir: artifacts/logs/execution_engine/summaries
  idempotency_store_path: artifacts/state/execution_engine/idempotency.json

features:
  source: validation_snapshot
  path: null
  feature_window_seconds: 120
  source_is_sell_only: false

thresholds:
  t_up: null
  t_down: null

orders:
  enabled: false
  planner: maker_ev
  max_total_budget_usdc: 7.0
  max_order_budget_usdc: 4.0
  min_shares: 5.0
  max_orders_per_window: 3
  maker_only: true

polymarket:
  host: https://clob.polymarket.com
  gamma_base_url: https://gamma-api.polymarket.com
  data_api_url: https://data-api.polymarket.com
  chain_id: 137
  signature_type: 1
  private_key_env: POLYMARKET_PRIVATE_KEY
  api_key_env: CLOB_API_KEY
  api_secret_env: CLOB_SECRET
  api_passphrase_env: CLOB_PASS_PHRASE
  funder_env: DEPOSIT_WALLET_ADDRESS
```

## 13. Paper 与 Live 验证

Paper smoke test：

- `prewarm.py --print-json` 成功。
- `run_once.py --mode paper --print-json` 成功。
- summary JSON 包含：market、window、features、p_up、p_down、decision、q、candidate_orders、selected_orders、edge、budget、skip reasons。
- 当模型 abstain 时，不提交订单，但仍记录原因。

Live 前检查：

- 模型 manifest `live_eligible=true`。
- 最近部署 artifact hash 与本地导出 hash 一致。
- Polymarket CLOB credentials 与 private key 对齐。
- Deposit wallet 已部署且有余额/allowance。
- `orders.enabled=true` 只在人工确认后修改。
- 先用最小价格/最小 shares 做 live full-flow 测试。

Live 成功标准：

- service 单次运行退出码为 0。
- 若有订单，订单响应包含有效 order id 或明确 rejected reason。
- idempotency 防止同一窗口重复下单。
- 日志不包含 secret。

## 14. 日志与监控

必须记录：

- UTC timestamp。
- model_version。
- artifact hash。
- market slug / condition id / token id。
- decision window。
- feature availability。
- p_up / p_down。
- thresholds。
- accepted / abstain reason。
- q 来源。
- maker_fill_table bucket 和 fallback level。
- candidate edge。
- selected order price / size / budget。
- paper/live 模式。
- Polymarket response status。

不得记录：

- private key。
- CLOB secret。
- CLOB passphrase。
- deposit wallet 完整地址。
- API key 完整值。

## 15. 回滚

回滚触发条件：

- paper smoke test 失败。
- live order path 报认证或签名错误。
- 发现 artifact schema 不兼容。
- 发现 validation 指标被错误计算。
- 出现重复下单或预算约束失效。

回滚方式：

1. 停止 timer 和 service。
2. 将当前 `fortune_bot` 打包留档。
3. 从 `~/fortune_bot_backups` 恢复最近一个健康版本。
4. 恢复后以 paper 模式启动。
5. 重新跑 smoke test。
6. 记录 incident summary。

## 16. 验收标准

PRD/文档验收：

- 本 PRD 存在且覆盖调优、部署、交易、风控、回滚。
- `docs/polymarket_btc5m_maker_method_summary.md` 可读、无乱码、公式正确。
- 文档明确说明 `q`、`a_j`、`edge_j`、预算和订单约束。

代码验收：

- 能一键运行离线调优 pipeline。
- 能导出 deploy bundle。
- 能生成 `maker_fill_table`。
- 能在 paper 模式生成 maker EV order plan。
- 不达指标时 manifest 自动标记 `live_eligible=false`。
- 达指标时 manifest 自动标记 `live_eligible=true`。

模型验收：

- `validation coverage >= 0.70`。
- `accepted_sample_accuracy > 0.80`。
- up/down 两侧都有足够信号。
- 复核窗口未明显退化。

部署验收：

- `version3` 上旧 `fortune_bot` 已备份。
- `fortune_bot` 已清空后重新部署。
- server 依赖安装成功。
- paper smoke test 成功。
- systemd timer 正常触发。
- live 默认关闭。

交易验收：

- 只提交 maker limit buy。
- 预算、价格、shares、正 EV、idempotency 约束全部生效。
- Polymarket CLOB v2 使用 `POLYMARKET_SIGNATURE_TYPE=1`，并通过 smoke test 验证 signer、funder、CLOB credentials 三者一致。

## 17. 实施顺序

1. 修复并重写 `docs/polymarket_btc5m_maker_method_summary.md`。
2. 新增 maker fill table 离线生成逻辑。
3. 新增 maker EV order planner。
4. 把 planner 接入 execution runtime 的 paper path。
5. 增加 artifact manifest 与 deploy bundle 导出。
6. 对离线模型调优，直到满足 `coverage >= 0.70` 且 `accepted_sample_accuracy > 0.80`。
7. 生成 `live_eligible=true` artifact。
8. 部署到 `version3`，先停服务、备份、清空 `fortune_bot`，再解包。
9. 运行 prewarm 和 paper smoke test。
10. 观察 paper 运行，再人工决定是否开启 live。

## 18. 未决问题

- `version3` 的 SSH alias、远端用户、最终绝对路径需要执行前确认。
- `fortune_bot` 是否已有 systemd unit 名称，需要部署前读取 server 状态。
- `q` 最终采用实时 calibrated probability 还是 validation bucket accuracy，需要调优实验后固定。
- 如果在 `coverage >= 0.70` 下无法达到 `accepted_sample_accuracy > 0.80`，需要决定是降低 coverage、增加数据、扩展特征，还是保持 paper-only。

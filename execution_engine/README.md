# execution_engine 部署与定时任务运维说明

本文说明如何把当前仓库的 `execution_engine` 部署到 server 上运行，以及每次 GitHub 代码更新后如何重新生成 artifact、重新部署 workflow、重启/恢复 systemd 定时任务。

当前远端仓库：

```text
https://github.com/xzavierxu-pixel/trade_distribution.git
```

默认部署目标：

```text
server ssh alias: version3
server path: ~/fortune_bot
server resolved path: /home/ec2-user/fortune_bot
systemd user timer: fortune-bot.timer
systemd user service: fortune-bot.service
default runtime mode: paper
```

## 1. 目录和关键文件

```text
execution_engine/
  run_once.py                 单次执行入口，paper/live 都走这里
  observe_paper.py            连续 paper 观察入口
  preflight.py                paper/live 环境检查
  verify_prd.py               artifact、bundle、模型门槛和交易约束校验
  artifact_export.py          从模型目录导出 deploy artifact 和 tar.gz bundle
  deploy/early_trade_label_v1 当前随包发布的模型 artifact

deploy/
  version3_deploy.ps1         本地到 version3 的部署脚本
  version3_rollback.ps1       server 回滚脚本
  fortune-bot.service         systemd user service
  fortune-bot.timer           每 5 分钟触发一次 service 的 user timer

config.example.yaml           server 首次部署时复制为 config.yaml
dist/fortune_bot_early_trade_label_v1.tar.gz  当前部署包
```

## 2. 本地生成部署包

在本地仓库根目录执行：

```powershell
python -m execution_engine.artifact_export `
  --model-dir models\early_trade_label_v1 `
  --deploy-dir execution_engine\deploy `
  --model-version early_trade_label_v1 `
  --maker-fill-table models\early_trade_label_v1\maker_fill_table.csv `
  --bundle-out dist\fortune_bot_early_trade_label_v1.tar.gz
```

导出后必须先本地校验：

```powershell
python -m execution_engine.verify_prd `
  --artifact-dir execution_engine\deploy\early_trade_label_v1 `
  --bundle dist\fortune_bot_early_trade_label_v1.tar.gz

python -m execution_engine.self_test `
  --artifact-dir execution_engine\deploy\early_trade_label_v1 `
  --bundle dist\fortune_bot_early_trade_label_v1.tar.gz `
  --config config.example.yaml
```

如果当前模型的 `accepted_sample_accuracy > 0.80` 门槛没过，`verify_prd` 在普通 paper 校验下仍可 `ok=true`，但会输出准确率 gate warning，并且 artifact 会保持：

```text
live_eligible: false
deployment_status: paper_only_blocked
```

这种状态只能部署 paper，不允许 live。

## 3. 首次部署到 server

先确认本机 SSH alias 能连到 server：

```powershell
ssh version3 "whoami; pwd; hostname"
```

做一次 dry run，确认远端路径只会解析到 `~/fortune_bot`：

```powershell
.\deploy\version3_deploy.ps1 `
  -Bundle dist\fortune_bot_early_trade_label_v1.tar.gz `
  -HostName version3 `
  -Target "~/fortune_bot" `
  -DryRun
```

正式部署：

```powershell
.\deploy\version3_deploy.ps1 `
  -Bundle dist\fortune_bot_early_trade_label_v1.tar.gz `
  -HostName version3 `
  -Target "~/fortune_bot"
```

部署脚本会在 server 上自动完成：

```text
1. 停止 fortune-bot.timer 和 fortune-bot.service。
2. 备份旧目录到 ~/fortune_bot_backups/fortune_bot_<timestamp>.tar.gz。
3. 清空 ~/fortune_bot。
4. 解压新的 bundle。
5. 创建/更新 .venv。
6. 安装运行依赖。
7. 如果 config.yaml 不存在，则从 config.example.yaml 复制。
8. 运行 verify_prd、self_test、paper preflight、prewarm、paper run_once、observe_paper 1 cycle。
9. 安装 deploy/fortune-bot.service 和 deploy/fortune-bot.timer 到 ~/.config/systemd/user/。
10. systemctl --user daemon-reload。
11. enable --now fortune-bot.timer。
```

如果想只部署文件和 smoke test，但暂不启动定时任务：

```powershell
.\deploy\version3_deploy.ps1 `
  -Bundle dist\fortune_bot_early_trade_label_v1.tar.gz `
  -HostName version3 `
  -Target "~/fortune_bot" `
  -SkipTimerStart
```

## 4. server 上的配置

部署后 server 目录是：

```bash
cd ~/fortune_bot
```

首次部署会创建：

```text
config.yaml
.venv/
artifacts/logs/execution_engine/
artifacts/state/execution_engine/
```

默认 `config.yaml` 是 paper 模式：

```yaml
runtime:
  mode: paper
orders:
  enabled: false
features:
  source: validation_snapshot
```

paper 模式可以不配置真实交易密钥。live 模式必须满足：

```text
artifact_manifest.live_eligible=true
orders.enabled=true
runtime.mode=live
POLYMARKET_PRIVATE_KEY 已设置
CLOB_API_KEY 已设置
CLOB_SECRET 已设置
CLOB_PASS_PHRASE 已设置
DEPOSIT_WALLET_ADDRESS 已设置
polymarket.signature_type=3
```

密钥应放在 server 的：

```text
~/fortune_bot/execution_engine/secrets.env
```

该文件会被 systemd service 通过 `EnvironmentFile` 读取，不要提交到 GitHub。

## 5. GitHub 代码更新后的重新部署流程

每次本地从 GitHub 拉到新代码后，按这个顺序执行：

```powershell
git fetch origin
git pull --ff-only origin main
```

如果新代码包含模型、fill table、配置、部署脚本或 execution engine 变化，重新导出 bundle：

```powershell
python -m execution_engine.artifact_export `
  --model-dir models\early_trade_label_v1 `
  --deploy-dir execution_engine\deploy `
  --model-version early_trade_label_v1 `
  --maker-fill-table models\early_trade_label_v1\maker_fill_table.csv `
  --bundle-out dist\fortune_bot_early_trade_label_v1.tar.gz
```

重新跑本地校验：

```powershell
python -m execution_engine.verify_prd `
  --artifact-dir execution_engine\deploy\early_trade_label_v1 `
  --bundle dist\fortune_bot_early_trade_label_v1.tar.gz

python -m execution_engine.self_test `
  --artifact-dir execution_engine\deploy\early_trade_label_v1 `
  --bundle dist\fortune_bot_early_trade_label_v1.tar.gz `
  --config config.example.yaml
```

再部署到 server：

```powershell
.\deploy\version3_deploy.ps1 `
  -Bundle dist\fortune_bot_early_trade_label_v1.tar.gz `
  -HostName version3 `
  -Target "~/fortune_bot"
```

部署完成后在 server 验证 timer：

```bash
systemctl --user status fortune-bot.timer
systemctl --user list-timers fortune-bot.timer
journalctl --user -u fortune-bot.service -n 100 --no-pager
```

如果 server 本身也保留 Git checkout，仍建议以 bundle 部署为准。不要直接在 `~/fortune_bot` 里 `git pull` 后运行旧虚拟环境，因为这样可能绕过 artifact manifest、bundle verifier、备份和 systemd 单元更新。

## 6. 定时任务操作

查看 timer 状态：

```bash
systemctl --user status fortune-bot.timer
systemctl --user list-timers fortune-bot.timer
```

手动启动/停止 timer：

```bash
systemctl --user start fortune-bot.timer
systemctl --user stop fortune-bot.timer
```

重新加载 service/timer 文件：

```bash
systemctl --user daemon-reload
systemctl --user enable --now fortune-bot.timer
```

手动触发一次 paper run：

```bash
cd ~/fortune_bot
. .venv/bin/activate
python -m execution_engine.run_once --config config.yaml --mode paper --print-json
```

观察最近日志：

```bash
journalctl --user -u fortune-bot.service -n 100 --no-pager
tail -n 50 artifacts/logs/execution_engine/live.jsonl
ls -lt artifacts/logs/execution_engine/summaries | head
```

如果 user timer 在退出 SSH 后不触发，需要启用 linger：

```bash
loginctl enable-linger "$USER"
```

## 7. Paper 到 Live 的切换

先在 server 跑 paper 检查：

```bash
cd ~/fortune_bot
. .venv/bin/activate
python -m execution_engine.verify_prd \
  --artifact-dir execution_engine/deploy/early_trade_label_v1 \
  --bundle /tmp/fortune_bot_bundle.tar.gz
python -m execution_engine.preflight --config config.yaml --mode paper --print-json
python -m execution_engine.observe_paper --config config.yaml --cycles 3 --sleep-seconds 300 --print-json
```

只有当下面命令通过时，才允许考虑 live：

```bash
python -m execution_engine.verify_prd \
  --artifact-dir execution_engine/deploy/early_trade_label_v1 \
  --bundle /tmp/fortune_bot_bundle.tar.gz \
  --require-live

python -m execution_engine.preflight --config config.yaml --mode live --print-json
```

当前代码的 live guard 在 `run_once.py` 中强制要求：

```text
orders.enabled=true
artifact_manifest.live_eligible=true
```

如果任一条件不满足，live 会直接报错并拒绝下单。

## 8. 回滚

查看 server 备份：

```bash
ls -lt ~/fortune_bot_backups
```

本地触发回滚到最新备份：

```powershell
.\deploy\version3_rollback.ps1 `
  -HostName version3 `
  -Target "~/fortune_bot"
```

回滚到指定备份：

```powershell
.\deploy\version3_rollback.ps1 `
  -HostName version3 `
  -Target "~/fortune_bot" `
  -BackupPath "/home/ec2-user/fortune_bot_backups/fortune_bot_YYYYMMDDTHHMMSSZ.tar.gz"
```

回滚脚本会先停止 timer/service，给当前目录做一份 pre-rollback 备份，再解压指定备份并尝试重新启动 timer。

## 9. 常见问题

### SSH alias `version3` 无法解析

先修复本机 SSH config，确保下面命令能成功：

```powershell
ssh version3 "echo ok"
```

也可以临时传入真实 host：

```powershell
.\deploy\version3_deploy.ps1 -Bundle dist\fortune_bot_early_trade_label_v1.tar.gz -HostName "<user>@<host>"
```

### verify_prd 只有 accuracy warning

这表示 paper 部署包结构和交易约束可用，但模型未满足 live 门槛。可以部署 paper，不允许 live。

### timer 存在但没有 summary

检查：

```bash
systemctl --user status fortune-bot.timer
systemctl --user status fortune-bot.service
journalctl --user -u fortune-bot.service -n 200 --no-pager
```

确认 `WorkingDirectory=%h/fortune_bot` 存在，`.venv/bin/python` 存在，`config.yaml` 存在，并且 `execution_engine/deploy/early_trade_label_v1/artifact_manifest.json` 存在。

### 重新部署后配置丢失

部署脚本会清空 `~/fortune_bot`，因此重要的 server 配置需要在部署前备份。脚本会把旧目录打包到 `~/fortune_bot_backups`，可以从备份中恢复 `config.yaml` 或 `execution_engine/secrets.env`。

## 10. 推荐的每次更新检查清单

```text
1. git pull --ff-only origin main
2. 重新生成 dist/fortune_bot_early_trade_label_v1.tar.gz
3. 本地 verify_prd 通过
4. 本地 self_test 通过
5. deploy/version3_deploy.ps1 正式部署
6. server paper preflight 通过
7. server run_once paper 成功
8. systemctl --user list-timers fortune-bot.timer 显示下一次触发时间
9. journalctl --user -u fortune-bot.service 无异常
10. 如果要 live，额外通过 verify_prd --require-live 和 preflight --mode live
```

param(
  [Parameter(Mandatory=$true)][string]$Bundle,
  [string]$HostName = "version3",
  [string]$Target = "~/fortune_bot",
  [switch]$DryRun,
  [switch]$SkipTimerStart
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Bundle)) {
  throw "Bundle not found: $Bundle"
}

$bundlePath = (Resolve-Path -LiteralPath $Bundle).Path
$remoteBundle = "/tmp/fortune_bot_bundle.tar.gz"

Write-Host "Verifying local deploy bundle..."
python -m execution_engine.verify_prd `
  --artifact-dir execution_engine/deploy/early_trade_label_v1 `
  --bundle $bundlePath | Out-Host

$remoteScript = @'
set -euo pipefail

TARGET="${TARGET:-$HOME/fortune_bot}"
case "$TARGET" in
  "~") TARGET="$HOME" ;;
  "~/"*) TARGET="$HOME/${TARGET:2}" ;;
esac
EXPECTED="$HOME/fortune_bot"
BUNDLE="/tmp/fortune_bot_bundle.tar.gz"

echo "remote_user=$(whoami)"
echo "remote_pwd=$(pwd)"
echo "target=$TARGET"
echo "expected=$EXPECTED"

resolved_parent="$(cd "$(dirname "$TARGET")" && pwd)"
resolved_target="$resolved_parent/$(basename "$TARGET")"
if [ "$resolved_target" != "$EXPECTED" ]; then
  echo "Refusing to deploy: resolved target is $resolved_target, expected $EXPECTED" >&2
  exit 20
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "Dry run: path checks passed; no service stop, clear, or extract performed."
  exit 0
fi

sudo systemctl stop fortune-bot.timer 2>/dev/null || systemctl --user stop fortune-bot.timer 2>/dev/null || true
sudo systemctl stop fortune-bot.service 2>/dev/null || systemctl --user stop fortune-bot.service 2>/dev/null || true

mkdir -p "$TARGET"
find "$TARGET" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
tar -xzf "$BUNDLE" -C "$TARGET"

cd "$TARGET"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
fi
chmod 600 execution_engine/secrets.env 2>/dev/null || true

mkdir -p artifacts/logs/execution_engine/summaries artifacts/state/execution_engine
python -m execution_engine.verify_prd --artifact-dir execution_engine/deploy/early_trade_label_v1 --bundle "$BUNDLE"
python -m execution_engine.self_test --artifact-dir execution_engine/deploy/early_trade_label_v1 --bundle "$BUNDLE" --config config.yaml
python -m execution_engine.preflight --config config.yaml --mode paper --print-json
python -m execution_engine.prewarm --config config.yaml --print-json
python -m execution_engine.run_once --config config.yaml --mode paper --print-json
python -m execution_engine.observe_paper --config config.yaml --cycles 1 --sleep-seconds 0 --print-json

mkdir -p "$HOME/.config/systemd/user"
cp deploy/fortune-bot.service deploy/fortune-bot.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload || true
if [ "${SKIP_TIMER_START:-0}" = "1" ]
then
  echo "Timer start skipped by request."
fi
if [ "${SKIP_TIMER_START:-0}" != "1" ]
then
  systemctl --user enable --now fortune-bot.timer || true
fi
'@

if ($DryRun) {
  Write-Host "Running remote dry-run checks on $HostName..."
  $remoteScript | ssh $HostName "TARGET='$Target' DRY_RUN=1 bash -s"
  exit $LASTEXITCODE
}

Write-Host "Copying bundle to ${HostName}:$remoteBundle..."
scp $bundlePath "${HostName}:$remoteBundle"

$skip = if ($SkipTimerStart) { "1" } else { "0" }
Write-Host "Deploying to ${HostName}:$Target..."
$remoteScript | ssh $HostName "TARGET='$Target' SKIP_TIMER_START='$skip' bash -s"
exit $LASTEXITCODE

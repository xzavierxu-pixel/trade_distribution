param(
  [string]$HostName = "version3",
  [string]$Target = "~/fortune_bot",
  [string]$BackupPath = "",
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$remoteScript = @'
set -euo pipefail

TARGET="${TARGET:-$HOME/fortune_bot}"
case "$TARGET" in
  "~") TARGET="$HOME" ;;
  "~/"*) TARGET="$HOME/${TARGET:2}" ;;
esac
EXPECTED="$HOME/fortune_bot"
BACKUP="${BACKUP_PATH:-}"

resolved_parent="$(cd "$(dirname "$TARGET")" && pwd)"
resolved_target="$resolved_parent/$(basename "$TARGET")"
if [ "$resolved_target" != "$EXPECTED" ]; then
  echo "Refusing rollback: resolved target is $resolved_target, expected $EXPECTED" >&2
  exit 20
fi

if [ -z "$BACKUP" ]; then
  BACKUP="$(ls -1t "$HOME"/fortune_bot_backups/fortune_bot_*.tar.gz 2>/dev/null | head -n 1 || true)"
fi
if [ -z "$BACKUP" ] || [ ! -f "$BACKUP" ]; then
  echo "No backup found. Pass -BackupPath or create ~/fortune_bot_backups first." >&2
  exit 21
fi

echo "target=$TARGET"
echo "backup=$BACKUP"
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "Dry run: rollback checks passed; no files changed."
  exit 0
fi

sudo systemctl stop fortune-bot.timer 2>/dev/null || systemctl --user stop fortune-bot.timer 2>/dev/null || true
sudo systemctl stop fortune-bot.service 2>/dev/null || systemctl --user stop fortune-bot.service 2>/dev/null || true

ts="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$HOME/fortune_bot_backups"
if [ -d "$TARGET" ]; then
  tar -czf "$HOME/fortune_bot_backups/fortune_bot_pre_rollback_$ts.tar.gz" -C "$HOME" fortune_bot
fi

mkdir -p "$TARGET"
find "$TARGET" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
tar -xzf "$BACKUP" -C "$HOME"
cd "$TARGET"
if [ -f config.yaml ]; then
  python -m execution_engine.prewarm --config config.yaml --print-json || true
  python -m execution_engine.run_once --config config.yaml --mode paper --print-json || true
fi
systemctl --user start fortune-bot.timer 2>/dev/null || true
'@

$dry = if ($DryRun) { "1" } else { "0" }
$backup = $BackupPath.Replace("'", "'\''")
$targetEscaped = $Target.Replace("'", "'\''")
$remoteScript | ssh $HostName "TARGET='$targetEscaped' BACKUP_PATH='$backup' DRY_RUN='$dry' bash -s"
exit $LASTEXITCODE

#!/bin/bash
# 在 Mac mini 上运行：安装/重启 LaunchAgent（登录后自动启动，崩溃自动拉起）。
# 卸载：launchctl bootout gui/$(id -u)/local.mailowl && rm ~/Library/LaunchAgents/local.mailowl.plist
set -euo pipefail
APP="$HOME/Developer/apps/MailOwl"
LABEL="local.mailowl"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/.local/share/mailowl/logs"
mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"

"$APP/.venv/bin/mailowl" check || { echo "mailowl check 未全部通过，先修好配置再启动服务"; exit 1; }

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP/.venv/bin/mailowl</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$APP</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOGDIR/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>LANG</key><string>en_US.UTF-8</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>DISABLE_AUTOUPDATER</key><string>1</string>
    <key>CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC</key><string>1</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
# bootout 是异步的：旧进程没退干净就 bootstrap 会报 "Bootstrap failed: 5"
for _ in $(seq 1 20); do launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break; sleep 0.5; done
launchctl bootstrap "gui/$(id -u)" "$PLIST" || { sleep 3; launchctl bootstrap "gui/$(id -u)" "$PLIST"; }
sleep 3
launchctl print "gui/$(id -u)/$LABEL" | grep -E "state|pid" | head -3
echo "日志：tail -f $LOGDIR/mailowl.log"

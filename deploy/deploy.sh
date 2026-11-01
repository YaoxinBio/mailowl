#!/bin/bash
# 在 MBP 上运行：把代码同步到 Mac mini，装依赖、跑测试；带 --restart 时重启后台服务。
# 用法：deploy/deploy.sh [--restart] [主机名，默认 macmini]
set -euo pipefail
RESTART=0
if [ "${1:-}" = "--restart" ]; then RESTART=1; shift; fi
HOST="${1:-macmini}"
cd "$(dirname "$0")/.."

rsync -a --delete \
  --exclude .venv --exclude .git --exclude tmp --exclude __pycache__ \
  --exclude .pytest_cache --exclude '*.egg-info' \
  ./ "$HOST:Developer/apps/MailOwl/"

ssh "$HOST" RESTART="$RESTART" 'bash -s' <<'EOS'
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
cd ~/Developer/apps/MailOwl
command -v uv >/dev/null || { echo "Mac mini 上缺少 uv（~/.local/bin/uv）"; exit 1; }
[ -x .venv/bin/python ] || uv venv -q --python 3.12 .venv
uv pip install -q --python .venv/bin/python -e '.[test]'
.venv/bin/python -m pytest -q
[ -f ~/.config/mailowl/config.toml ] || .venv/bin/mailowl init
if [ "$RESTART" = 1 ]; then bash deploy/install-launchd.sh; fi
EOS

#!/bin/bash
# 安装 / 重启后台服务（等同于 `mailowl service install`），供 deploy.sh --restart 调用。
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/mailowl service install "$@"

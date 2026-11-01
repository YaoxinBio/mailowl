#!/bin/bash
# 安装 / 重启后台服务，供 deploy.sh --restart 调用。
# 先跑一遍 check 给出提示；但不因为检查失败而拒绝重启——部署前已跑过测试，
# 检查失败多半是某个邮箱网络抖动，服务本身会自动重试。首次安装请手动运行 `mailowl service install`。
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/mailowl check || echo "⚠️  检查有失败项（多半是网络抖动），仍然重启服务；服务会自动重试。"
exec .venv/bin/mailowl service install --no-check "$@"

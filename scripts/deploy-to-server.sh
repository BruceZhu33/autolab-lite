#!/usr/bin/env bash
# Mac 开发主位:commit 之后一条命令同时发布 GitHub(origin)并部署到服务器(server)。
# 服务器(gpu-server:~/autolab-lite)已配 receive.denyCurrentBranch=updateInstead:
# 推送会直接更新服务器工作区(前提:服务器工作区干净,别在服务器上留未提交改动)。
#
# 用法: scripts/deploy-to-server.sh [分支]   # 缺省 = 当前分支
set -euo pipefail
cd "$(dirname "$0")/.."

BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD)}"
git push origin "${BRANCH}"
git push server "${BRANCH}"
echo "已发布 GitHub 并部署到 gpu-server:~/autolab-lite(${BRANCH})"

#!/usr/bin/env bash
# 服务器(gpu-server:~/autolab-lite)为开发主位,Mac 只做推送位:
# 拉取服务器当前分支的最新提交 → 快进本地 → 发布到 GitHub(origin)。
#
# 用法:
#   scripts/publish-from-server.sh             # 同步并推送 GitHub
#   scripts/publish-from-server.sh --no-push   # 只同步到本地,不发布(先看 diff)
#
# 反方向(GitHub 有新提交要带回服务器):
#   git pull origin <分支> && git push server <分支>   # 服务器已配 updateInstead
set -euo pipefail
cd "$(dirname "$0")/.."

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git fetch server

# 只允许快进:与服务器分叉时停下来人工看,不自动制造 merge commit
if ! git merge --ff-only "server/${BRANCH}"; then
  echo "本地与 server/${BRANCH} 分叉,请人工处理(git log --oneline server/${BRANCH} ^HEAD)" >&2
  exit 1
fi

NEW="$(git rev-list --count server/${BRANCH} --not --remotes=origin 2>/dev/null || echo '?')"
echo "server/${BRANCH} 已同步到本地,领先 GitHub ${NEW} 个提交"

if [[ "${1:-}" == "--no-push" ]]; then
  echo "(--no-push:未推送 GitHub)"
  exit 0
fi
git push origin "${BRANCH}"
echo "已发布 ${BRANCH} → GitHub(origin)"

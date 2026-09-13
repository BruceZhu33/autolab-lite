#!/usr/bin/env bash
# 由 schema.sql + sm_transitions.generated.sql 重新生成基线迁移（勿手改 0001）
# schema.sql = 手写权威 DDL；sm_transitions.generated.sql = 由 state-machine.yaml 生成（S11 机器化）
set -euo pipefail
cd "$(dirname "$0")"
python3 ../scripts/gen_sm_transitions.py >/dev/null 2>&1 || /tmp/autolab_venv/bin/python ../scripts/gen_sm_transitions.py >/dev/null
{
  echo "-- 0001_init.sql — 基线迁移（由 schema.sql + sm_transitions.generated.sql 生成，勿手改）"
  echo "-- 生成方式：bash migrations/generate.sh"
  echo "BEGIN IMMEDIATE;"
  cat ../schema.sql
  echo ""
  cat ../sm_transitions.generated.sql
  echo ""
  echo "INSERT INTO schema_migrations(version, name) VALUES (1, 'init');"
  echo "COMMIT;"
} > 0001_init.sql
echo "regenerated 0001_init.sql"

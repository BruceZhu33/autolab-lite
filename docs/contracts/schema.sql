-- =====================================================================
-- AutoLab · SQLite DDL 契约（第五契约）
-- 版本：v1.0-draft.6（第五轮评审修订版） · 2026-09-13
-- 基线：技术设计 v2.7（冻结）· 需求 v0.5.3 · 两轮复审处置（r2：S2/S3/S5/S6/S7/S10/S13 + ADD-5）
-- 范围：设计 §3 七组逻辑实体的全部物理表 + FK/CHECK/部分唯一索引/trigger
-- 标注约定：
--   [§3-xxx]  = 直接来自设计 §3 对应实体组
--   [GAP-n]   = §3 未显式列出、由设计其他章节或需求推导的实体（GAP-2/3/4/5 已获复审仲裁）
--   [BLOCKED-ADD-n] = 待设计 v2.8 增补提案冻结后落地（见 增补提案草案）
-- 会话级 PRAGMA（应用与测试 harness 启动时必须设置——复审 S11）：
--   PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;
--   PRAGMA busy_timeout=5000; PRAGMA synchronous=NORMAL;
-- 时长单位：预算全部为整数秒（_s）；计量误差容限在 plan 中声明（R02）。
-- 状态枚举同步义务：projects/runs/attempts 的 status CHECK 必须与 state-machine.yaml
-- 状态清单保持一致（tests/test_state_machine.py 机械校验）。
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. 迁移账本（迁移规范见 migrations/README.md）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 通用 API 幂等记录 [S8]：与业务写入同事务提交；同键异载荷→应用层判 IDEMPOTENCY_CONFLICT
CREATE TABLE api_idempotency (
  scope           TEXT NOT NULL,        -- 作用域：'api:{operationId}:{资源id}' 或具名（final_confirmation/budget_reduction）
  idempotency_key TEXT NOT NULL,
  request_hash    TEXT NOT NULL,
  status_code     INTEGER NOT NULL,
  response_json   TEXT NOT NULL CHECK (json_valid(response_json)),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (scope, idempotency_key)
);
CREATE TRIGGER trg_api_idempotency_no_mutation
BEFORE UPDATE ON api_idempotency BEGIN
  SELECT RAISE(ABORT, 'api_idempotency is insert-only');
END;
CREATE TRIGGER trg_api_idempotency_no_delete
BEFORE DELETE ON api_idempotency BEGIN
  SELECT RAISE(ABORT, 'api_idempotency is insert-only');
END;

-- =====================================================================
-- 组一 · 核心 [§3-核心]：servers / gpus / projects / project_gpus
-- =====================================================================

CREATE TABLE servers (
  id                   TEXT PRIMARY KEY,
  name                 TEXT NOT NULL UNIQUE,
  host                 TEXT NOT NULL,
  port                 INTEGER NOT NULL DEFAULT 22 CHECK (port BETWEEN 1 AND 65535),
  username             TEXT NOT NULL,
  auth_kind            TEXT NOT NULL CHECK (auth_kind IN ('password','key')),
  -- 凭据本体存平台钥匙串（D30/D48），库内只存引用 [GAP-1 已追认]
  credential_ref       TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'UNKNOWN'
                       CHECK (status IN ('UNKNOWN','REACHABLE','UNREACHABLE','DECOMMISSIONED')),
  agent_capabilities_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(agent_capabilities_json)),
  agent_schema_ver     TEXT,
  sandbox_policy_hash  TEXT,
  sudo_verified        INTEGER NOT NULL DEFAULT 0,  -- 三执行身份初始化前置（无 sudo=诊断模式）
  registered_at        TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen_at         TEXT,
  updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE gpus (
  id            TEXT PRIMARY KEY,
  server_id     TEXT NOT NULL REFERENCES servers(id) ON DELETE RESTRICT,
  gpu_index     INTEGER NOT NULL CHECK (gpu_index >= 0),
  model_name    TEXT NOT NULL,
  vram_mb       INTEGER NOT NULL CHECK (vram_mb > 0),
  status        TEXT NOT NULL DEFAULT 'UNKNOWN'
                CHECK (status IN ('UNKNOWN','IDLE','BUSY_PLATFORM','BUSY_EXTERNAL','UNREACHABLE','DRAINED')),
                -- BUSY_EXTERNAL = 外部进程占用显式标注不进队列（A03）
  extra_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra_json)),  -- 实时遥测（利用率/已用显存等，A1）
  UNIQUE (server_id, gpu_index)
);

CREATE TABLE projects (
  id                  TEXT PRIMARY KEY,
  name                TEXT NOT NULL UNIQUE,
  direction_text      TEXT NOT NULL DEFAULT '',
  base_source_kind    TEXT NOT NULL CHECK (base_source_kind IN ('github','local')),
  base_source_ref     TEXT NOT NULL,
  base_snapshot_id    TEXT REFERENCES code_snapshots(id) ON DELETE RESTRICT,
  current_protocol_id TEXT REFERENCES plan_protocols(id) ON DELETE RESTRICT,
  status              TEXT NOT NULL DEFAULT 'INIT'
                      CHECK (status IN ('INIT','ENV_SETUP','REVIEW_GATE','BASELINE_ACCEPTANCE','SEARCH',
                                        'CONFIRMATION_PREP','CONFIRMATION_RUNNING','TEST_RESULT_SEALED',
                                        'STOPPING','STOP_PENDING_CONFIRMATION','FINISHING','REPORTING','REPORTED','PAUSED')),
  state_revision      INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
  pause_reason        TEXT,
  paused_from_state   TEXT
                      CHECK (paused_from_state IS NULL OR paused_from_state IN
                        ('SEARCH','BASELINE_ACCEPTANCE','CONFIRMATION_PREP','CONFIRMATION_RUNNING','REPORTING')),
                      -- resume 目标枚举（S10：动态目标展开）
  outcome             TEXT CHECK (outcome IS NULL OR outcome IN ('completed','failed','cancelled','budget_exhausted','stopped')),
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- S2：项目当前协议/基线快照必须属于本项目
-- r3-S5：INSERT 只能初态——终态/中间态只能经状态机合法迁移到达（防绕过状态机的出生路径）
CREATE TRIGGER trg_projects_initial_state_only
BEFORE INSERT ON projects
WHEN NEW.status NOT IN ('INIT') BEGIN
  SELECT RAISE(ABORT, 'projects must be created in INIT; use state machine transitions (r3-S5)');
END;
CREATE TRIGGER trg_projects_version_closure
BEFORE INSERT ON projects
WHEN (NEW.current_protocol_id IS NOT NULL AND
      (SELECT project_id FROM plan_protocols WHERE id = NEW.current_protocol_id) != NEW.id)
  OR (NEW.base_snapshot_id IS NOT NULL AND
      (SELECT project_id FROM code_snapshots WHERE id = NEW.base_snapshot_id) != NEW.id)
BEGIN
  SELECT RAISE(ABORT, 'projects version closure violated: current_protocol/base_snapshot must belong to project (S2)');
END;
CREATE TRIGGER trg_projects_version_closure_upd
BEFORE UPDATE OF current_protocol_id, base_snapshot_id ON projects
WHEN (NEW.current_protocol_id IS NOT NULL AND
      (SELECT project_id FROM plan_protocols WHERE id = NEW.current_protocol_id) != NEW.id)
  OR (NEW.base_snapshot_id IS NOT NULL AND
      (SELECT project_id FROM code_snapshots WHERE id = NEW.base_snapshot_id) != NEW.id)
BEGIN
  SELECT RAISE(ABORT, 'projects version closure violated on update (S2)');
END;

-- 卡池：跨服务器勾卡（D15）；移除=停止派发+排空后移除（R11）
CREATE TABLE project_gpus (
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  gpu_id      TEXT NOT NULL REFERENCES gpus(id) ON DELETE RESTRICT,
  status      TEXT NOT NULL DEFAULT 'VALIDATING'
              CHECK (status IN ('VALIDATING','ACTIVE','DRAINING','REMOVED')),
  added_at    TEXT NOT NULL DEFAULT (datetime('now')),
  removed_at  TEXT,
  PRIMARY KEY (project_id, gpu_id)
);
CREATE TRIGGER trg_project_gpus_status_flow
BEFORE UPDATE OF status ON project_gpus
WHEN NOT (
     (OLD.status='VALIDATING' AND NEW.status IN ('ACTIVE','REMOVED'))
  OR (OLD.status='ACTIVE'      AND NEW.status IN ('DRAINING','REMOVED'))
  OR (OLD.status='DRAINING'    AND NEW.status='REMOVED')
  OR (OLD.status='REMOVED'     AND NEW.status='REMOVED')
) BEGIN
  SELECT RAISE(ABORT, 'project_gpus invalid status transition');
END;

-- =====================================================================
-- 组二 · 快照与版本 [§3-快照与版本]
-- =====================================================================

-- 代码快照：每 run 独立、运行期不可变（D40）
CREATE TABLE code_snapshots (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  commit_sha   TEXT NOT NULL,
  export_hash  TEXT NOT NULL,
  lfs_note     TEXT,
  parent_id    TEXT REFERENCES code_snapshots(id) ON DELETE RESTRICT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (project_id, export_hash)
);
CREATE TRIGGER trg_code_snapshots_no_mutation
BEFORE UPDATE ON code_snapshots BEGIN
  SELECT RAISE(ABORT, 'code_snapshots is append-only (D40)');
END;
CREATE TRIGGER trg_code_snapshots_no_delete
BEFORE DELETE ON code_snapshots BEGIN
  SELECT RAISE(ABORT, 'code_snapshots is append-only (D40)');
END;
-- r3-S1/r4-S1：快照父链闭包（NULL 安全）：自引用拒绝、必须存在、同项目
CREATE TRIGGER trg_code_snapshots_parent_closure
BEFORE INSERT ON code_snapshots
WHEN NEW.parent_id IS NOT NULL AND (
     NEW.parent_id = NEW.id
  OR NOT EXISTS (SELECT 1 FROM code_snapshots c WHERE c.id = NEW.parent_id AND c.project_id = NEW.project_id)
)
BEGIN
  SELECT RAISE(ABORT, 'code_snapshots.parent invalid: self-ref/cross-project/missing (D40/r4-S1)');
END;

-- 环境版本：身份冻结，仅状态推进白名单（S3/D40）
CREATE TABLE environment_versions (
  id          TEXT PRIMARY KEY,
  server_id   TEXT NOT NULL REFERENCES servers(id) ON DELETE RESTRICT,
  ver         INTEGER NOT NULL CHECK (ver > 0),
  spec_hash   TEXT NOT NULL,
  spec_json   TEXT NOT NULL CHECK (json_valid(spec_json)),
  env_name    TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'PLANNED'
              CHECK (status IN ('PLANNED','INSTALLING','READY','FAILED')),
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  ready_at    TEXT,
  UNIQUE (server_id, ver),
  UNIQUE (server_id, spec_hash)
);
CREATE TRIGGER trg_environment_versions_frozen
BEFORE UPDATE ON environment_versions
WHEN NEW.id != OLD.id OR NEW.server_id != OLD.server_id OR NEW.ver != OLD.ver
  OR NEW.spec_hash != OLD.spec_hash OR NEW.spec_json != OLD.spec_json
  OR NEW.env_name != OLD.env_name OR NEW.created_at != OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'environment_versions identity is frozen (D40)');
END;
CREATE TRIGGER trg_environment_versions_status_flow
BEFORE UPDATE OF status ON environment_versions
WHEN NOT (
     (OLD.status='PLANNED'    AND NEW.status IN ('INSTALLING','FAILED'))
  OR (OLD.status='INSTALLING' AND NEW.status IN ('READY','FAILED'))
  OR (OLD.status='FAILED'     AND NEW.status='INSTALLING')
) BEGIN
  SELECT RAISE(ABORT, 'environment_versions invalid status transition');
END;
CREATE TRIGGER trg_environment_versions_no_delete
BEFORE DELETE ON environment_versions BEGIN
  SELECT RAISE(ABORT, 'environment_versions is append-only (D40)');
END;

-- 数据版本：append-only（D39）
CREATE TABLE data_versions (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  logical_name  TEXT NOT NULL,
  manifest_sha  TEXT NOT NULL,
  manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (project_id, manifest_sha)
);
CREATE TRIGGER trg_data_versions_no_mutation
BEFORE UPDATE ON data_versions BEGIN
  SELECT RAISE(ABORT, 'data_versions is append-only (D39)');
END;
CREATE TRIGGER trg_data_versions_no_delete
BEFORE DELETE ON data_versions BEGIN
  SELECT RAISE(ABORT, 'data_versions is append-only (D39)');
END;

-- 数据副本：SYNCING→VERIFYING→READY|FAILED（D39）；FAILED 可重校验
CREATE TABLE data_replicas (
  id              TEXT PRIMARY KEY,
  data_version_id TEXT NOT NULL REFERENCES data_versions(id) ON DELETE RESTRICT,
  server_id       TEXT NOT NULL REFERENCES servers(id) ON DELETE RESTRICT,
  logical_path    TEXT NOT NULL,
  actual_path     TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'SYNCING'
                  CHECK (status IN ('SYNCING','VERIFYING','READY','FAILED')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (data_version_id, server_id)
);
CREATE TRIGGER trg_data_replicas_frozen
BEFORE UPDATE ON data_replicas
WHEN NEW.data_version_id != OLD.data_version_id OR NEW.server_id != OLD.server_id
  OR NEW.logical_path != OLD.logical_path
BEGIN
  SELECT RAISE(ABORT, 'data_replicas binding is frozen (D39)');
END;
-- r5-S7：副本出生只能 SYNCING（READY 必须经校验事务）
CREATE TRIGGER trg_data_replicas_born_syncing
BEFORE INSERT ON data_replicas
WHEN NEW.status != 'SYNCING' BEGIN
  SELECT RAISE(ABORT, 'data_replicas must be born SYNCING; READY only via validation (D39/r5-S7)');
END;
-- r5-S7：READY 迁移要求存在匹配当前 manifest_sha 的 PASSED 证据
CREATE TRIGGER trg_data_replicas_ready_needs_evidence
BEFORE UPDATE OF status ON data_replicas
WHEN NEW.status = 'READY' AND OLD.status != 'READY' AND NOT EXISTS (
  SELECT 1 FROM data_replica_validations v
  WHERE v.data_replica_id = OLD.id AND v.status = 'PASSED'
    AND v.manifest_sha = (SELECT manifest_sha FROM data_versions WHERE id = OLD.data_version_id))
BEGIN
  SELECT RAISE(ABORT, 'data_replicas READY requires matching-manifest PASSED validation (D39/r5-S7)');
END;
CREATE TRIGGER trg_data_replicas_status_flow
BEFORE UPDATE OF status ON data_replicas
WHEN NOT (
     (OLD.status='SYNCING'   AND NEW.status IN ('VERIFYING','FAILED'))
  OR (OLD.status='VERIFYING' AND NEW.status IN ('READY','FAILED'))
  OR (OLD.status='READY'     AND NEW.status='VERIFYING')   -- 重校验（revalidate）入口
  OR (OLD.status='FAILED'    AND NEW.status='VERIFYING')
) BEGIN
  SELECT RAISE(ABORT, 'data_replicas invalid status transition (D39)');
END;

CREATE TABLE data_replica_validations (
  id              TEXT PRIMARY KEY,
  data_replica_id TEXT NOT NULL REFERENCES data_replicas(id) ON DELETE RESTRICT,
  manifest_sha    TEXT NOT NULL,
  verifier_ver    TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('PASSED','FAILED')),
  checked_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (data_replica_id, verifier_ver, manifest_sha)
);

-- 实验协议（plan）：不可变版本链（D38）
CREATE TABLE plan_protocols (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  parent_id       TEXT REFERENCES plan_protocols(id) ON DELETE RESTRICT,
  ver             INTEGER NOT NULL CHECK (ver > 0),
  body_hash       TEXT NOT NULL,
  frozen_yaml     TEXT NOT NULL,
  effective_round INTEGER,
  status          TEXT NOT NULL DEFAULT 'DRAFT'
                  CHECK (status IN ('DRAFT','FROZEN','SUPERSEDED','INVALIDATED')),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  frozen_at       TEXT,
  UNIQUE (project_id, ver)
);
-- A6/S13：拆分为「状态流 trigger」与「身份/冻结元数据 trigger」——DRAFT→FROZEN 必须一次性写入
-- 非空 frozen_at 与 effective_round；仅改 status 不带冻结元数据 → ABORT（复审实测绕过点）
CREATE TRIGGER trg_plan_protocols_flow
BEFORE UPDATE OF status ON plan_protocols
WHEN NOT (
     (OLD.status = 'DRAFT'   AND NEW.status = 'FROZEN'
        AND NEW.frozen_at IS NOT NULL AND NEW.effective_round IS NOT NULL)
  OR (OLD.status = 'DRAFT'   AND NEW.status = 'INVALIDATED'
        AND NEW.frozen_at IS NULL AND NEW.effective_round IS NULL)
  OR (OLD.status = 'FROZEN'  AND NEW.status IN ('SUPERSEDED','INVALIDATED'))
) BEGIN
  SELECT RAISE(ABORT, 'plan_protocols invalid status flow (D38): DRAFT->FROZEN requires frozen_at + effective_round');
END;
CREATE TRIGGER trg_plan_protocols_immutable
BEFORE UPDATE ON plan_protocols
WHEN NEW.project_id != OLD.project_id OR NEW.parent_id != OLD.parent_id
  OR NEW.ver != OLD.ver OR NEW.body_hash != OLD.body_hash
  OR NEW.frozen_yaml != OLD.frozen_yaml OR NEW.created_at != OLD.created_at
  -- frozen_at/effective_round 仅允许在 DRAFT→FROZEN 时从 NULL 写入；此后逐列冻结
  OR NOT (
       (OLD.status = 'DRAFT' AND NEW.status = 'FROZEN')
       OR (NEW.frozen_at IS OLD.frozen_at AND NEW.effective_round IS OLD.effective_round)
     )
BEGIN
  SELECT RAISE(ABORT, 'plan_protocols body/freeze-metadata is immutable (D38/S13)');
END;
CREATE TRIGGER trg_plan_protocols_no_delete
BEFORE DELETE ON plan_protocols BEGIN
  SELECT RAISE(ABORT, 'plan_protocols is append-only (D38)');
END;
-- r3-S1/r4-S1：版本父链闭包（NULL 安全）：自引用拒绝、必须存在、同项目、父 ver 严格小于子 ver
CREATE TRIGGER trg_plan_protocols_parent_closure
BEFORE INSERT ON plan_protocols
WHEN NEW.parent_id IS NOT NULL AND (
     NEW.parent_id = NEW.id
  OR NOT EXISTS (SELECT 1 FROM plan_protocols p WHERE p.id = NEW.parent_id AND p.project_id = NEW.project_id)
  OR (SELECT ver FROM plan_protocols WHERE id = NEW.parent_id) >= NEW.ver
)
BEGIN
  SELECT RAISE(ABORT, 'plan_protocols.parent invalid: self-ref/cross-project/missing/ver-not-increasing (D38/r4-S1)');
END;

-- 控制纪元：控制类变更按 control_epoch 分轨（U4 竞态裁决④）
CREATE TABLE control_state (
  project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE RESTRICT,
  epoch      INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TRIGGER trg_control_state_epoch_monotonic
BEFORE UPDATE ON control_state
WHEN NEW.epoch != OLD.epoch + 1 BEGIN
  SELECT RAISE(ABORT, 'control_state.epoch must increase by exactly 1');
END;

CREATE TABLE control_events (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  epoch_from  INTEGER NOT NULL,
  epoch_to    INTEGER NOT NULL CHECK (epoch_to = epoch_from + 1),
  change_json TEXT NOT NULL CHECK (json_valid(change_json)),
  actor       TEXT NOT NULL,
  occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- =====================================================================
-- 组三 · 执行 [§3-执行]
-- =====================================================================

-- 训练配方：不可变（R10）
CREATE TABLE training_recipes (
  id              TEXT PRIMARY KEY,
  budget_ep       INTEGER NOT NULL CHECK (budget_ep > 0),
  pretrain_src    TEXT NOT NULL DEFAULT '',
  optimizer       TEXT NOT NULL,
  augment         TEXT NOT NULL DEFAULT '',
  precision       TEXT NOT NULL,
  selection_rule  TEXT NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TRIGGER trg_training_recipes_no_mutation
BEFORE UPDATE ON training_recipes BEGIN
  SELECT RAISE(ABORT, 'training_recipes is immutable (R10)');
END;
CREATE TRIGGER trg_training_recipes_no_delete
BEFORE DELETE ON training_recipes BEGIN
  SELECT RAISE(ABORT, 'training_recipes is immutable (R10)');
END;

-- 初始化规格：不可变（D35）
CREATE TABLE initialization_specs (
  id                  TEXT PRIMARY KEY,
  kind                TEXT NOT NULL CHECK (kind IN ('from_scratch','parent_ckpt','pretrained')),
  parent_attempt_id   TEXT REFERENCES attempts(id) ON DELETE RESTRICT,
  ckpt_hash           TEXT,
  load_report         TEXT NOT NULL,
  optimizer_state_ref TEXT,
  cum_steps           INTEGER NOT NULL DEFAULT 0,
  seed_train          INTEGER NOT NULL,
  seed_infer          INTEGER NOT NULL,
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (kind != 'parent_ckpt' OR (parent_attempt_id IS NOT NULL AND ckpt_hash IS NOT NULL)),
  CHECK (kind = 'from_scratch' OR ckpt_hash IS NOT NULL)
);
CREATE TRIGGER trg_initialization_specs_no_mutation
BEFORE UPDATE ON initialization_specs BEGIN
  SELECT RAISE(ABORT, 'initialization_specs is immutable (D35)');
END;
CREATE TRIGGER trg_initialization_specs_no_delete
BEFORE DELETE ON initialization_specs BEGIN
  SELECT RAISE(ABORT, 'initialization_specs is immutable (D35)');
END;

-- runs：六绑定身份冻结（S3）；状态枚举与 state-machine.yaml 同步（S10）
-- 候选身份由 candidates.selected_run_id 表达（D37），role 不设 'candidate'——避免
-- runs↔candidates 互相引用在立即 FK 下不可插入（复审期间发现的建模修正）
CREATE TABLE runs (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  round_no          INTEGER NOT NULL CHECK (round_no >= 0),
  role              TEXT NOT NULL CHECK (role IN ('baseline','search','ablation','multi_seed')),
  plan_protocol_id  TEXT NOT NULL REFERENCES plan_protocols(id) ON DELETE RESTRICT,
  code_snapshot_id  TEXT NOT NULL REFERENCES code_snapshots(id) ON DELETE RESTRICT,
  env_version_id    TEXT NOT NULL REFERENCES environment_versions(id) ON DELETE RESTRICT,
  data_version_id   TEXT NOT NULL REFERENCES data_versions(id) ON DELETE RESTRICT,
  recipe_id         TEXT NOT NULL REFERENCES training_recipes(id) ON DELETE RESTRICT,
  init_spec_id      TEXT NOT NULL REFERENCES initialization_specs(id) ON DELETE RESTRICT,
  candidate_id      TEXT REFERENCES candidates(id) ON DELETE RESTRICT,  -- 为该候选执行的批跑 run 可反向标注
  control_epoch     INTEGER NOT NULL,
  status            TEXT NOT NULL DEFAULT 'PLANNED'
                    CHECK (status IN ('PLANNED','ANALYZING','PATCHING','SMOKING','REPAIRING','LLM_BACKOFF',
                                      'SNAPSHOTTING','READY_TO_DISPATCH','QUEUED','TRAINING','EVAL_QUEUED',
                                      'INFRA_RETRY','METRIC_MISSING','JUDGED')),
  state_revision    INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
  resume_state      TEXT,
  fail_class        TEXT CHECK (fail_class IS NULL OR fail_class IN ('model','infra','metric','llm')),
  parent_run_id     TEXT REFERENCES runs(id) ON DELETE RESTRICT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TRIGGER trg_runs_identity_frozen
BEFORE UPDATE ON runs
WHEN NEW.project_id != OLD.project_id OR NEW.round_no != OLD.round_no OR NEW.role != OLD.role
  OR NEW.plan_protocol_id != OLD.plan_protocol_id OR NEW.code_snapshot_id != OLD.code_snapshot_id
  OR NEW.env_version_id != OLD.env_version_id OR NEW.data_version_id != OLD.data_version_id
  OR NEW.recipe_id != OLD.recipe_id OR NEW.init_spec_id != OLD.init_spec_id
  OR NEW.candidate_id IS NOT OLD.candidate_id OR NEW.control_epoch != OLD.control_epoch
  OR NEW.parent_run_id IS NOT OLD.parent_run_id OR NEW.created_at != OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'runs six-way binding is frozen after creation (D38/D39/D40/D45)');
END;
-- S2 关系闭包：run 的 protocol / code snapshot / data version 必须属于同一项目；
-- candidate（如反向标注）必须挂本 run 的协议
CREATE TRIGGER trg_runs_initial_state_only
BEFORE INSERT ON runs
WHEN NEW.status NOT IN ('PLANNED') BEGIN
  SELECT RAISE(ABORT, 'runs must be created in PLANNED; use state machine transitions (r3-S5)');
END;
-- r3-S1：版本闭包（protocol/snapshot/data/parent-ckpt 同项目）
CREATE TRIGGER trg_runs_project_closure
BEFORE INSERT ON runs
WHEN
     (SELECT project_id FROM plan_protocols WHERE id = NEW.plan_protocol_id) != NEW.project_id
  OR (SELECT project_id FROM code_snapshots WHERE id = NEW.code_snapshot_id) != NEW.project_id
  OR (SELECT project_id FROM data_versions WHERE id = NEW.data_version_id) != NEW.project_id
  OR (NEW.candidate_id IS NOT NULL AND
      (SELECT proto_id FROM candidates WHERE id = NEW.candidate_id) != NEW.plan_protocol_id)
  OR (SELECT r2.project_id FROM initialization_specs i
      LEFT JOIN attempts pa ON pa.id = i.parent_attempt_id
      LEFT JOIN runs r2 ON r2.id = pa.run_id
      WHERE i.id = NEW.init_spec_id) NOT IN (NEW.project_id)
  OR (NEW.parent_run_id IS NOT NULL AND (
       NEW.parent_run_id = NEW.id
    OR NOT EXISTS (SELECT 1 FROM runs pr WHERE pr.id = NEW.parent_run_id AND pr.project_id = NEW.project_id)))
BEGIN
  SELECT RAISE(ABORT, 'runs closure violated: protocol/snapshot/data/parent-ckpt must be same project (S2/r3-S1)');
END;
CREATE INDEX idx_runs_project_status ON runs(project_id, status);
CREATE INDEX idx_runs_protocol ON runs(plan_protocol_id);

-- attempts：dispatch 幂等；身份冻结（S3）；infer/agg 必须挂 request 且同 run（S2）
CREATE TABLE attempts (
  id                    TEXT PRIMARY KEY,
  run_id                TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
  seq                   INTEGER NOT NULL CHECK (seq >= 1),
  dispatch_id           TEXT NOT NULL UNIQUE,          -- 派发幂等键（D31/U-B5）
  evaluation_request_id TEXT REFERENCES evaluation_requests(id) ON DELETE RESTRICT,
  server_id             TEXT NOT NULL REFERENCES servers(id) ON DELETE RESTRICT,
  gpu_id                TEXT REFERENCES gpus(id) ON DELETE RESTRICT,
  profile               TEXT NOT NULL CHECK (profile IN ('train','infer','agg')),
  exec_manifest_hash    TEXT NOT NULL,
  control_epoch         INTEGER NOT NULL,
  budget_reservation_id TEXT UNIQUE REFERENCES budget_reservations(id) ON DELETE RESTRICT,
  status                TEXT NOT NULL DEFAULT 'SUBMITTED'
                        CHECK (status IN ('SUBMITTED','ACKED','REMOTE_QUEUED','RUNNING','FINISHED','FAILED',
                                          'STOP_REQUESTED','STOP_SENT','STOP_CONFIRMED','ATTEMPT_STOP_UNKNOWN','CANCELLED')),
  state_revision        INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
  fail_class            TEXT CHECK (fail_class IS NULL OR fail_class IN ('model','infra','metric','llm')),
  fail_reason           TEXT,
  created_at            TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (run_id, seq),
  CHECK ((profile = 'train' AND evaluation_request_id IS NULL)
      OR (profile IN ('infer','agg') AND evaluation_request_id IS NOT NULL))
);
CREATE TRIGGER trg_attempts_identity_frozen
BEFORE UPDATE ON attempts
WHEN NEW.run_id != OLD.run_id OR NEW.seq != OLD.seq OR NEW.dispatch_id != OLD.dispatch_id
  OR NEW.evaluation_request_id IS NOT OLD.evaluation_request_id OR NEW.server_id != OLD.server_id
  OR NEW.gpu_id IS NOT OLD.gpu_id OR NEW.profile != OLD.profile
  OR NEW.exec_manifest_hash != OLD.exec_manifest_hash OR NEW.control_epoch != OLD.control_epoch
  OR NEW.created_at != OLD.created_at
  OR (NEW.budget_reservation_id IS NOT OLD.budget_reservation_id AND OLD.budget_reservation_id IS NOT NULL)
      -- 预算授权可延迟绑定，但仅允许 NULL→值 一次（设计 §3 "budget_reservation_id NULL可延迟"）
BEGIN
  SELECT RAISE(ABORT, 'attempts identity is frozen after creation (S3/U-B5)');
END;
-- S2 同源：infer/agg attempt 必须挂在 evaluation_request 自己的 run 上
CREATE TRIGGER trg_attempts_initial_state_only
BEFORE INSERT ON attempts
WHEN NEW.status NOT IN ('SUBMITTED') BEGIN
  SELECT RAISE(ABORT, 'attempts must be created in SUBMITTED; use T-INGEST transitions (r3-S5)');
END;
CREATE TRIGGER trg_attempts_same_run_as_request
BEFORE INSERT ON attempts
WHEN NEW.evaluation_request_id IS NOT NULL
  AND (SELECT er.run_id FROM evaluation_requests er WHERE er.id = NEW.evaluation_request_id) != NEW.run_id
BEGIN
  SELECT RAISE(ABORT, 'infer/agg attempt must belong to the run of its evaluation_request (S2 closure)');
END;
CREATE TRIGGER trg_attempts_same_run_as_request_upd
BEFORE UPDATE OF evaluation_request_id, run_id ON attempts
WHEN NEW.evaluation_request_id IS NOT NULL
  AND (SELECT er.run_id FROM evaluation_requests er WHERE er.id = NEW.evaluation_request_id) != NEW.run_id
BEGIN
  SELECT RAISE(ABORT, 'infer/agg attempt must belong to the run of its evaluation_request (S2 closure)');
END;
CREATE INDEX idx_attempts_run ON attempts(run_id);
CREATE INDEX idx_attempts_status ON attempts(status);
-- 每 evaluation_request 至多一个"成功"infer/agg（仅成功态唯一，重试合法，U-B7）
CREATE UNIQUE INDEX uq_attempts_eval_success
  ON attempts(evaluation_request_id, profile)
  WHERE evaluation_request_id IS NOT NULL AND status = 'FINISHED';

-- 产物：受保护产物清理保护（A06）。[BLOCKED-ADD-3：project 级归属泛化待增补冻结]
CREATE TABLE artifacts (
  id          TEXT PRIMARY KEY,
  attempt_id  TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
  kind        TEXT NOT NULL CHECK (kind IN ('ckpt','log','metrics_file','predictions','diff','manifest','report','chart','repro_package','other')),
  path        TEXT NOT NULL,
  sha256      TEXT NOT NULL,
  protected   INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0,1)),
  size_bytes  INTEGER,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (attempt_id, kind, path)
);

-- =====================================================================
-- 组四 · 科研身份与批次 [§3-科研身份与批次]（D36/D45/U-B1）
-- =====================================================================

CREATE TABLE modules (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  identity        TEXT NOT NULL,
  idea_text       TEXT NOT NULL DEFAULT '',
  code_refs_json  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(code_refs_json)),
  status          TEXT NOT NULL DEFAULT 'PROPOSED'
                  CHECK (status IN ('PROPOSED','REVIEWING','ACCEPTED','REALIZED','EVOLVED_OUT','REJECTED')),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (project_id, identity)
);
-- [BLOCKED-ADD-4：review_threads/review_turns/review_confirmations 待增补 ADD-4 冻结]

-- 候选：选择身份冻结 + 状态白名单（S3/D37）
CREATE TABLE candidates (
  id                  TEXT PRIMARY KEY,
  proto_id            TEXT NOT NULL REFERENCES plan_protocols(id) ON DELETE RESTRICT,
  selection_rule_ver  TEXT NOT NULL,
  selected_run_id     TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
  status              TEXT NOT NULL DEFAULT 'PROPOSED'
                      CHECK (status IN ('PROPOSED','SELECTED','CONFIRMING','CONFIRMED','CONFIRMATION_FAILED','INVALIDATED')),
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  selected_at         TEXT
);
CREATE TRIGGER trg_candidates_identity_frozen
BEFORE UPDATE ON candidates
WHEN NEW.proto_id != OLD.proto_id OR NEW.selection_rule_ver != OLD.selection_rule_ver
  OR NEW.selected_run_id != OLD.selected_run_id OR NEW.created_at != OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'candidates selection identity is frozen (D37：不依测试分数换候选)');
END;
CREATE TRIGGER trg_candidates_status_flow
BEFORE UPDATE OF status ON candidates
WHEN NOT (
     (OLD.status='PROPOSED'  AND NEW.status IN ('SELECTED','INVALIDATED'))
  OR (OLD.status='SELECTED'  AND NEW.status IN ('CONFIRMING','INVALIDATED'))
  OR (OLD.status='CONFIRMING' AND NEW.status IN ('CONFIRMED','CONFIRMATION_FAILED','INVALIDATED'))
) BEGIN
  SELECT RAISE(ABORT, 'candidates invalid status transition');
END;
-- S5 闭包：候选的 selected_run 必须属于其协议的项目，且 run 绑定该协议
CREATE TRIGGER trg_candidates_closure
BEFORE INSERT ON candidates
WHEN (SELECT r.project_id FROM runs r WHERE r.id = NEW.selected_run_id)
    != (SELECT p.project_id FROM plan_protocols p WHERE p.id = NEW.proto_id)
  OR (SELECT r.plan_protocol_id FROM runs r WHERE r.id = NEW.selected_run_id) != NEW.proto_id
BEGIN
  SELECT RAISE(ABORT, 'candidates closure violated: selected_run must bind same protocol & project (D37/S5)');
END;

CREATE TABLE ablation_matrices (
  id         TEXT PRIMARY KEY,
  proto_id   TEXT NOT NULL REFERENCES plan_protocols(id) ON DELETE RESTRICT,
  version    INTEGER NOT NULL CHECK (version > 0),
  body_hash  TEXT NOT NULL,
  frozen_at  TEXT,
  status     TEXT NOT NULL DEFAULT 'DRAFT'
             CHECK (status IN ('DRAFT','REVALIDATING','NEEDS_CONFIRMATION','FROZEN','SUPERSEDED')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (proto_id, version)
);
CREATE TRIGGER trg_ablation_matrices_frozen
BEFORE UPDATE ON ablation_matrices
WHEN NEW.proto_id != OLD.proto_id OR NEW.version != OLD.version OR NEW.body_hash != OLD.body_hash
  OR NEW.created_at != OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'ablation_matrices body is frozen (D36)');
END;
CREATE TRIGGER trg_ablation_matrices_status_flow
BEFORE UPDATE OF status ON ablation_matrices
WHEN NOT (
     (OLD.status='DRAFT'             AND NEW.status IN ('REVALIDATING','FROZEN','SUPERSEDED'))
  OR (OLD.status='REVALIDATING'      AND NEW.status IN ('NEEDS_CONFIRMATION','FROZEN','SUPERSEDED'))
  OR (OLD.status='NEEDS_CONFIRMATION' AND NEW.status IN ('FROZEN','SUPERSEDED'))
  OR (OLD.status='FROZEN'            AND NEW.status='SUPERSEDED')
) BEGIN
  SELECT RAISE(ABORT, 'ablation_matrices invalid status transition');
END;

CREATE TABLE ablation_items (
  id               TEXT PRIMARY KEY,
  matrix_id        TEXT NOT NULL REFERENCES ablation_matrices(id) ON DELETE RESTRICT,
  module_identity  TEXT NOT NULL,
  config_json      TEXT NOT NULL CHECK (json_valid(config_json)),
  status           TEXT NOT NULL DEFAULT 'PENDING'
                   CHECK (status IN ('PENDING','RUNNABLE','NOT_RUNNABLE','RUNNING','DONE','FAILED')),
  fail_reason      TEXT
);
CREATE TRIGGER trg_ablation_items_frozen
BEFORE UPDATE ON ablation_items
WHEN NEW.matrix_id != OLD.matrix_id OR NEW.module_identity != OLD.module_identity
  OR NEW.config_json != OLD.config_json
BEGIN
  SELECT RAISE(ABORT, 'ablation_items config is frozen (D36：不能运行的组合显式标注原因，不伪填)');
END;
CREATE TRIGGER trg_ablation_items_status_flow
BEFORE UPDATE OF status ON ablation_items
WHEN NOT (
     (OLD.status='PENDING'   AND NEW.status IN ('RUNNABLE','NOT_RUNNABLE','FAILED'))
  OR (OLD.status='RUNNABLE'  AND NEW.status IN ('RUNNING','FAILED'))
  OR (OLD.status='RUNNING'   AND NEW.status IN ('DONE','FAILED'))
) BEGIN
  SELECT RAISE(ABORT, 'ablation_items invalid status transition');
END;

CREATE TABLE ablation_item_runs (
  item_id TEXT NOT NULL REFERENCES ablation_items(id) ON DELETE RESTRICT,
  run_id  TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
  PRIMARY KEY (item_id, run_id)
);

-- 批次：五绑定身份冻结 + 状态白名单（S3/U-B1/D45）
CREATE TABLE batches (
  id                TEXT PRIMARY KEY,
  candidate_id      TEXT NOT NULL REFERENCES candidates(id) ON DELETE RESTRICT,
  plan_protocol_id  TEXT NOT NULL REFERENCES plan_protocols(id) ON DELETE RESTRICT,
  recipe_id         TEXT NOT NULL REFERENCES training_recipes(id) ON DELETE RESTRICT,
  matrix_id         TEXT NOT NULL REFERENCES ablation_matrices(id) ON DELETE RESTRICT,
  init_spec_id      TEXT NOT NULL REFERENCES initialization_specs(id) ON DELETE RESTRICT,
  status            TEXT NOT NULL DEFAULT 'FROZEN'
                    CHECK (status IN ('FROZEN','RUNNING','COMPLETED','PARTIAL','FAILED','CANCELLED')),
  -- r3-S2/r4-S2：冻结比较 spec 在批次冻结时固化且**必须源自冻结 plan**（T-FREEZE-BATCH 提取）
  comparison_spec_json TEXT NOT NULL CHECK (json_valid(comparison_spec_json)),
  comparison_spec_hash TEXT NOT NULL,
  comparison_spec_source_hash TEXT NOT NULL,  -- = 冻结 plan.body_hash（可追溯权威来源）
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  frozen_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TRIGGER trg_batches_identity_frozen
BEFORE UPDATE ON batches
WHEN NEW.candidate_id != OLD.candidate_id OR NEW.plan_protocol_id != OLD.plan_protocol_id
  OR NEW.recipe_id != OLD.recipe_id OR NEW.matrix_id != OLD.matrix_id
  OR NEW.init_spec_id != OLD.init_spec_id OR NEW.created_at != OLD.created_at
  OR NEW.frozen_at != OLD.frozen_at
  OR NEW.comparison_spec_json IS NOT OLD.comparison_spec_json
  OR NEW.comparison_spec_hash != OLD.comparison_spec_hash
  OR NEW.comparison_spec_source_hash != OLD.comparison_spec_source_hash
BEGIN
  SELECT RAISE(ABORT, 'batches five-way binding + frozen comparison spec are immutable (U-B1/D45/r3-S2)');
END;
CREATE TRIGGER trg_batches_status_flow
BEFORE UPDATE OF status ON batches
WHEN NOT (
     (OLD.status='FROZEN'  AND NEW.status IN ('RUNNING','CANCELLED'))
  OR (OLD.status='RUNNING' AND NEW.status IN ('COMPLETED','PARTIAL','FAILED','CANCELLED'))
) BEGIN
  SELECT RAISE(ABORT, 'batches invalid status transition');
END;
-- S5 五绑定闭包：candidate.proto = batch.plan_protocol；matrix.proto 同协议；
-- init/recipe 属于候选 run 的绑定（同一套方法一致性，D45）
CREATE TRIGGER trg_batches_closure
BEFORE INSERT ON batches
WHEN (SELECT proto_id FROM candidates WHERE id = NEW.candidate_id) != NEW.plan_protocol_id
  OR (SELECT status FROM plan_protocols WHERE id = NEW.plan_protocol_id) != 'FROZEN'
  OR (SELECT body_hash FROM plan_protocols WHERE id = NEW.plan_protocol_id) != NEW.comparison_spec_source_hash
  OR (SELECT proto_id FROM ablation_matrices WHERE id = NEW.matrix_id) != NEW.plan_protocol_id
  OR (SELECT recipe_id FROM runs WHERE id = (SELECT selected_run_id FROM candidates WHERE id = NEW.candidate_id)) != NEW.recipe_id
  OR (SELECT init_spec_id FROM runs WHERE id = (SELECT selected_run_id FROM candidates WHERE id = NEW.candidate_id)) != NEW.init_spec_id
  -- r5-S2：来源 run 必须有已验收基线（D33：BASE 对照的权威来源）
  OR NOT EXISTS (SELECT 1 FROM runs br JOIN baseline_acceptances ba ON ba.run_id = br.id
                  WHERE br.project_id = (SELECT project_id FROM plan_protocols WHERE id = NEW.plan_protocol_id)
                    AND br.role = 'baseline' AND ba.verdict = 'accepted')
BEGIN
  SELECT RAISE(ABORT, 'batches five-binding closure violated: candidate/matrix/recipe/init must be consistent (U-B1/D45/S5)');
END;

CREATE TABLE batch_items (
  id             TEXT PRIMARY KEY,
  batch_id       TEXT NOT NULL REFERENCES batches(id) ON DELETE RESTRICT,
  run_id         TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
  expected_role  TEXT NOT NULL CHECK (expected_role IN ('candidate_seed','base_seed','ablation_item')),
  expected_seed  INTEGER NOT NULL,
  is_required    INTEGER NOT NULL DEFAULT 1 CHECK (is_required IN (0,1)),
                 -- S6：必要对照（候选/BASE/指定 seed）必须 SUCCEEDED 才可 final-read；
                 -- 非必要消融失败显式留空标注（A07），不阻断
  status         TEXT NOT NULL DEFAULT 'PENDING'
                 CHECK (status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED','MISSING')),
  fail_reason    TEXT,
  UNIQUE (batch_id, run_id)
);
CREATE TRIGGER trg_batch_items_frozen
BEFORE UPDATE ON batch_items
WHEN NEW.batch_id != OLD.batch_id OR NEW.run_id != OLD.run_id
  OR NEW.expected_role != OLD.expected_role OR NEW.expected_seed != OLD.expected_seed
  OR NEW.is_required != OLD.is_required
BEGIN
  SELECT RAISE(ABORT, 'batch_items expectation is frozen (A07：预期任务清单不可改)');
END;
-- r3-S8：条目出生必须 PENDING——SUCCEEDED/FAILED 只能经运行推进（服务端 T-FREEZE-BATCH 派生清单）
CREATE TRIGGER trg_batch_items_initial_state_only
BEFORE INSERT ON batch_items
WHEN NEW.status NOT IN ('PENDING') BEGIN
  SELECT RAISE(ABORT, 'batch_items must be created PENDING; derive via T-FREEZE-BATCH (r3-S8)');
END;
-- 闭包：条目 run 同项目同协议 + seed 必须来自冻结协议的预登记 confirm 组（r4-S7：D34 权威来源）
CREATE TRIGGER trg_batch_items_closure
BEFORE INSERT ON batch_items
WHEN (SELECT r.project_id FROM runs r WHERE r.id = NEW.run_id)
    != (SELECT p.project_id FROM plan_protocols p
        WHERE p.id = (SELECT plan_protocol_id FROM batches WHERE id = NEW.batch_id))
  OR (SELECT r.plan_protocol_id FROM runs r WHERE r.id = NEW.run_id)
    != (SELECT plan_protocol_id FROM batches WHERE id = NEW.batch_id)
  OR (NEW.expected_role IN ('candidate_seed','base_seed') AND NOT EXISTS (
      SELECT 1 FROM seeds_registered sr, json_each(sr.seed_set_json) je
      WHERE sr.proto_id = (SELECT plan_protocol_id FROM batches WHERE id = NEW.batch_id)
        AND sr.role = 'confirm' AND je.value = NEW.expected_seed))
  -- r5-S2：expected_role 与 run.role 匹配；expected_seed = 该 run 的 init_spec.seed_train（per-seed run 闭包）
  OR (SELECT r.role FROM runs r WHERE r.id = NEW.run_id) NOT IN
      (CASE NEW.expected_role WHEN 'candidate_seed' THEN 'multi_seed'
                              WHEN 'base_seed' THEN 'baseline' END)
  OR (SELECT i.seed_train FROM initialization_specs i, runs r
      WHERE i.id = r.init_spec_id AND r.id = NEW.run_id) != NEW.expected_seed
BEGIN
  SELECT RAISE(ABORT, 'batch_items closure: role/protocol/seed-pre-registration/run-role/init-seed (D34/D45/r5-S2)');
END;
CREATE TRIGGER trg_batch_items_status_flow
BEFORE UPDATE OF status ON batch_items
WHEN NOT (
     (OLD.status='PENDING' AND NEW.status IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED','MISSING'))
  OR (OLD.status='RUNNING' AND NEW.status IN ('SUCCEEDED','FAILED','CANCELLED'))
) BEGIN
  SELECT RAISE(ABORT, 'batch_items invalid status transition');
END;

-- seed 预登记（D34：先登记后使用；r5-S2：协议冻结后禁改）
CREATE TABLE seeds_registered (
  id            TEXT PRIMARY KEY,
  proto_id      TEXT NOT NULL REFERENCES plan_protocols(id) ON DELETE RESTRICT,
  role          TEXT NOT NULL CHECK (role IN ('train','infer','screening','confirm')),
  seed_set_json TEXT NOT NULL CHECK (json_valid(seed_set_json)
                                     AND json_type(seed_set_json) = 'array'),
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (proto_id, role)
);
CREATE TRIGGER trg_seeds_frozen_after_freeze
BEFORE UPDATE ON seeds_registered
WHEN (SELECT status FROM plan_protocols WHERE id = OLD.proto_id) IN ('FROZEN','SUPERSEDED','INVALIDATED')
BEGIN
  SELECT RAISE(ABORT, 'seeds_registered is frozen after protocol freeze (D34/r5-S2)');
END;

-- =====================================================================
-- 组五 · 评测 [§3-评测]（U-B2/U-B7/D29/D43 + 复审 S2 关系闭包）
-- =====================================================================

-- 数据划分：role 与 request.scope 一致（S2/D43）
CREATE TABLE splits (
  id                    TEXT PRIMARY KEY,
  proto_id              TEXT NOT NULL REFERENCES plan_protocols(id) ON DELETE RESTRICT,
  name                  TEXT NOT NULL,
  role                  TEXT NOT NULL CHECK (role IN ('train','val','test')),
  unit_rule             TEXT NOT NULL,
  preprocess_fit_scope  TEXT NOT NULL DEFAULT 'train_only'
                        CHECK (preprocess_fit_scope IN ('train_only','declared_exception')),
  UNIQUE (proto_id, name)
);

-- 划分有效性校验：guard 只读有效 PASSED（四元组绑定）
CREATE TABLE split_validations (
  id                TEXT PRIMARY KEY,
  plan_protocol_id  TEXT NOT NULL REFERENCES plan_protocols(id) ON DELETE RESTRICT,
  data_version_id   TEXT NOT NULL REFERENCES data_versions(id) ON DELETE RESTRICT,
  split_id          TEXT NOT NULL REFERENCES splits(id) ON DELETE RESTRICT,
  validator_version TEXT NOT NULL,
  status            TEXT NOT NULL CHECK (status IN ('RUNNING','PASSED','FAILED')),
  evidence_hash     TEXT,
  validated_at      TEXT,
  UNIQUE (plan_protocol_id, data_version_id, split_id, validator_version)
);
-- 同项目闭包（S2）：校验的四元组不得跨项目拼接
CREATE TRIGGER trg_split_validations_closure
BEFORE INSERT ON split_validations
WHEN (SELECT p.project_id FROM plan_protocols p WHERE p.id = NEW.plan_protocol_id)
   != (SELECT d.project_id FROM data_versions d WHERE d.id = NEW.data_version_id)
  OR (SELECT s.proto_id FROM splits s WHERE s.id = NEW.split_id) != NEW.plan_protocol_id
BEGIN
  SELECT RAISE(ABORT, 'split_validations closure violated: protocol/data/split must be same project (S2)');
END;

-- 评测身份快照 [GAP-2 已仲裁追认后修改]
CREATE TABLE evaluator_snapshots (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  digest      TEXT NOT NULL,
  frozen_yaml TEXT NOT NULL,
  spec_hash   TEXT NOT NULL,
  frozen_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (project_id, digest)
);
CREATE TRIGGER trg_evaluator_snapshots_no_mutation
BEFORE UPDATE ON evaluator_snapshots BEGIN
  SELECT RAISE(ABORT, 'evaluator_snapshots is append-only (D29)');
END;
CREATE TRIGGER trg_evaluator_snapshots_no_delete
BEFORE DELETE ON evaluator_snapshots BEGIN
  SELECT RAISE(ABORT, 'evaluator_snapshots is append-only (D29)');
END;

-- 评测请求：冻结完整评测身份；S2 关系闭包（同项目五方一致 + ckpt 属于本 run + sha 相等 + split role 一致）
CREATE TABLE evaluation_requests (
  id                      TEXT PRIMARY KEY,
  run_id                  TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
  scope                   TEXT NOT NULL CHECK (scope IN ('val','test')),
  plan_protocol_id        TEXT NOT NULL REFERENCES plan_protocols(id) ON DELETE RESTRICT,
  data_version_id         TEXT NOT NULL REFERENCES data_versions(id) ON DELETE RESTRICT,
  split_id                TEXT NOT NULL REFERENCES splits(id) ON DELETE RESTRICT,
  ckpt_artifact_id        TEXT NOT NULL REFERENCES artifacts(id) ON DELETE RESTRICT,
  ckpt_expected_sha256    TEXT NOT NULL,
  evaluator_snapshot_id   TEXT NOT NULL REFERENCES evaluator_snapshots(id) ON DELETE RESTRICT,
  evaluator_digest        TEXT NOT NULL,   -- 冗余列，trigger 强制 = snapshot.digest（§3 精神：冗余须与权威相等）
  ckpt_selection_rule_ver TEXT NOT NULL,
  status                  TEXT NOT NULL DEFAULT 'QUEUED'
                          CHECK (status IN ('QUEUED','INFER_PENDING','INFER_RUNNING','AGG_PENDING','AGG_RUNNING','COMPLETED','FAILED')),
  created_at              TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (run_id, scope, plan_protocol_id)
);
CREATE TRIGGER trg_evaluation_requests_closure
BEFORE INSERT ON evaluation_requests
WHEN
  -- S2：request 的协议/数据必须与 run 的冻结绑定逐字相等（不得另声版本份）
     (SELECT plan_protocol_id FROM runs WHERE id = NEW.run_id) != NEW.plan_protocol_id
  OR (SELECT data_version_id FROM runs WHERE id = NEW.run_id) != NEW.data_version_id
  -- 同项目五方一致
  OR (SELECT project_id FROM runs WHERE id = NEW.run_id)
     != (SELECT project_id FROM plan_protocols WHERE id = NEW.plan_protocol_id)
  OR (SELECT project_id FROM data_versions WHERE id = NEW.data_version_id)
     != (SELECT project_id FROM runs WHERE id = NEW.run_id)
  OR (SELECT proto_id FROM splits WHERE id = NEW.split_id) != NEW.plan_protocol_id
  OR (SELECT role FROM splits WHERE id = NEW.split_id) != NEW.scope
  -- ckpt：必须属于本 run 的 train attempt、kind=ckpt、受保护、sha 相等
  OR (SELECT a.run_id FROM attempts a WHERE a.id =
        (SELECT art.attempt_id FROM artifacts art WHERE art.id = NEW.ckpt_artifact_id)) != NEW.run_id
  OR (SELECT a.profile FROM attempts a WHERE a.id =
        (SELECT art.attempt_id FROM artifacts art WHERE art.id = NEW.ckpt_artifact_id)) != 'train'
  OR (SELECT art.kind FROM artifacts art WHERE art.id = NEW.ckpt_artifact_id) != 'ckpt'
  OR (SELECT art.protected FROM artifacts art WHERE art.id = NEW.ckpt_artifact_id) != 1
  OR (SELECT art.sha256 FROM artifacts art WHERE art.id = NEW.ckpt_artifact_id) != NEW.ckpt_expected_sha256
  -- evaluator：同项目且 digest 与快照权威相等
  OR (SELECT project_id FROM evaluator_snapshots WHERE id = NEW.evaluator_snapshot_id)
     != (SELECT project_id FROM runs WHERE id = NEW.run_id)
  OR (SELECT digest FROM evaluator_snapshots WHERE id = NEW.evaluator_snapshot_id) != NEW.evaluator_digest
BEGIN
  SELECT RAISE(ABORT, 'evaluation_requests identity closure violated: run binding/kind-ckpt/split-role/evaluator (S2/S3/U-B2)');
END;
-- 身份列冻结：仅 status/updated_at 可变
CREATE TRIGGER trg_evaluation_requests_identity_frozen
BEFORE UPDATE ON evaluation_requests
WHEN NEW.run_id != OLD.run_id OR NEW.scope != OLD.scope
  OR NEW.plan_protocol_id != OLD.plan_protocol_id OR NEW.data_version_id != OLD.data_version_id
  OR NEW.split_id != OLD.split_id OR NEW.ckpt_artifact_id != OLD.ckpt_artifact_id
  OR NEW.ckpt_expected_sha256 != OLD.ckpt_expected_sha256
  OR NEW.evaluator_snapshot_id != OLD.evaluator_snapshot_id
  OR NEW.evaluator_digest != OLD.evaluator_digest
  OR NEW.ckpt_selection_rule_ver != OLD.ckpt_selection_rule_ver
  OR NEW.created_at != OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'evaluation_requests identity columns are frozen once created (U-B2)');
END;
-- trg_evaluation_requests_status_flow 移至文件尾（引用 evaluation_results，后建表）
-- S3：正式指标写入门槛——agg 步 FINISHED、predictions 在案、request 处于 AGG 阶段
-- trg_evaluation_results_gate 移至文件尾（引用 predictions/attempts，后建表）
CREATE TRIGGER trg_evaluation_requests_no_delete
BEFORE DELETE ON evaluation_requests BEGIN
  SELECT RAISE(ABORT, 'evaluation_requests is append-only (audit)');
END;

-- 正式指标：一条 request×metric 唯一（U-B7）
CREATE TABLE evaluation_results (
  evaluation_request_id TEXT NOT NULL REFERENCES evaluation_requests(id) ON DELETE RESTRICT,
  metric                TEXT NOT NULL,
  value                 REAL NOT NULL,
  evidence_hash         TEXT NOT NULL,
  created_at            TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (evaluation_request_id, metric)
);
CREATE TRIGGER trg_evaluation_results_no_mutation
BEFORE UPDATE ON evaluation_results BEGIN
  SELECT RAISE(ABORT, 'evaluation_results is insert-only (U-B7)');
END;
CREATE TRIGGER trg_evaluation_results_no_delete
BEFORE DELETE ON evaluation_results BEGIN
  SELECT RAISE(ABORT, 'evaluation_results is insert-only (U-B7)');
END;

-- predictions：受控 artifact；infer_attempt 必须是同 request 的 infer attempt（S2）
CREATE TABLE predictions (
  id                    TEXT PRIMARY KEY,
  evaluation_request_id TEXT NOT NULL UNIQUE REFERENCES evaluation_requests(id) ON DELETE RESTRICT,
  infer_attempt_id      TEXT NOT NULL UNIQUE REFERENCES attempts(id) ON DELETE RESTRICT,
  sha256                TEXT NOT NULL,
  format_version        TEXT NOT NULL,
  sample_ids_hash       TEXT NOT NULL,
  row_count             INTEGER NOT NULL CHECK (row_count >= 0),
  created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TRIGGER trg_predictions_same_request
BEFORE INSERT ON predictions
WHEN (SELECT a.evaluation_request_id FROM attempts a WHERE a.id = NEW.infer_attempt_id) != NEW.evaluation_request_id
  OR (SELECT a.profile FROM attempts a WHERE a.id = NEW.infer_attempt_id) != 'infer'
BEGIN
  SELECT RAISE(ABORT, 'predictions.infer_attempt must be the infer attempt of the same evaluation_request (S2)');
END;
CREATE TRIGGER trg_evaluation_requests_status_flow
BEFORE UPDATE OF status ON evaluation_requests
WHEN NOT (
     (OLD.status='QUEUED'        AND NEW.status IN ('INFER_PENDING','FAILED'))
  OR (OLD.status='INFER_PENDING' AND NEW.status IN ('INFER_RUNNING','FAILED'))
  OR (OLD.status='INFER_RUNNING' AND NEW.status IN ('AGG_PENDING','FAILED'))
  OR (OLD.status='AGG_PENDING'   AND NEW.status IN ('AGG_RUNNING','COMPLETED','FAILED'))
  OR (OLD.status='AGG_RUNNING'   AND NEW.status IN ('COMPLETED','FAILED'))
) OR (
  -- S3：置 COMPLETED 的硬前提——受控 predictions 存在、FINISHED agg attempt 存在、至少一条正式结果
  NEW.status='COMPLETED' AND (
       NOT EXISTS (SELECT 1 FROM predictions p WHERE p.evaluation_request_id = NEW.id)
    OR NOT EXISTS (SELECT 1 FROM attempts a WHERE a.evaluation_request_id = NEW.id
                     AND a.profile='agg' AND a.status='FINISHED')
    OR NOT EXISTS (SELECT 1 FROM evaluation_results er WHERE er.evaluation_request_id = NEW.id)
  )
) BEGIN
  SELECT RAISE(ABORT, 'evaluation_requests invalid status transition or COMPLETED without agg provenance (S3)');
END;
CREATE TRIGGER trg_evaluation_results_gate
BEFORE INSERT ON evaluation_results
WHEN (SELECT status FROM evaluation_requests WHERE id = NEW.evaluation_request_id) != 'AGG_RUNNING'
  OR NOT EXISTS (SELECT 1 FROM attempts a WHERE a.evaluation_request_id = NEW.evaluation_request_id
                   AND a.profile='agg' AND a.status='FINISHED')
  OR NOT EXISTS (SELECT 1 FROM predictions p WHERE p.evaluation_request_id = NEW.evaluation_request_id)
  OR NEW.evidence_hash IS NULL OR NEW.evidence_hash = ''
BEGIN
  SELECT RAISE(ABORT, 'evaluation_results gate: requires AGG_RUNNING + FINISHED agg attempt + predictions (D29/S3)');
END;
CREATE TRIGGER trg_predictions_no_mutation
BEFORE UPDATE ON predictions BEGIN
  SELECT RAISE(ABORT, 'predictions is insert-only');
END;
CREATE TRIGGER trg_predictions_no_delete
BEFORE DELETE ON predictions BEGIN
  SELECT RAISE(ABORT, 'predictions is insert-only');
END;

-- Round 0 基线验收（D33）
CREATE TABLE baseline_acceptances (
  id          TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE RESTRICT,
  verdict     TEXT NOT NULL CHECK (verdict IN ('accepted','rejected')),
  patch_class TEXT NOT NULL DEFAULT 'none' CHECK (patch_class IN ('none','env_compat','method')),
  notes       TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  -- r3-S9：D33 库级强制——方法修改（Round 0 禁止）不得作为任何 acceptance 的正面结论
  CHECK (patch_class != 'method' OR verdict = 'rejected')
);

-- 文献对照证据（D42）
CREATE TABLE review_evidence (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  retrieval_mode  TEXT NOT NULL CHECK (retrieval_mode IN ('imported_only','external')),
  query           TEXT NOT NULL,
  sources_json    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(sources_json)),
  checked_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- =====================================================================
-- 组六 · 预算 [§3-预算]（U-B3/U-B4/V6）
-- =====================================================================

CREATE TABLE budget_accounts (
  project_id        TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE RESTRICT,
  limit_s           INTEGER NOT NULL CHECK (limit_s > 0),
  status            TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','DRAINING')),
                    -- [BLOCKED-ADD-1：TARGET_UNATTAINABLE 待增补冻结]
  requested_limit_s INTEGER,
  drain_started_at  TEXT,
  consumed_s        INTEGER NOT NULL DEFAULT 0 CHECK (consumed_s >= 0),
  reserved_s        INTEGER NOT NULL DEFAULT 0 CHECK (reserved_s >= 0),
  overage_s         INTEGER NOT NULL DEFAULT 0 CHECK (overage_s >= 0),
  revision          INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
  -- r4-S5：恢复冻结基线 C+R≤B 恒成立（D32）。真实用量的**流水**永远可入账（usage_events 无上限）；
  -- 账面超限观测进 overage_s（telemetry，不计入 C 不放宽不变量）——授权侧以 execution_cap+margin
  -- 计入 R（ADD-1 r4 条款），远端超额在本 CHECK 之下被记为 overage 而非放宽账本
  CHECK ((status = 'ACTIVE' AND requested_limit_s IS NULL)
      OR (status = 'DRAINING' AND requested_limit_s IS NOT NULL
          AND drain_started_at IS NOT NULL)),
  CHECK (requested_limit_s IS NULL OR (requested_limit_s > 0 AND requested_limit_s <= limit_s)),
  CHECK (consumed_s + reserved_s <= limit_s)
);

CREATE TABLE budget_reservations (
  id             TEXT PRIMARY KEY,
  attempt_id     TEXT NOT NULL UNIQUE REFERENCES attempts(id) ON DELETE RESTRICT,
  authorized_s   INTEGER NOT NULL CHECK (authorized_s > 0),  -- reserved_total = execution_cap + margin（R 占用）
  execution_cap_s INTEGER NOT NULL CHECK (execution_cap_s > 0),  -- r5-S4：远端可执行额度（manifest 携带）
  remaining_s   INTEGER NOT NULL CHECK (remaining_s >= 0 AND remaining_s <= authorized_s),
  status        TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE','STOPPING','SETTLED')),
  settle_reason TEXT CHECK (settle_reason IS NULL OR settle_reason IN ('completed','stop_confirmed','hard_deadline','expired','cancelled')),
  expires_at    TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
-- 对账防御触发器：账本先动、预留后动（transactions.md §0 规则 7）保证每步等式成立
CREATE TRIGGER trg_budget_reconcile_ins
AFTER INSERT ON budget_reservations BEGIN
  SELECT RAISE(ABORT, 'budget ledger mismatch: reserved_s != SUM(remaining_s) of unsettled reservations')
  WHERE (SELECT reserved_s FROM budget_accounts WHERE project_id =
         (SELECT r.project_id FROM runs r JOIN attempts a ON a.run_id = r.id WHERE a.id = NEW.attempt_id))
      != (SELECT COALESCE(SUM(b.remaining_s),0) FROM budget_reservations b
          JOIN attempts a2 ON a2.id = b.attempt_id
          JOIN runs r2 ON r2.id = a2.run_id
          WHERE b.status IN ('ACTIVE','STOPPING')
            AND r2.project_id =
                (SELECT r.project_id FROM runs r JOIN attempts a ON a.run_id = r.id WHERE a.id = NEW.attempt_id));
END;
CREATE TRIGGER trg_budget_reconcile_upd
AFTER UPDATE OF remaining_s, status ON budget_reservations BEGIN
  SELECT RAISE(ABORT, 'budget ledger mismatch: reserved_s != SUM(remaining_s) of unsettled reservations')
  WHERE (SELECT reserved_s FROM budget_accounts WHERE project_id =
         (SELECT r.project_id FROM runs r JOIN attempts a ON a.run_id = r.id WHERE a.id = NEW.attempt_id))
      != (SELECT COALESCE(SUM(b.remaining_s),0) FROM budget_reservations b
          JOIN attempts a2 ON a2.id = b.attempt_id
          JOIN runs r2 ON r2.id = a2.run_id
          WHERE b.status IN ('ACTIVE','STOPPING')
            AND r2.project_id =
                (SELECT r.project_id FROM runs r JOIN attempts a ON a.run_id = r.id WHERE a.id = NEW.attempt_id));
END;

-- usage 为 agent 自采样事件；(attempt, seq) 唯一 → 重放安全；累计值必须单调（r3-S6：
-- 乱序/回退采样不得折算消耗——20→10→30 的真实累计是 30 而非 40）
CREATE TABLE usage_events (
  attempt_id   TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
  seq          INTEGER NOT NULL CHECK (seq >= 1),
  cumulative_s INTEGER NOT NULL CHECK (cumulative_s >= 0),
  recorded_at  TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (attempt_id, seq)
);
CREATE TRIGGER trg_usage_monotonic
BEFORE INSERT ON usage_events
WHEN NEW.cumulative_s < COALESCE(
  (SELECT MAX(cumulative_s) FROM usage_events WHERE attempt_id = NEW.attempt_id), 0)
BEGIN
  SELECT RAISE(ABORT, 'usage cumulative_s must be monotonic (r3-S6: 乱序采样拒绝，不得折算消耗)');
END;

-- =====================================================================
-- 组七 · 失效与审计与基础设施 [§3-失效与审计与基础设施]（U-B7/V2）
-- =====================================================================

CREATE TABLE outbox (
  id                TEXT PRIMARY KEY,
  idempotency_key  TEXT NOT NULL UNIQUE,
  aggregate_type    TEXT NOT NULL CHECK (aggregate_type IN ('project','run','attempt','budget','evaluation','batch','sync','report','control')),
  aggregate_id      TEXT NOT NULL,
  op                TEXT NOT NULL,
  payload_json      TEXT NOT NULL CHECK (json_valid(payload_json)),
  status            TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENT','CONFIRMED','FAILED')),
  delivery_attempts INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  sent_at           TEXT
);
CREATE INDEX idx_outbox_pending ON outbox(status) WHERE status IN ('PENDING','SENT');
CREATE TRIGGER trg_outbox_update_guard
BEFORE UPDATE ON outbox
WHEN NEW.idempotency_key != OLD.idempotency_key OR NEW.aggregate_type != OLD.aggregate_type
  OR NEW.aggregate_id != OLD.aggregate_id OR NEW.op != OLD.op
  OR NEW.payload_json != OLD.payload_json OR NEW.created_at != OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'outbox payload is immutable; only delivery fields may change (TD15)');
END;

CREATE TABLE invalidation_edges (
  id                TEXT PRIMARY KEY,
  cause_type        TEXT NOT NULL CHECK (cause_type IN ('protocol','candidate','recipe','data_version','method')),
  cause_id          TEXT NOT NULL,
  invalidated_kind  TEXT NOT NULL CHECK (invalidated_kind IN ('baseline','candidate','evaluation_result','ablation_item','batch','split_validation')),
  invalidated_id    TEXT NOT NULL,
  revalidate_status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (revalidate_status IN ('PENDING','REVALIDATED','WAIVED')),
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (cause_type, cause_id, invalidated_kind, invalidated_id)
);

CREATE TABLE audit_events (
  id           TEXT PRIMARY KEY,
  actor        TEXT NOT NULL,
  action       TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
  t            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_audit_t ON audit_events(t);

-- test 结果一次性释放（库级保证）；S5：release_payload_json=canonical 固定响应体（幂等重放原样返回）
CREATE TABLE test_result_releases (
  id                   TEXT PRIMARY KEY,
  batch_id             TEXT NOT NULL UNIQUE REFERENCES batches(id) ON DELETE RESTRICT,
  idempotency_key      TEXT NOT NULL UNIQUE,
  release_payload_json TEXT NOT NULL CHECK (json_valid(release_payload_json)),
  release_payload_hash TEXT NOT NULL,
  outcome              TEXT NOT NULL CHECK (outcome IN ('completed','failed')),
  released_at          TEXT NOT NULL DEFAULT (datetime('now')),
  released_by          TEXT NOT NULL
);
CREATE TRIGGER trg_test_result_releases_no_mutation
BEFORE UPDATE ON test_result_releases BEGIN
  SELECT RAISE(ABORT, 'test_result_releases is insert-only (one-shot release)');
END;
CREATE TRIGGER trg_test_result_releases_no_delete
BEFORE DELETE ON test_result_releases BEGIN
  SELECT RAISE(ABORT, 'test_result_releases is insert-only (one-shot release)');
END;

-- 状态迁移留痕 [GAP-3 已追认]：唯一约束防同一迁移双写（复审建议）
CREATE TABLE state_events (
  id                TEXT PRIMARY KEY,
  aggregate_type    TEXT NOT NULL CHECK (aggregate_type IN ('project','run','attempt')),
  aggregate_id      TEXT NOT NULL,
  event             TEXT NOT NULL,
  from_state        TEXT NOT NULL,
  to_state          TEXT NOT NULL,
  actor             TEXT NOT NULL,
  reason_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(reason_json)),
  occurred_at       TEXT NOT NULL DEFAULT (datetime('now')),
  expected_revision INTEGER NOT NULL CHECK (expected_revision >= 0),
  -- 同一聚合同一 CAS 期望值的同一目标态只能写一次（重放双写 → ABORT）
  UNIQUE (aggregate_type, aggregate_id, to_state, expected_revision)
);
CREATE INDEX idx_state_events_agg ON state_events(aggregate_type, aggregate_id, occurred_at);
-- r5-S8：state_events 的 (layer, from, event, to) 必须是权威状态机的合法边——伪事件拒写
CREATE TRIGGER trg_state_events_legal_edge
BEFORE INSERT ON state_events
WHEN NOT EXISTS (SELECT 1 FROM sm_legal_transitions t
                  WHERE t.layer = NEW.aggregate_type
                    AND t.from_state = NEW.from_state AND t.event = NEW.event
                    AND t.to_state = NEW.to_state)
BEGIN
  SELECT RAISE(ABORT, 'state_events not a legal (layer,from,event,to) edge per state-machine.yaml (r5-S8)');
END;

-- agent 实例与收件箱 [GAP-4 已仲裁追认后修改：cursor 与副作用同事务推进见 T-INGEST]
CREATE TABLE agent_instances (
  id           TEXT PRIMARY KEY,
  server_id    TEXT NOT NULL REFERENCES servers(id) ON DELETE RESTRICT,
  agent_ver    TEXT NOT NULL,
  schema_ver   TEXT NOT NULL,
  started_at   TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen_at TEXT
);

CREATE TABLE agent_inbox (
  agent_instance_id TEXT NOT NULL REFERENCES agent_instances(id) ON DELETE RESTRICT,
  event_seq         INTEGER NOT NULL CHECK (event_seq >= 0),
  event_type        TEXT NOT NULL,
  event_json        TEXT NOT NULL CHECK (json_valid(event_json)),
  received_at       TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (agent_instance_id, event_seq)
);

CREATE TABLE agent_cursors (
  agent_instance_id TEXT PRIMARY KEY REFERENCES agent_instances(id) ON DELETE RESTRICT,
  last_seq          INTEGER NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
  updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
-- S10：快照重定位登记（T-INGEST-SNAPSHOT 先写本表再跳跃 cursor，trigger 验证登记在案）
CREATE TABLE agent_cursor_repositions (
  id                TEXT PRIMARY KEY,
  agent_instance_id TEXT NOT NULL REFERENCES agent_instances(id) ON DELETE RESTRICT,
  from_seq          INTEGER NOT NULL CHECK (from_seq >= 0),
  to_seq            INTEGER NOT NULL CHECK (to_seq > from_seq),
  snapshot_watermark INTEGER NOT NULL CHECK (snapshot_watermark >= to_seq),
  reason            TEXT NOT NULL DEFAULT 'cursor_expired_resync',
  occurred_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
-- cursor 单调：+1 前进（T-INGEST），或经登记的重定位跳跃（T-INGEST-SNAPSHOT）；禁止回退
CREATE TRIGGER trg_agent_cursors_monotonic
BEFORE UPDATE ON agent_cursors
WHEN (NEW.last_seq != OLD.last_seq + 1
      AND NOT EXISTS (SELECT 1 FROM agent_cursor_repositions r
                       WHERE r.agent_instance_id = NEW.agent_instance_id
                         AND r.from_seq = OLD.last_seq AND r.to_seq = NEW.last_seq))
   OR NEW.last_seq < OLD.last_seq
BEGIN
  SELECT RAISE(ABORT, 'agent_cursors must advance by +1 or via registered snapshot reposition (S10)');
END;

-- =====================================================================
-- 缺口补实体 [GAP-5 已仲裁：papers 追认；评审对话实体 → BLOCKED-ADD-4]
-- =====================================================================
CREATE TABLE papers (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  source_kind  TEXT NOT NULL CHECK (source_kind IN ('url','upload')),
  url          TEXT,
  file_path    TEXT,
  file_sha256  TEXT,
  title        TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
  parsed_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(parsed_json)),
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
-- [BLOCKED-ADD-3：reports 实体与 artifacts 归属泛化待增补冻结——当前阶段报告产物
--  挂 project 专用 attempt 之外无路径，Q05 完整落库随 ADD-3 解锁]

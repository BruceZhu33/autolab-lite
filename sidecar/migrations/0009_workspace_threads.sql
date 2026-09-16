-- 0009_workspace_threads.sql — Phase 3 评论驱动 Agent（RFC-002 / v0.2 §7）
-- 五表：work_items / threads / messages / agent_jobs / action_proposals
-- + review_turns 幂等 backfill（v0.2 §7.6 步骤 1–6）
-- 契约 schema.sql 暂无以下表（攒批修订，同 #2/#8 纪律）。
BEGIN IMMEDIATE;

-- ------------------------------------------------------------------
-- work_items（v0.2 §7.1；UI 概念后置，表预留）
-- ------------------------------------------------------------------
CREATE TABLE work_items (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  stage_key     TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'task',
  title         TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','doing','done','cancelled')),
  linked_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at   TEXT
);
CREATE INDEX idx_work_items_project ON work_items(project_id, stage_key);

-- ------------------------------------------------------------------
-- threads（v0.2 §7.2；懒创建：UNIQUE(project, subject_type, subject_id)）
-- ------------------------------------------------------------------
CREATE TABLE threads (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  subject_type TEXT NOT NULL CHECK (subject_type IN ('project','stage','run')),
  subject_id   TEXT NOT NULL,
  title        TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (project_id, subject_type, subject_id)
);

-- ------------------------------------------------------------------
-- messages（v0.2 §7.3；insert-only + client_key 幂等）
-- ------------------------------------------------------------------
CREATE TABLE messages (
  id            TEXT PRIMARY KEY,
  thread_id     TEXT NOT NULL REFERENCES threads(id) ON DELETE RESTRICT,
  client_key    TEXT,
  actor_type    TEXT NOT NULL CHECK (actor_type IN ('user','agent')),
  actor_id      TEXT NOT NULL DEFAULT 'local-user',
  kind          TEXT NOT NULL CHECK (kind IN ('comment','progress','question','rationale','result','decision')),
  content_md    TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
  reply_to_id   TEXT REFERENCES messages(id) ON DELETE SET NULL,
  agent_job_id  TEXT,
  edited_at     TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (thread_id, client_key)
);
CREATE INDEX idx_messages_thread ON messages(thread_id, created_at);
-- insert-only：可审计产出不允许改写（编辑功能引入时再议语义）
CREATE TRIGGER trg_messages_no_update
BEFORE UPDATE ON messages BEGIN
  SELECT RAISE(ABORT, 'messages is insert-only (v0.2 §3.2 可审计产出)');
END;
CREATE TRIGGER trg_messages_no_delete
BEFORE DELETE ON messages BEGIN
  SELECT RAISE(ABORT, 'messages is insert-only');
END;

-- ------------------------------------------------------------------
-- agent_jobs（v0.2 §7.4；进程内异步 + 重启 sweep）
-- ------------------------------------------------------------------
CREATE TABLE agent_jobs (
  id                    TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  thread_id             TEXT NOT NULL REFERENCES threads(id) ON DELETE RESTRICT,
  trigger_message_id    TEXT REFERENCES messages(id) ON DELETE SET NULL,
  agent_key             TEXT NOT NULL DEFAULT 'autolab-coordinator',
  status                TEXT NOT NULL DEFAULT 'QUEUED'
                        CHECK (status IN ('QUEUED','RUNNING','WAITING_USER','WAITING_APPROVAL',
                                          'COMPLETED','FAILED','CANCELLED')),
  context_snapshot_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(context_snapshot_json)),
  error_json            TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
  started_at            TEXT,
  finished_at           TEXT,
  created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_agent_jobs_project ON agent_jobs(project_id, created_at);
-- 完成态不可复活（终态吸收，防迟到回调改历史）
CREATE TRIGGER trg_agent_jobs_terminal_absorb
BEFORE UPDATE OF status ON agent_jobs
WHEN OLD.status IN ('COMPLETED','FAILED','CANCELLED') AND NEW.status != OLD.status BEGIN
  SELECT RAISE(ABORT, 'agent_jobs terminal state is final');
END;

-- messages.agent_job_id 后挂外键（agent_jobs 已建）
-- SQLite 无法 ALTER ADD CONSTRAINT：由应用层保证（写入顺序 job→message）。

-- ------------------------------------------------------------------
-- action_proposals（v0.2 §7.5；状态机落库，执行=Phase 4 接现有 tx）
-- ------------------------------------------------------------------
CREATE TABLE action_proposals (
  id                       TEXT PRIMARY KEY,
  agent_job_id             TEXT REFERENCES agent_jobs(id) ON DELETE SET NULL,
  project_id               TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  action_type              TEXT NOT NULL,
  title                    TEXT NOT NULL,
  rationale_md             TEXT NOT NULL DEFAULT '',
  payload_json             TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
  impact_json              TEXT NOT NULL DEFAULT '{"level":"low"}' CHECK (json_valid(impact_json)),
  status                   TEXT NOT NULL DEFAULT 'PROPOSED'
                           CHECK (status IN ('PROPOSED','APPROVED','REJECTED','EXECUTING',
                                             'SUCCEEDED','FAILED','STALE')),
  expected_project_revision INTEGER NOT NULL DEFAULT 0,
  executed_result_json     TEXT CHECK (executed_result_json IS NULL OR json_valid(executed_result_json)),
  created_at               TEXT NOT NULL DEFAULT (datetime('now')),
  decided_at               TEXT,
  UNIQUE (agent_job_id, action_type, created_at)
);

-- ------------------------------------------------------------------
-- review_turns 幂等 backfill（v0.2 §7.6；可重复执行）
-- ------------------------------------------------------------------
-- 1) 每项目建 stage/review thread
INSERT OR IGNORE INTO threads(id, project_id, subject_type, subject_id, title)
SELECT 'th-rt-' || p.id, p.id, 'stage', 'review', '评审对话（review_turns 迁入）'
FROM projects p;
-- 2-6) 逐轮映射：user→comment / assistant→rationale；legacy key=rt-{id} 幂等
INSERT OR IGNORE INTO messages(id, thread_id, client_key, actor_type, actor_id, kind,
                               content_md, metadata_json, created_at)
SELECT 'msg-rt-' || rt.id,
       'th-rt-' || rt.project_id,
       'rt-' || rt.id,
       CASE WHEN rt.role = 'user' THEN 'user' ELSE 'agent' END,
       CASE WHEN rt.role = 'user' THEN 'local-user' ELSE 'autolab-coordinator' END,
       CASE WHEN rt.role = 'user' THEN 'comment' ELSE 'rationale' END,
       COALESCE(json_extract(rt.content_json, '$.text'),
                json_extract(rt.content_json, '$.content'),
                rt.content_json),
       json_object('legacy_source', 'review_turns', 'legacy_id', rt.id),
       rt.created_at
FROM review_turns rt;

INSERT INTO schema_migrations(version, name) VALUES (9, 'workspace_threads');
COMMIT;

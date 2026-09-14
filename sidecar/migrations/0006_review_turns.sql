-- 0006_review_turns.sql — 评审对话留痕（ADD-4 简化版，deviation #26）
-- 契约 review_threads/review_turns/review_confirmations 为 BLOCKED-ADD-4；
-- M3 落地最小集：对话轮次（挂 module 或整体汇总）+ 定稿确认记录。
BEGIN IMMEDIATE;
CREATE TABLE review_turns (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  module_id   TEXT REFERENCES modules(id) ON DELETE CASCADE,
  stage       TEXT NOT NULL CHECK (stage IN ('module_draft','module_refine','synthesis')),
  role        TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content_json TEXT NOT NULL CHECK (json_valid(content_json)),
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_review_turns_project ON review_turns(project_id, created_at);

CREATE TABLE review_confirmations (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE RESTRICT,
  confirmed_by TEXT NOT NULL DEFAULT 'user',
  conclusions_json TEXT NOT NULL CHECK (json_valid(conclusions_json)),
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO schema_migrations(version, name) VALUES (6, 'review_turns');
COMMIT;

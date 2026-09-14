-- 0007_engine_flag.sql — 提案引擎选择（builtin 自研 | rdagent 真身，TD6'/D11）
BEGIN IMMEDIATE;
ALTER TABLE llm_settings ADD COLUMN engine TEXT NOT NULL DEFAULT 'builtin'
  CHECK (engine IN ('builtin','rdagent'));
INSERT INTO schema_migrations(version, name) VALUES (7, 'engine_flag');
COMMIT;

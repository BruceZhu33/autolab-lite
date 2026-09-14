-- 0005_llm_settings.sql — LLM 使用点配置（D11/P7；契约 openapi 有端点无表，攒批修订 #19）
-- 密钥本体存凭据存储（D30：库内不存明文），表内只存引用。
BEGIN IMMEDIATE;
CREATE TABLE llm_settings (
  use_point  TEXT PRIMARY KEY CHECK (use_point IN ('round_analysis','self_repair','review','report')),
  base_url   TEXT NOT NULL,
  model      TEXT NOT NULL,
  api_key_ref TEXT NOT NULL,
  params_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(params_json)),
  enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO schema_migrations(version, name) VALUES (5, 'llm_settings');
COMMIT;

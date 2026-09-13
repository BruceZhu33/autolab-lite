-- 0002_server_transport.sql — 服务器传输层扩展（M1）
-- 契约 schema.sql 暂无以下列（deviation-log #2 攒批修订）：
--   transport_mode: 'ssh'（生产，D14）| 'local'（开发/单机纵切：sidecar 与 agent 同机直连）
--   agent_port:     gpu-agent 监听的 loopback 端口（SSH 隧道/直连的远端目标）
--   agent_token_ref: agent 共享令牌的钥匙串引用（凭据本体不入库）
BEGIN IMMEDIATE;
ALTER TABLE servers ADD COLUMN transport_mode TEXT NOT NULL DEFAULT 'ssh'
  CHECK (transport_mode IN ('ssh','local'));
ALTER TABLE servers ADD COLUMN agent_port INTEGER
  CHECK (agent_port IS NULL OR (agent_port BETWEEN 1 AND 65535));
ALTER TABLE servers ADD COLUMN agent_token_ref TEXT;
INSERT INTO schema_migrations(version, name) VALUES (2, 'server_transport');
COMMIT;

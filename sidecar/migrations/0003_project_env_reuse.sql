-- 0003_project_env_reuse.sql — 立项可声明复用服务器上已有的 conda 环境
-- 动机：M1 实测发现目标 GPU 服务器无出网（conda create / pip install 不可用），
-- 需允许项目绑定机器上现成环境（D20 场景延伸：检测到即用 → 复用即用）。
-- 契约 schema.sql 无此列（deviation-log #14 攒批修订）。
BEGIN IMMEDIATE;
ALTER TABLE projects ADD COLUMN env_reuse_name TEXT;
INSERT INTO schema_migrations(version, name) VALUES (3, 'project_env_reuse');
COMMIT;

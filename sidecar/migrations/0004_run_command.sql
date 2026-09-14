-- 0004_run_command.sql — run 持久化训练命令
-- 动机（M2-1 实测发现）：创建时自定义命令若未随 run 落库，预算不足等延迟派发场景
-- 会退回 plan 模板命令（deviation-log #18 攒批修订）。
BEGIN IMMEDIATE;
ALTER TABLE runs ADD COLUMN command TEXT;
INSERT INTO schema_migrations(version, name) VALUES (4, 'run_command');
COMMIT;

-- 0008_stage_flip.sql — 阶段顺序翻转：基线验证先于方案评审（RFC-001 / deviation #34、#37）
-- 契约 schema.sql 暂无以下列（攒批修订，同 #2/#14/#18 纪律）：
--   stage_epoch: 2 = 新序（setup_done→BASELINE_ACCEPTANCE→accept→REVIEW_GATE→confirm(F5)→SEARCH）
--                1 = 存量旧序（setup_done→REVIEW_GATE→confirm→BASELINE_ACCEPTANCE→accept→SEARCH）
-- 顺序裁决在应用层：tx_freeze / tx_baseline_acceptance / env_setup 按 stage_epoch 分流。
-- 新建项目默认 2；存量项目回填 1 沿旧序走完（RFC §9 方案 A：双序并存，不强制映射）。
--
-- 库级合法边表补新序 4 边（INSERT OR IGNORE 幂等：全新库的 0001 已含全量边，此段 no-op；
-- 已部署库的 0001 是旧 129 边版本，此处补齐——旧边保留供 epoch=1 存量项目使用）。
BEGIN IMMEDIATE;
ALTER TABLE projects ADD COLUMN stage_epoch INTEGER NOT NULL DEFAULT 2
  CHECK (stage_epoch IN (1, 2));
UPDATE projects SET stage_epoch = 1;

INSERT OR IGNORE INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES
  ('project', 'ENV_SETUP', 'setup_done', 'BASELINE_ACCEPTANCE'),
  ('project', 'BASELINE_ACCEPTANCE', 'accept', 'REVIEW_GATE'),
  ('project', 'REVIEW_GATE', 'confirm', 'SEARCH'),
  ('project', 'REVIEW_GATE', 'baseline_redo', 'BASELINE_ACCEPTANCE');
INSERT INTO schema_migrations(version, name) VALUES (8, 'stage_flip');
COMMIT;

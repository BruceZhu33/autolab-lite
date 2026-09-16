-- =====================================================================
-- AutoLab · 状态机合法迁移表（自动生成，勿手改）
-- 生成自 state-machine.yaml v3：python3 scripts/gen_sm_transitions.py
-- 语义：库级状态流兜底（S11）——非法 from→to 迁移在 UPDATE 时 ABORT；
--       guard 与乐观锁仍由应用层 S-CAS（transactions.md §1）负责。
-- =====================================================================
CREATE TABLE IF NOT EXISTS sm_legal_transitions (
  layer       TEXT NOT NULL CHECK (layer IN ('project','run','attempt')),
  from_state  TEXT NOT NULL,
  event       TEXT NOT NULL,
  to_state    TEXT NOT NULL,
  PRIMARY KEY (layer, from_state, event, to_state)
);
DELETE FROM sm_legal_transitions;
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'ACKED', 'remote_start', 'REMOTE_QUEUED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'ACKED', 'snapshot_reconciled', 'FAILED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'ACKED', 'snapshot_reconciled', 'FINISHED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'ACKED', 'snapshot_reconciled', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'ACKED', 'stop', 'STOP_REQUESTED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'ATTEMPT_STOP_UNKNOWN', 'reconciled_failed', 'FAILED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'ATTEMPT_STOP_UNKNOWN', 'reconciled_running', 'RUNNING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'ATTEMPT_STOP_UNKNOWN', 'reconciled_stopped', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'ATTEMPT_STOP_UNKNOWN', 'reconciled_terminal', 'FINISHED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'ATTEMPT_STOP_UNKNOWN', 'snapshot_reconciled', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'REMOTE_QUEUED', 'deadline_exceeded_ack', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'REMOTE_QUEUED', 'deadline_expired', 'ATTEMPT_STOP_UNKNOWN');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'REMOTE_QUEUED', 'running', 'RUNNING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'REMOTE_QUEUED', 'snapshot_reconciled', 'FAILED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'REMOTE_QUEUED', 'snapshot_reconciled', 'FINISHED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'REMOTE_QUEUED', 'snapshot_reconciled', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'REMOTE_QUEUED', 'stop', 'STOP_REQUESTED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'RUNNING', 'deadline_exceeded_ack', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'RUNNING', 'deadline_expired', 'ATTEMPT_STOP_UNKNOWN');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'RUNNING', 'failed', 'FAILED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'RUNNING', 'finished', 'FINISHED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'RUNNING', 'snapshot_reconciled', 'FAILED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'RUNNING', 'snapshot_reconciled', 'FINISHED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'RUNNING', 'snapshot_reconciled', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'RUNNING', 'stop', 'STOP_REQUESTED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'RUNNING', 'stop_sig_exit', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_REQUESTED', 'agent_unreachable', 'ATTEMPT_STOP_UNKNOWN');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_REQUESTED', 'deadline_exceeded_ack', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_REQUESTED', 'deadline_expired', 'ATTEMPT_STOP_UNKNOWN');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_REQUESTED', 'failed', 'FAILED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_REQUESTED', 'finished', 'FINISHED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_REQUESTED', 'snapshot_reconciled', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_REQUESTED', 'stop_sent', 'STOP_SENT');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_SENT', 'agent_unreachable', 'ATTEMPT_STOP_UNKNOWN');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_SENT', 'deadline_exceeded_ack', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_SENT', 'deadline_expired', 'ATTEMPT_STOP_UNKNOWN');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_SENT', 'failed', 'FAILED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_SENT', 'finished', 'FINISHED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_SENT', 'snapshot_reconciled', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'STOP_SENT', 'stop_ack', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'SUBMITTED', 'ack', 'ACKED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'SUBMITTED', 'cancel', 'CANCELLED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'SUBMITTED', 'snapshot_reconciled', 'FAILED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'SUBMITTED', 'snapshot_reconciled', 'FINISHED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'SUBMITTED', 'snapshot_reconciled', 'STOP_CONFIRMED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('attempt', 'SUBMITTED', 'stop', 'STOP_REQUESTED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'BASELINE_ACCEPTANCE', 'accept', 'REVIEW_GATE');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'BASELINE_ACCEPTANCE', 'accept', 'SEARCH');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'BASELINE_ACCEPTANCE', 'pause', 'PAUSED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'BASELINE_ACCEPTANCE', 'reject', 'PAUSED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'BASELINE_ACCEPTANCE', 'terminate', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'BASELINE_ACCEPTANCE', 'terminate', 'STOPPING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'CONFIRMATION_PREP', 'batch_frozen', 'CONFIRMATION_RUNNING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'CONFIRMATION_PREP', 'matrix_revision_required', 'PAUSED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'CONFIRMATION_PREP', 'terminate', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'CONFIRMATION_PREP', 'terminate', 'STOPPING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'CONFIRMATION_RUNNING', 'all_required_results_present', 'TEST_RESULT_SEALED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'CONFIRMATION_RUNNING', 'protocol_or_method_changed', 'PAUSED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'CONFIRMATION_RUNNING', 'terminate', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'CONFIRMATION_RUNNING', 'terminate', 'STOPPING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'ENV_SETUP', 'setup_done', 'BASELINE_ACCEPTANCE');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'ENV_SETUP', 'setup_done', 'REVIEW_GATE');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'ENV_SETUP', 'terminate', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'ENV_SETUP', 'terminate', 'STOPPING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'FINISHING', 'report_requested', 'REPORTING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'FINISHING', 'terminate', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'INIT', 'start_env_setup', 'ENV_SETUP');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'INIT', 'terminate', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'INIT', 'terminate', 'STOPPING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'apply_control_revision', 'BASELINE_ACCEPTANCE');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'apply_control_revision', 'CONFIRMATION_PREP');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'apply_control_revision', 'CONFIRMATION_RUNNING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'apply_control_revision', 'REPORTING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'apply_control_revision', 'SEARCH');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'apply_method_revision', 'CONFIRMATION_PREP');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'apply_method_revision', 'SEARCH');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'apply_protocol_revision', 'BASELINE_ACCEPTANCE');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'apply_protocol_revision', 'REVIEW_GATE');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'resume', 'BASELINE_ACCEPTANCE');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'resume', 'CONFIRMATION_PREP');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'resume', 'CONFIRMATION_RUNNING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'resume', 'REPORTING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'resume', 'SEARCH');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'revise', 'REVIEW_GATE');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'revise', 'SEARCH');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'terminate', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'PAUSED', 'terminate', 'STOPPING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'REPORTED', 'terminate', 'REPORTED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'REPORTING', 'report_done', 'REPORTED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'REPORTING', 'report_failed', 'REPORTING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'REPORTING', 'report_failed_escalate', 'PAUSED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'REPORTING', 'terminate', 'REPORTING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'REVIEW_GATE', 'baseline_redo', 'BASELINE_ACCEPTANCE');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'REVIEW_GATE', 'confirm', 'BASELINE_ACCEPTANCE');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'REVIEW_GATE', 'confirm', 'SEARCH');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'REVIEW_GATE', 'terminate', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'REVIEW_GATE', 'terminate', 'STOPPING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'SEARCH', 'candidate_selected', 'CONFIRMATION_PREP');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'SEARCH', 'pause', 'PAUSED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'SEARCH', 'terminate', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'SEARCH', 'terminate', 'STOPPING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'STOPPING', 'agent_unreachable', 'STOP_PENDING_CONFIRMATION');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'STOPPING', 'all_attempts_terminal_or_stop_confirmed', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'STOPPING', 'terminate', 'STOPPING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'STOP_PENDING_CONFIRMATION', 'reconciled_running', 'STOPPING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'STOP_PENDING_CONFIRMATION', 'reconciled_stopped', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'STOP_PENDING_CONFIRMATION', 'still_unreachable', 'STOP_PENDING_CONFIRMATION');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'STOP_PENDING_CONFIRMATION', 'terminate', 'STOP_PENDING_CONFIRMATION');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'TEST_RESULT_SEALED', 'final_read', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'TEST_RESULT_SEALED', 'terminate', 'FINISHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('project', 'TEST_RESULT_SEALED', 'terminate', 'STOPPING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'ANALYZING', 'llm_error', 'LLM_BACKOFF');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'ANALYZING', 'patch_generated', 'PATCHING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'EVAL_QUEUED', 'metric_missing', 'METRIC_MISSING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'EVAL_QUEUED', 'trusted_metrics_stored', 'JUDGED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'INFRA_RETRY', 'redispatched', 'READY_TO_DISPATCH');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'LLM_BACKOFF', 'backoff_ok', 'ANALYZING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'LLM_BACKOFF', 'backoff_ok', 'REPAIRING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'METRIC_MISSING', 'metric_repaired', 'EVAL_QUEUED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'PATCHING', 'smoke_started', 'SMOKING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'PLANNED', 'analysis_started', 'ANALYZING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'QUEUED', 'attempt_running', 'TRAINING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'QUEUED', 'infra_fail', 'INFRA_RETRY');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'READY_TO_DISPATCH', 'dispatch_ready', 'QUEUED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'REPAIRING', 'llm_error', 'LLM_BACKOFF');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'REPAIRING', 'model_fail', 'REPAIRING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'REPAIRING', 'smoke_passed', 'SNAPSHOTTING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'SMOKING', 'model_fail', 'REPAIRING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'SMOKING', 'smoke_passed', 'SNAPSHOTTING');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'SNAPSHOTTING', 'snapshot_committed', 'READY_TO_DISPATCH');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'TRAINING', 'attempt_finished', 'EVAL_QUEUED');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'TRAINING', 'infra_fail', 'INFRA_RETRY');
INSERT INTO sm_legal_transitions(layer, from_state, event, to_state) VALUES ('run', 'TRAINING', 'model_fail', 'REPAIRING');

CREATE TRIGGER IF NOT EXISTS trg_projects_status_flow
BEFORE UPDATE OF status ON projects
WHEN NEW.status != OLD.status AND NOT EXISTS (
  SELECT 1 FROM sm_legal_transitions
  WHERE layer = 'project' AND from_state = OLD.status AND to_state = NEW.status
) BEGIN
  SELECT RAISE(ABORT, 'projects: illegal status transition per state-machine.yaml');
END;
CREATE TRIGGER IF NOT EXISTS trg_runs_status_flow
BEFORE UPDATE OF status ON runs
WHEN NEW.status != OLD.status AND NOT EXISTS (
  SELECT 1 FROM sm_legal_transitions
  WHERE layer = 'run' AND from_state = OLD.status AND to_state = NEW.status
) BEGIN
  SELECT RAISE(ABORT, 'runs: illegal status transition per state-machine.yaml');
END;
CREATE TRIGGER IF NOT EXISTS trg_attempts_status_flow
BEFORE UPDATE OF status ON attempts
WHEN NEW.status != OLD.status AND NOT EXISTS (
  SELECT 1 FROM sm_legal_transitions
  WHERE layer = 'attempt' AND from_state = OLD.status AND to_state = NEW.status
) BEGIN
  SELECT RAISE(ABORT, 'attempts: illegal status transition per state-machine.yaml');
END;

/**
 * Phase A 静态交互原型的固定 fixture（deviation：方案 v0.2.1 §10 Phase A）。
 * 不接 sidecar、不改数据库；内容取材于 mynet-clinic 真机轮次的真实事件，
 * 用于验证看板 / 工作台 / Run 详情的交互逻辑。
 */

export type StageKey = 'prepare' | 'review' | 'baseline' | 'iterate' | 'confirm' | 'done';

export const STAGES: { key: StageKey; label: string }[] = [
  { key: 'prepare', label: '准备' },
  { key: 'review', label: '方案评审' },
  { key: 'baseline', label: '基线验证' },
  { key: 'iterate', label: '自动迭代' },
  { key: 'confirm', label: '最终确认' },
  { key: 'done', label: '已完成' }
];

/** Agent 对用户的可见状态（卡片 chip 与工作台头部共用）。 */
export type AgentStatus = 'idle' | 'working' | 'waiting-reply' | 'waiting-approval' | 'failed';

export const AGENT_STATUS_LABEL: Record<AgentStatus, string> = {
  idle: '空闲',
  working: '工作中',
  'waiting-reply': '等待你的回复',
  'waiting-approval': '等待批准',
  failed: '失败'
};

/** 卡片 / 工作台首屏的"需要你"阻塞项（方案 §4.2/§12：不得只埋在状态码里）。 */
export interface Blocker {
  title: string;
  reason: string;
  /** 恢复入口占位：Phase A 只展示，不接真实命令。 */
  actions: string[];
}

export interface BoardProject {
  id: string;
  name: string;
  goal: string;
  stage: StageKey;
  /** 阶段内进度的一句话描述（如"第 4/20 轮"、"环境 62%"）。 */
  stageProgress: string;
  agent: AgentStatus;
  agentNote: string;
  bestMetric?: { name: string; value: string; deltaVsBase?: string };
  compute?: string;
  budget?: string;
  unread?: number;
  lastActivity: string;
  primaryAction: string;
  blockers: Blocker[];
}

export const BOARD_PROJECTS: BoardProject[] = [
  {
    id: 'polyp-swin-v2',
    name: 'polyp-swin-v2',
    goal: 'Swin 骨干迁移到息肉分割，验证骨干增益',
    stage: 'prepare',
    stageProgress: '环境搭建 62% · 数据校验中',
    agent: 'idle',
    agentNote: '等待环境就绪',
    compute: 'A6000 未占用',
    budget: '0 / 14400s',
    lastActivity: '3 分钟前',
    primaryAction: '查看准备进度',
    blockers: []
  },
  {
    id: 'lesion-unet-registry',
    name: 'lesion-unet-registry',
    goal: '皮肤病灶分割：多数据集联合训练方案设计',
    stage: 'review',
    stageProgress: '3 个模块已出初稿 · 2 个问题待回答',
    agent: 'waiting-reply',
    agentNote: '评审对话等你回答 2 个反问',
    unread: 2,
    lastActivity: '26 分钟前',
    primaryAction: '继续评审',
    blockers: []
  },
  {
    id: 'mynet-clinic',
    name: 'mynet-clinic',
    goal: 'NLiMynet（K 值学习）在 CVC-Clinic 上的首轮基线',
    stage: 'baseline',
    stageProgress: 'Round 1 训练失败 ×1',
    agent: 'failed',
    agentNote: 'run 停在 REPAIRING，无自动恢复出口',
    compute: 'A6000 #0 · 已释放',
    budget: '1512 / 7200s',
    lastActivity: '18 小时前',
    primaryAction: '处理失败',
    blockers: [
      {
        title: '需要你：run 停在 REPAIRING，无自动出口',
        reason:
          '训练进程因 PYTHONPATH 环境污染启动即崩（fail_class 判为 model）。' +
          '基线阶段没有自动修复路径，需要人工裁决。',
        actions: ['修复环境后重开 Round 1（推荐）', '标记为 infra 触发同机重派（占位）']
      }
    ]
  },
  {
    id: 'seg-aug-benchmark',
    name: 'seg-aug-benchmark',
    goal: '增广策略对分割精度的系统性对比',
    stage: 'iterate',
    stageProgress: '第 2 轮 · 评测停滞',
    agent: 'failed',
    agentNote: 'EVAL_QUEUED 停滞：改码被阻断',
    bestMetric: { name: 'dice', value: '0.671', deltaVsBase: '+0.021' },
    compute: 'A6000 #0 · 空闲',
    budget: '2240 / 10800s',
    lastActivity: '2 小时前',
    primaryAction: '处理评测失败',
    blockers: [
      {
        title: '需要你：EVAL_QUEUED 评测失败且重试耗尽',
        reason:
          'round 2 的 infer 未产出 metrics.json（重试 2 次耗尽）。' +
          '正式指标缺失阻断继续改码（D24），需要人工介入。',
        actions: ['查看 infer 日志定位原因', '重置评测请求（占位）']
      }
    ]
  },
  {
    id: 'mynet-clinic-v3',
    name: 'mynet-clinic-v3',
    goal: 'NLiMynet 稳定化迭代：梯度裁剪 + 评测适配后的 K 值学习',
    stage: 'iterate',
    stageProgress: '第 4/20 轮 · 训练中 epoch 7/30',
    agent: 'working',
    agentNote: 'Round 4 训练运行中',
    bestMetric: { name: 'dice', value: '0.702', deltaVsBase: '+0.052' },
    compute: 'A6000 #0 · 6.2G 显存',
    budget: '3120 / 7200s',
    unread: 2,
    lastActivity: '12 分钟前',
    primaryAction: '查看当前实验',
    blockers: []
  },
  {
    id: 'vl-medseg',
    name: 'vl-medseg',
    goal: '视觉-语言先验用于医学分割的消融确认',
    stage: 'confirm',
    stageProgress: 'test 结果封存 · 待读取',
    agent: 'waiting-approval',
    agentNote: '等待你读取最终结果（一次性释放）',
    bestMetric: { name: 'dice', value: '0.796', deltaVsBase: '+0.043' },
    budget: '6300 / 7200s',
    lastActivity: '1 小时前',
    primaryAction: '读取最终结果',
    blockers: []
  },
  {
    id: 'emcad-clinic-repro',
    name: 'emcad-clinic-repro',
    goal: 'EMCAD 原文复现与基线对齐（已完成）',
    stage: 'done',
    stageProgress: '报告已产出',
    agent: 'idle',
    agentNote: '项目已完成',
    bestMetric: { name: 'dice', value: '0.808', deltaVsBase: '+0.043' },
    budget: '6900 / 7200s',
    lastActivity: '3 天前',
    primaryAction: '查看报告',
    blockers: []
  }
];

/* ------------------------------------------------------------------ */
/* Project Workspace（两个：健康项目全要素 + 阻塞项目首屏告警）        */
/* ------------------------------------------------------------------ */

export type TimelineItem =
  | { kind: 'user'; ts: string; content: string }
  | {
      kind: 'agent';
      ts: string;
      agent: string;
      jobState: string;
      content: string;
    }
  | { kind: 'question'; ts: string; agent: string; content: string }
  | {
      kind: 'proposal';
      ts: string;
      agent: string;
      title: string;
      rationale: string;
      impact: { budget?: string; command?: string; risk?: string };
    }
  | { kind: 'system'; ts: string; event: string; detail?: string }
  | {
      kind: 'run';
      ts: string;
      runId: string;
      label: string;
      state: string;
      metrics?: string;
      note?: string;
    }
  | {
      kind: 'blocker';
      ts: string;
      title: string;
      reason: string;
      actions: string[];
    }
  | { kind: 'decision'; ts: string; who: string; what: string };

export interface WorkspaceFixture {
  projectId: string;
  header: {
    title: string;
    stageLabel: string;
    stageProgress: string;
    agentStatus: AgentStatus;
    agentNote: string;
    budget: string;
    compute: string;
    primaryAction: string;
  };
  /** Workflow Rail 上各步的状态。 */
  rail: { stage: StageKey; state: 'done' | 'current' | 'future'; note?: string }[];
  context: {
    plan: string;
    papers: { title: string; note: string }[];
    experiment: string;
    resources: { gpu: string; budget: string };
    artifacts: string[];
  };
  alerts: Blocker[];
  timeline: TimelineItem[];
}

export const WORKSPACES: WorkspaceFixture[] = [
  {
    projectId: 'mynet-clinic-v3',
    header: {
      title: 'mynet-clinic-v3',
      stageLabel: '自动迭代',
      stageProgress: '第 4/20 轮 · 训练中 epoch 7/30',
      agentStatus: 'working',
      agentNote: 'AutoLab · Round 4 训练运行中',
      budget: '3120 / 7200s',
      compute: 'A6000 #0 · 6.2G',
      primaryAction: '查看当前实验'
    },
    rail: [
      { stage: 'prepare', state: 'done', note: '环境/数据 09-13 就绪' },
      { stage: 'review', state: 'done', note: '两段式评审 09-14 定稿' },
      { stage: 'baseline', state: 'done', note: 'dice 0.650 · 已验收' },
      { stage: 'iterate', state: 'current', note: 'round 4/20 · 无改进终止 n=5' },
      { stage: 'confirm', state: 'future', note: '终止条件触发后进入批次确认' },
      { stage: 'done', state: 'future', note: 'final-read + 报告产出' }
    ],
    context: {
      plan:
        'plan v2：保留 K 值学习模块（fixed_k 起步 0.5）；训练加梯度裁剪 max_norm=12；' +
        '评测走 metrics.json / final.ckpt 适配约定；30 epoch 快速迭代配置。',
      papers: [
        { title: 'EMCAD-Net (MICCAI 2024)', note: '骨干出处 · 已导入全文' },
        { title: 'Polyp segmentation survey', note: '对照方法 · 摘要入库' }
      ],
      experiment: 'round 4 · attempt RUNNING（epoch 7/30）\n指标序列 0.650 → 0.681 → 0.702',
      resources: { gpu: 'A6000 #0 · 6.2G / 48G', budget: '3120 / 7200s（剩余 57%）' },
      artifacts: ['final.ckpt（r3 · 29.6MB）', 'metrics.json（r3）', '阶段报告 · 09-14']
    },
    alerts: [],
    timeline: [
      { kind: 'system', ts: '09-13 21:02', event: 'project.created 立项（clinic 数据登记 · A6000 勾卡）' },
      {
        kind: 'system',
        ts: '09-13 21:11',
        event: 'env READY（复用 zwm 环境）· data replica 校验 PASSED（612 文件）',
        detail: 'environment_versions: env-64751… · manifest_sha 9f2c…'
      },
      {
        kind: 'user',
        ts: '09-13 21:15',
        content: '数据用 clinic，30 epoch 快速迭代配置，先把链路跑通再谈精度。'
      },
      {
        kind: 'agent',
        ts: '09-13 21:16',
        agent: 'Researcher',
        jobState: 'job #12 · COMPLETED',
        content:
          '已读 BASE 结构：NLiMynet = EMCAD 骨干 + 9 个 K 值选择模块。' +
          '计划：先按原配置跑基线，重点观察 K 模块的数值稳定性；依据是同类可微 top-k 实现在小数据上易梯度爆炸。'
      },
      {
        kind: 'run',
        ts: '09-13 21:20 → 21:47',
        runId: 'run-baseline-r0',
        label: 'Round 0 · 基线',
        state: 'FINISHED',
        metrics: 'dice 0.650 · mIoU 0.482',
        note: '预算 402s · best epoch 26'
      },
      { kind: 'decision', ts: '09-13 21:48', who: '你', what: '基线验收 accept（patch_class=none）' },
      {
        kind: 'question',
        ts: '09-14 09:30',
        agent: 'Researcher',
        content:
          'K 值学习在 epoch 1 出现梯度爆炸（NaN → Xid 43，见 run 日志）。' +
          '建议：基线与首轮迭代以 fixed_k=0.5 固定，把可学习 K 作为独立候选方向再开——同意吗？'
      },
      { kind: 'user', ts: '09-14 09:52', content: '同意。先稳定跑通，K 学习单独开候选。' },
      {
        kind: 'agent',
        ts: '09-14 10:05',
        agent: 'Engineer',
        jobState: 'job #13 · COMPLETED',
        content:
          '已为 BASE 落地三项适配并冒烟通过：梯度裁剪（max_norm=12）、非有限损失跳批、' +
          '评测产出 metrics.json / final.ckpt。训练链路在 A6000 复验无 Xid 复发。'
      },
      {
        kind: 'run',
        ts: '09-14 10:31 → 11:02',
        runId: 'run-r3',
        label: 'Round 3 · 梯度裁剪 + 评测适配',
        state: 'JUDGED',
        metrics: 'dice 0.702（+0.052 vs BASE）',
        note: '指标序列 0.650 → 0.681 → 0.702'
      },
      {
        kind: 'proposal',
        ts: '09-15 08:40',
        agent: 'AutoLab',
        title: '派发 Round 4：可学习 K 首次引入（fixed_k → learnable）',
        rationale:
          '裁剪与跳批稳定后，K 模块具备引入条件。本轮只改 fixed_k=None，' +
          '其余训练配置不动，保持与 r3 可比。',
        impact: {
          budget: '预计 ~600s（累计 3720 / 7200s）',
          command: 'next-round（提案 → JUDGE → 冒烟 → 派发）',
          risk: 'K 模块仍有爆炸可能；已有跳批守卫，最坏情况整轮无改进'
        }
      },
      {
        kind: 'system',
        ts: '09-15 09:12',
        event: 'run round4 RUNNING · epoch 7/30 · 预算计费中',
        detail: 'attempt disp-at-9781… · execution_cap 900s'
      }
    ]
  },
  {
    projectId: 'mynet-clinic',
    header: {
      title: 'mynet-clinic',
      stageLabel: '基线验证',
      stageProgress: 'Round 1 失败 ×1 · REPAIRING',
      agentStatus: 'failed',
      agentNote: 'run 无自动恢复出口',
      budget: '1512 / 7200s',
      compute: 'A6000 #0 · 已释放',
      primaryAction: '处理失败'
    },
    rail: [
      { stage: 'prepare', state: 'done', note: '环境/数据就绪' },
      { stage: 'review', state: 'done', note: '方案评审定稿' },
      { stage: 'baseline', state: 'current', note: 'Round 1 失败 · REPAIRING' },
      { stage: 'iterate', state: 'future', note: '基线验收后进入' },
      { stage: 'confirm', state: 'future' },
      { stage: 'done', state: 'future' }
    ],
    context: {
      plan: 'plan v1（自动冻结）：NLiMynet 原配置 · 30 epoch · clinic 数据',
      papers: [{ title: 'EMCAD-Net (MICCAI 2024)', note: '骨干出处' }],
      experiment: 'Round 1 · FAILED（启动即崩）',
      resources: { gpu: '已释放', budget: '1512 / 7200s' },
      artifacts: ['失败日志 log.txt（含 traceback）']
    },
    alerts: [
      {
        title: '需要你：run 停在 REPAIRING，无自动出口',
        reason:
          '训练进程因 agent 泄漏的 PYTHONPATH 污染启动即崩（import torch 时 typing_extensions 冲突）。' +
          '失败被分类为 model 而非 infra，因此没有走自动重派；基线阶段的 REPAIRING 没有自动修复路径，等待人工裁决。',
        actions: ['修复环境后重开 Round 1（推荐）', '标记为 infra 触发同机重派（占位）']
      }
    ],
    timeline: [
      { kind: 'system', ts: '09-14 20:58', event: 'plan v1 冻结生效 · effective_round=0' },
      {
        kind: 'run',
        ts: '09-14 20:59',
        runId: 'run-r1-failed',
        label: 'Round 1 · 基线',
        state: 'FAILED',
        note: 'rc=1 · 启动即崩'
      },
      {
        kind: 'blocker',
        ts: '09-14 21:00',
        title: 'REPAIRING · 无自动出口',
        reason:
          'traceback：typing_extensions._SpecialGenericAlias 与 py3.8 冲突（PYTHONPATH 污染）。' +
          'fail_class=model · D23 自修复在基线阶段未接入。',
        actions: ['修复环境后重开 Round 1', '标记 infra 重派（占位）']
      },
      {
        kind: 'system',
        ts: '09-14 21:40',
        event: 'agent 升级 0.1.5-m2：子进程剥离 PYTHONPATH（同类污染已根治）',
        detail: 'gpu-agent runner env.pop("PYTHONPATH")'
      }
    ]
  }
];

/* ------------------------------------------------------------------ */
/* Run detail                                                          */
/* ------------------------------------------------------------------ */

export interface RunFixture {
  runId: string;
  label: string;
  state: string;
  project: string;
  metricsTable: { name: string; value: string; base?: string }[];
  metricSeries: { round: string; value: number }[];
  logTail: string[];
  diff: string;
  artifacts: { name: string; note: string }[];
}

export const RUNS: RunFixture[] = [
  {
    runId: 'run-r3',
    label: 'Round 3 · 梯度裁剪 + 评测适配',
    state: 'JUDGED',
    project: 'mynet-clinic-v3',
    metricsTable: [
      { name: 'val_metric (dice)', value: '0.702', base: '0.650' },
      { name: 'f1_or_dsc', value: '0.702', base: '0.650' },
      { name: 'miou', value: '0.531', base: '0.482' },
      { name: 'accuracy', value: '0.937', base: '0.929' },
      { name: 'val_loss', value: '0.794', base: '0.846' }
    ],
    metricSeries: [
      { round: 'BASE', value: 0.65 },
      { round: 'r1', value: 0.681 },
      { round: 'r2', value: 0.695 },
      { round: 'r3', value: 0.702 }
    ],
    logTail: [
      'train: epoch 28, iter:60, loss: 0.8123, lr: 0.000121',
      'val epoch: 28, loss: 0.7941, miou: 0.531, f1_or_dsc: 0.702',
      'train: epoch 29, iter:60, loss: 0.8051, lr: 0.000098',
      'train: epoch 30, iter:60, loss: 0.7990, lr: 0.000080',
      'test of best model, loss: 0.7941, miou: 0.531, f1_or_dsc: 0.702',
      'metrics.json written to $AUTOLAB_OUT_DIR',
      'final.ckpt written from checkpoints/best.pth',
      'TRAIN_DONE (rc=0 · wall 1873s · gpu 1822s)'
    ],
    diff: `--- a/engine.py
+++ b/engine.py
@@ train_one_epoch @@
-        loss.backward()
-        optimizer.step()
+        loss.backward()
+        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=12.0)
+        optimizer.step()
@@ _test_one_epoch_impl @@
+        # AutoLab 适配（deviation #21）：test 指标落 metrics.json
+        _m = {"val_metric": round(float(f1_or_dsc), 6), ...}
+        with open(os.path.join(config.work_dir, 'metrics.json'), 'w') as _f:
+            json.dump(_m, _f, indent=2)
--- a/train.py
+++ b/train.py
@@ main @@
+        # AutoLab 适配：受保护产物 final.ckpt（best 权重）
+        _shutil.copyfile(_src, os.path.join(config.work_dir, 'final.ckpt'))`,
    artifacts: [
      { name: 'final.ckpt', note: '29.6MB · sha256 e3b0c4… · 受保护产物' },
      { name: 'metrics.json', note: 'sha256 9f2c6d… · 评测唯一入口' },
      { name: 'log.txt', note: '完整训练日志 · 已归档' },
      { name: 'snapshot cs-2535…', note: '代码快照 · git commit 4f8a2c' }
    ]
  },
  {
    runId: 'run-baseline-r0',
    label: 'Round 0 · 基线',
    state: 'FINISHED',
    project: 'mynet-clinic-v3',
    metricsTable: [
      { name: 'val_metric (dice)', value: '0.650' },
      { name: 'miou', value: '0.482' },
      { name: 'accuracy', value: '0.929' }
    ],
    metricSeries: [{ round: 'BASE', value: 0.65 }],
    logTail: [
      'train: epoch 1, iter:60, loss: 1.3428, lr: 0.001',
      'val epoch: 1, loss: 1.0734, f1_or_dsc: 0.489',
      '…',
      'train: epoch 30, iter:60, loss: 0.8412, lr: 0.000080',
      'test of best model, f1_or_dsc: 0.650, miou: 0.482',
      'TRAIN_DONE (rc=0 · wall 1641s · gpu 1602s)'
    ],
    diff: '(Round 0 为基线：无补丁 · BASE 原样快照)',
    artifacts: [
      { name: 'final.ckpt', note: '29.6MB · best epoch 26' },
      { name: 'metrics.json', note: '基线指标' },
      { name: 'log.txt', note: '完整训练日志' }
    ]
  }
];

export function getWorkspace(id: string): WorkspaceFixture | undefined {
  return WORKSPACES.find((w) => w.projectId === id);
}

export function getBoardProject(id: string): BoardProject | undefined {
  return BOARD_PROJECTS.find((p) => p.id === id);
}

export function getRun(id: string): RunFixture | undefined {
  return RUNS.find((r) => r.runId === id);
}

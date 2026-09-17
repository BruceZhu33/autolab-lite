# Research Workspace Protocol v1

CLI `research init` 在用户登记的项目根目录内生成：

```text
.autolab/
├── context.md
├── experiments/
│   └── EXP-0001.md
└── knowledge/
    └── failures.md
```

本目录的模板是规范输入。实现可以增加注释或格式化，但不得增加 QUEUED、RUNNING、GPU Lease、PID 等运行状态。

## 写入边界

- 本地项目：直接在登记项目根目录下读写。
- 远端项目：由 Control 通过受限 SSH/SFTP 工作区工具读写。
- 所有路径先做项目根 containment；拒绝绝对路径、`..` 和越界 symlink。
- 更新使用 expected content hash；文件被并发修改时返回冲突，不静默覆盖。
- Plan 批准时冻结实验记录完整内容的 SHA-256。

## 机器校验字段

- YAML front matter 只保存稳定标识和研究语义。
- `context.md` 的 `schema_version` 和 `project_id` 必填。
- 实验记录的 `id`、`title`、`created_at`、`conclusion` 必填。
- `conclusion` 只允许 `pending | supported | rejected | inconclusive`。
- `next_decision` 只允许 `iterate | promote | stop` 或留空。
- `supported/rejected/inconclusive` 必须在正文列出至少一个证据引用。
- `rejected` 或 `next_decision: stop` 必须提供重新考虑条件。

## 证据引用

推荐使用稳定资源 ID，不把调度状态复制到 Markdown：

```text
- job:job-123
- artifact:artifact-456
- metric:artifact-456#val_dice
```

Control/UI 按资源 ID 查询当前事实。

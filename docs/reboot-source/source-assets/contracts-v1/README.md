# AutoLab Lite contracts v1

本目录是新产品协议的规范来源，优先级高于 `contracts-reference/`、旧 gpu-agent 和旧 Control 代码。

文件：

- `plan-spec-v1.schema.json`：人和 Agent 审查、批准的实验执行计划。
- `manifest-v1.schema.json`：Control 派发给 Runner 的不可变执行契约。
- `runner-event-v1.schema.json`：Runner 的持久单调事件信封。
- `artifact-v1.schema.json`：Control 登记的远端/本地产物描述。

## 固定决定

- 版本字段统一为整数 `schema_version: 1`。
- 命令统一为 argv 数组。需要 shell 时显式使用 `['bash', '-lc', '...']`，不能把普通字符串默认为 shell。
- Plan 的输出路径是相对模板；Manifest 的绝对输出目录由 Control 根据项目配置生成。
- Runner 仍需使用本机允许根目录做 `realpath` containment；不能因为 Manifest 已签名就跳过路径检查。
- GPU Lease 只属于 Control。Manifest 中的 `gpu_indices` 是已分配结果，不是 Runner 的调度建议。
- Runner Event 的 `(runner_instance_id, event_seq)` 唯一；相同事件重放不产生第二次副作用。
- Job 执行状态不包含 `LOST`。不可达通过 `observation_state=UNKNOWN` 的派生视图表达。

## Canonical bytes 与签名

- 结构化 JSON 的 hash 使用 RFC 8785 JSON Canonicalization Scheme（JCS）后的 UTF-8 字节；拒绝 NaN/Infinity。
- `spec_hash = SHA-256(JCS(PlanSpecV1))`。
- `payload_hash = SHA-256(JCS(ManifestV1))`，用于 dispatch_id 重放比较。
- Manifest 签名为 `HMAC-SHA256(key, UTF8("autolab-manifest-v1\n") + JCS(ManifestV1))`。
- `schema_version` 是 Manifest 本体的必填字段，调用者不能通过外层 envelope 覆盖。
- 实验记录 hash 使用批准时文件的原始字节 SHA-256；任何字节变化都会显示“记录已变化”，但不修改已批准 Plan。

## Job 状态

```text
QUEUED
DISPATCHING
RUNNING
CANCELLING
SUCCEEDED
FAILED
CANCELLED
TIMED_OUT
```

终态为 `SUCCEEDED | FAILED | CANCELLED | TIMED_OUT`。终态不回退。`QUEUED | DISPATCHING | RUNNING | CANCELLING` 均可在对账后根据 Runner 事实更新。

## 观察状态

```text
CONFIRMED | STALE | UNKNOWN
```

它由最近 Runner 联系时间、事件游标和 snapshot 计算，不替换 Job 的执行状态：

- `CONFIRMED`：状态已由近期 Runner 事件或 snapshot 确认。
- `STALE`：暂时没有新事实，但仍在容忍窗口内。
- `UNKNOWN`：Runner 不可达或游标失效，不能断言任务已失败或已停止。

Runner 恢复后，非终态 Job 可以从 `UNKNOWN` 重新显示 `CONFIRMED`，执行状态则按远端事实继续 RUNNING 或进入真实终态。

## 兼容策略

v1 不兼容旧 `/attempts`、旧 `m1/1` canonical envelope 或旧状态名。未来协议变更使用新的 `schema_version` 和明确迁移，不在同一版本中接受两套字段。

# AutoLab · WS 事件契约（sidecar → 前端）

- 版本：v1.0-draft.6（第五轮评审修订版） · 2026-09-13
- 基线：技术设计 v2.7 §6 · 需求 v0.5.3（P5 实时监视、D31 停止待确认显示）· 复审处置 A4 + GAP 仲裁"WS=投影，不持久化追认"
- 范围：`GET /ws`（WS 升级）——前端实时更新通道。**agent → sidecar 的事件流不走 WS**（HTTP 轮询 `GET /events?since_seq&agent_instance_id` + agent_inbox 幂等入库，设计 §5，U-O3）。
- 权威性声明：WS 是**派生投影**——写决策一律走 OpenAPI（响应带权威 state_revision）；WS 仅用于 UI 推进。

## 1. 连接与鉴权

- URL：`ws://127.0.0.1:{port}/ws?token={capability_token}`（浏览器 WS 无法自定义头；loopback + 随机端口，B2）。
- token 无效 → 关闭码 `4401`；30s 内未完成 subscribe → 服务端可关闭（`4408`）。
- 心跳：服务端每 20s 发 `heartbeat` 帧（**携带 `last_emitted_sub_seq`（已发 watermark）**，见 §3）；客户端 60s 未收到任何帧应主动重连。

## 2. 事件信封（A4 修订：订阅标识 + 双序列）

```json
{
  "subscription_id": "sub-8f3a",
  "sub_seq": 1042,
  "global_seq": 20911,
  "epoch": 3,
  "type": "attempt.state_changed",
  "occurred_at": "2026-09-12T08:30:00Z",
  "aggregate": { "type": "attempt", "id": "at-9f3", "project_id": "p-1", "run_id": "run-42" },
  "payload": { "from": "RUNNING", "to": "FINISHED", "event": "finished", "state_revision": 7 }
}
```

| 字段 | 语义 |
|---|---|
| `subscription_id` | **订阅标识**（服务端在 subscribe 受理时分配，连接存续期内持久于服务端内存）。重连 resume 必须携带——服务端据此定位"每订阅已发缓冲"完成重放；epoch 变化（进程重启）则 subscription_id 失效 → 必须全量快照 |
| `sub_seq` | **本订阅过滤后的连续序号**（每订阅从 1 起、严格 +1、无空洞）。客户端丢包检测与重连重放**只看它**——过滤不会造成跳号 |
| `global_seq` | sidecar 进程内全局事件计数（跨订阅共享），仅审计/排障，客户端不得用它判丢包 |
| `epoch` | sidecar 进程代号（内存）。进程重启 → 变化 → 客户端必须全量快照重同步（重启恢复语义见 state-machine.yaml recovery） |

## 3. 单调与丢包规则

1. `sub_seq` 每订阅从 1 开始严格 +1；服务端在**同一事件循环原子段**内完成"过滤 → 分配 sub_seq → 发送"。
2. 客户端收到 `sub_seq > 上次+1` → 判丢 → 发 `resync`（等同游标过期路径）。
3. `heartbeat` 携带 `{"type":"heartbeat","subscription_id":…,"last_emitted_sub_seq":N}`——N=服务端已实际发出的最后一帧 sub_seq（已发 watermark）。客户端发现 `last_emitted_sub_seq > 已收到最大 sub_seq` → 尾部丢失 → **立即 resync**；由于 sub_seq 为过滤后连续序号，不存在"被过滤"歧义。
4. `sub_seq` 与 agent 侧事件序（agent_inbox.event_seq）、`global_seq` 三者独立，不得混用。

## 4. 断线重连与恢复语义（A4 修订：snapshot watermark）

```
客户端                              服务端
  │ -- connect (?token=...) ----------->│
  │ <-- connected {epoch} --------------│
  │ -- subscribe {filters, resume?} ---->│
  │     resume = {subscription_id,       │
  │               epoch, last_sub_seq}   │
  │ <-- [replay (last_sub_seq, now]] ----│ ① 服务端事件循环原子段：注册订阅 → 记录 barrier 序号
  │                                     │    → 重放缓冲命中段（每订阅缓冲，默认 10000 帧已发事件）
  │ <-- snapshot_required ---------------│ ② epoch 变化 / resume 缺失 / 游标过期
  │ -- snapshot_ack -------------------->│
  │ <-- snapshot {snapshot_at_sub_seq,   │ ③ 快照读取于 barrier 之后：服务端先建缓冲屏障，
  │      takes: [...]} ----(多帧)--------│    再读 DB 快照，屏障后事件保证经正常帧补发
  │ <-- event ... (sub_seq = barrier+1…) │ ④ 增量接续（快照后 sub_seq 从 snapshot_at_sub_seq+1 连续）
```

- **快照 watermark（修复 A4 漏事件/回退窗口）**：`snapshot` 帧必含 `snapshot_at_sub_seq`。
  服务端在**同一权威提交序**上完成：① 暂停本订阅事件派发并记录 barrier（= 下一个待发 sub_seq）→
  ② 读 DB 快照（因此快照必然包含 barrier 之前所有已提交状态）→ ③ 恢复派发（barrier 后事件按 sub_seq 连续下发）。
  客户端应用规则：快照后收到的事件帧若其 `aggregate.state_revision` ≤ 快照内该实体状态版本 →
  **丢弃**（防旧增量回退 UI 状态）；更大则应用。
- 重放窗口：每订阅已发事件内存缓冲（默认 10000 帧）。窗口外 → `snapshot_required`。
- 快照按订阅过滤器裁剪；`takes[]` 为实体当前状态聚合（projects/runs/attempts/budget…）。
- WS 不持久化（GAP 仲裁追认）：进程重启 = epoch 变化 → 全量快照，无跨进程重放承诺；权威状态在 DB + HTTP。

## 5. 订阅过滤

`subscribe` 帧（可重复发，后发覆盖先发）：

```json
{
  "type": "subscribe",
  "filters": {
    "project_ids": ["p-1"],
    "kinds": ["project.state_changed", "budget.*", "evaluation.result_stored"],
    "aggregates": [ { "type": "run", "id": "run-42" } ],
    "include": { "log_tail": false, "metric_points": true }
  },
  "resume": { "epoch": 3, "last_sub_seq": 1041 }
}
```

- `project_ids`：不填=全部项目；`kinds`：支持 `prefix.*`，不填=全部；`aggregates`：单实体追踪（P5 轮次详情页）。
- **过滤器变更 = 新订阅**：`sub_seq` 重新从 1 计数（旧序号作废），服务端在 subscribe 响应帧（`subscribed {sub_seq_reset:true}`）中明示。
- `include.log_tail` 默认 false（高频，显式开启后 ≤10 帧/秒合并节流）；`include.metric_points` 默认 true（1s 窗口聚合下发）。

## 6. 事件类型清单

命名 `域.动作`；`*.state_changed` 载荷含 `{from, to, event, state_revision, reason?}`（与 state_events/outbox 同事务产生）。

| 类型 | 触发 | 载荷要点 | 默认订阅 |
|---|---|---|---|
| `system.ready` / `system.shutdown` | sidecar 启动/优雅退出（停派发+对账+落盘后） | 版本、schema_ver | ✔ |
| `project.state_changed` | 项目层任意迁移（state-machine.yaml） | from/to/event/reason/pause_reason | ✔ |
| `run.state_changed` | run 层迁移（含 REPAIRING/LLM_BACKOFF/SNAPSHOTTING 分流） | + fail_class | ✔ |
| `attempt.state_changed` | attempt 迁移与停止链（ATTEMPT_STOP_UNKNOWN/停止待确认，D31） | + dispatch_id/profile | ✔ |
| `budget.changed` | S-RESERVE/S-USAGE/S-SETTLE 提交 | C/R/limit/revision | ✔ |
| `budget.reduction_accepted` | 降额受理（202 accepted） | requested_limit | ✔ |
| `budget.reduction_settled` | 阶段③ 原子达成 | 新 limit_s、账户回 ACTIVE | ✔ |
| `budget.reduction_unattainable` | [BLOCKED-ADD-1] 目标不可达终态（待增补） | 原限维持、失败原因 | ✔ |
| `control.epoch_changed` | apply_control_revision（epoch+1） | epoch_from/to | ✔ |
| `env_setup.progress` | 每机 code/env/data 步骤推进/失败（A11） | server_id/step/status | ✔ |
| `sync.state_changed` | 数据副本 SYNCING→VERIFYING→READY\|FAILED（D39） | replica_id/manifest_sha | ✔ |
| `gpu.status_changed` | 卡状态与占用分类变化（A03：平台/外部） | gpu_id/status/occupied_by/utilization_pct | ✔ |
| `metric.point` | 训练曲线采样（诊断来源 D24） | run_id/metric/step/value/source | ✔（聚合） |
| `attempt.log_tail` | 日志尾块 | attempt_id/text | ✖（显式订阅） |
| `evaluation.result_stored` | T-EVAL 提交（可信指标入库） | request_id/scope=**val**/metrics[]/协议版本 | ✔ |
| `candidate.selected` | D37 选择落库 | candidate_id/selected_run_id | ✔ |
| `batch.item_status` | batch_items 状态推进（A07） | batch_id/item/status/fail_reason | ✔ |
| `ablation.matrix_status` | 矩阵重校验/需修订（D36/R07） | matrix_id/outcome/invalidated_items | ✔ |
| `final_confirmation.released` | T-FINAL-READ 提交 | outcome/release_payload_hash | ✔ |
| `invalidation.propagated` | D44 失效边写入 | cause→invalidated 列表 | ✔ |
| `plan.frozen` / `plan.revised` | T-FREEZE / D38 新版本 | protocol_id/ver/class | ✔ |
| `report.completed` / `report.failed` | 报告产物 [BLOCKED-ADD-3] | report_id/chart refs | ✔ |
| `heartbeat` | 周期 | + last_emitted_sub_seq（已发 watermark） | - |
| `snapshot_required` / `snapshot` / `subscribed` / `resync.ack` | 恢复协议（§4/§5） | - | - |

**硬约束**：`evaluation.result_stored` 的 `scope` 只允许 `val`——test 结果数值**永不经 WS 下发**，只能经 `final-confirmation/read` 原子释放后随 `final_confirmation.released` 的固定结果给出（U3）。

## 7. 错误帧

`{ "type": "error", "code": "...", "message": "...", "request_id": "..." }`
码集：`AUTH_INVALID_TOKEN`（4401 关闭）、`SUBSCRIBE_INVALID`、`CURSOR_EXPIRED`（随 snapshot_required）、`RATE_LIMITED`（log_tail 节流）。

## 8. 与既有契约的一致性

- 事件 `state_revision` 与 OpenAPI 乐观锁同源（S-CAS 产物）→ 前端用事件流推进 UI 版本号，写操作仍带 expected_state_revision。
- 恢复语义三层对齐：UI 重连（本文件 §4，sub_seq+watermark）↔ sidecar 重启恢复（state-machine.yaml recovery）↔ agent 事件对账（设计 §5/U-O3）——三层各自幂等、互不越权。

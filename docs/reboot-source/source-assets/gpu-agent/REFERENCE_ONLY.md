# gpu-agent reference only

本目录是旧 AutoLab gpu-agent 的内部参考快照，不是新产品 Runner 的可直接迁入模块。

## 可以复用的知识

- `dispatch_id + payload_hash` 幂等思想；
- 独立进程组和 TERM→grace→KILL 辅助函数；
- 本地 SQLite、单调事件序号、日志文件和硬超时；
- Runner 绑定 loopback、通过 SSH 隧道访问的部署形态。

复制任何函数前必须为新接口写测试，并移除旧 Manifest、`/attempts`、一卡一队列和旧状态名。

## 新 Runner 的强制要求

### 协议

- 只实现 `source-assets/contracts-v1/manifest-v1.schema.json` 和 `runner-event-v1.schema.json`。
- API 使用 `/jobs`；不提供旧 `/attempts` 兼容层。
- `schema_version` 不支持时明确拒绝，不能静默忽略字段。
- 同一 `dispatch_id` 的并发重放只启动一个进程；异载荷返回冲突。

### 调度权

- GPU Lease 和等待顺序只由 Control 管理。
- Runner 不维护第二套等待队列；目标 GPU 已被其他 Job 占用时返回 busy/conflict，由 Control 对账。
- 多 GPU Job 只有在 Control 原子持有全部 Lease 后才可派发。

### 鉴权

- 非显式 test mode 下，Token 或 HMAC key 为空必须拒绝启动。
- Token 不接受命令行参数，避免进入进程列表；从 0600 配置、服务管理器凭据或环境注入。
- 所有非 health 接口必须先鉴权；Manifest HMAC 默认强制。
- HMAC 版本由固定协议字段参与签名，调用者不能覆盖规范版本。

### 路径与进程

- `workdir` 和输出目录先解析 `realpath`，再校验位于服务器配置的允许根目录内。
- 拒绝绝对越界、`..`、越界 symlink 和宽泛删除。
- `cwd` 必须是 Manifest 的项目 `workdir`；输出目录只是产物位置。
- 使用 argv + `shell=False`。需要 shell 时，Plan 必须显式批准 `['bash', '-lc', '...']`。
- 持久化 PID 的同时保存 Linux process start time、进程组、Runner nonce 等身份信息；重启后验证身份再发信号，禁止只凭 PID 杀进程。

### 持久性

- Job 状态变更和对应事件追加必须在一个本地事务中完成。
- 提供按 cursor 的增量日志和当前 Job snapshot。
- Control 断开不影响进程；Runner 重启后的真实状态通过 snapshot/事件对账。
- 事件序号在同一 Runner instance 内严格单调，重启是否轮换 instance id 必须有确定规则。

### 测试

至少覆盖：

- 空 Token/HMAC 启动失败；
- 三个并发相同 dispatch 只启动一次；
- 相同 dispatch 异载荷冲突；
- 非法 schema version；
- workdir/output 的 `..`、symlink 和前缀碰撞越界；
- 超时后进程组无子进程残留；
- 陈旧 PID 不会误杀无关进程；
- 状态/事件原子性和 cursor 重放；
- Runner 无内部排队。

## 版本基线

- Control：Python 3.12。
- Runner：Python 3.10+，CI 至少覆盖 3.10 和当前开发版本。
- 包元数据、运行时 `__version__`、部署产物和 `/healthz` 必须使用同一个版本来源。

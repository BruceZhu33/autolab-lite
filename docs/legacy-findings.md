# 旧工作区（FREEHANDS）审查发现 → 新产品里程碑映射

2026-09-17 的一轮外部审查对旧 AutoLab（FREEHANDS）给出「不通过」结论，
列举了 9 项问题。核对结果：**全部 9 项的代码引用都位于
`/Users/juan/Documents/FREEHANDS/`（v1.3 交接包已定义为只读 legacy reference），
无一存在于本仓库。** 该审查的最终建议——冻结 legacy、按 v1.3 重建 7 表新仓库、
`/jobs` Runner、无 Web/SSH/LLM——正是本仓库已经实现的形态。

本文不否定这些发现本身：它们是旧系统的真实缺陷，也是重启决策的佐证。
凡对新里程碑仍有约束力的，映射如下，防止在新代码里重演。

## 结构性主张核对（审查 P0）

| 审查主张 | 实测位置 | 本仓库实测 |
|---|---|---|
| 57 张表 | FREEHANDS/sidecar：58 处 `CREATE TABLE` | 7 张业务表 + schema_migrations |
| Project/Run/Attempt 三层状态机 | FREEHANDS/sidecar | projects 仅 ACTIVE/ARCHIVED（db.py CHECK） |
| /attempts Runner | FREEHANDS/gpu-agent | 全仓 `/attempts` 出现 0 次；Runner 是 `/jobs` |
| Runner 内部等待队列 | FREEHANDS/gpu-agent | GPU 被占直接 409（test_gpu_busy_409_and_no_internal_queue） |
| SSH/Web/LLM/RD-Agent | FREEHANDS/sidecar、frontend-next | grep asyncssh/paramiko/openai/anthropic/rdagent = 0 |
| 无 docs/reboot-source | 审查看的不是本仓库 | `git ls-files`：55 个文件已提交 |
| E2E 等 REVIEW_GATE / 空 HMAC 重放 | FREEHANDS tests-reference | 新 E2E 20 连跑有日志（E2E GATE PASSED 20/20） |
| 73 单测 / 175 契约 / TS build | FREEHANDS 测试面 | 本仓库 126 个 Python 测试，无前端 |

审查引用的 evaluator.py / monitor.py / llm_loop.py / runs.py / env_setup.py /
judge.py 在本仓库均不存在（实测 find = 0），路径全部指向 FREEHANDS。

## 对新里程碑仍有约束力的教训

1. **评测必须绑定被评测的 checkpoint（M3+ 评测/Result Policy）**
   旧 evaluator 下载了 checkpoint 却不把它交给 eval 命令，指标是候选代码
   自报的。新产品任何评测 Job 的 Manifest 必须把被评测 artifact 的路径与
   sha256 显式注入命令与校验，metrics.json 必须可追溯到该 checkpoint。
2. **指标方向必须贯穿判定（未来判定/终止逻辑）**
   PlanSpecV1.metrics.direction 已支持 maximize/minimize/target；任何未来的
   成功判据评估、最佳轮选择、终止决策都必须读取 direction，禁止硬编码
   `>=`/`max()`。
3. **未知事件不得卡死消费（已结构性免疫，保持回归测试）**
   旧 monitor 的全局游标会被未知 dispatch_id 卡死。新架构按 job 快照拉取 +
   `(runner_instance_id, event_seq)` 去重，没有全局游标可卡；保持
   test_unknown_job_event_ignored 类回归测试。
4. **补丁冒烟失败必须回滚（M4 Agent 写工具约束）**
   旧 llm_loop 冒烟失败不回滚，坏补丁混入下一轮提交。M4 的
   apply_project_patch 必须带失败回滚（git checkout 恢复）与 hash/CAS 防覆盖，
   不自动 commit——09 号决策门已有该约束，此为具体反例依据。
5. **禁止 GPU 默认回退 0 卡（已满足）**
   旧 runs.py 无空闲卡时回退 index 0。新调度器 Lease 全有或全无，无卡可用时
   Job 保持 QUEUED，不存在默认卡（test_multi_gpu_job_lease_all_or_nothing）。
6. **环境就绪门必须检查环境本身而非仅数据副本（M3 env_setup 需求）**
   M3 实现环境部署时，就绪判定必须包含解释器/依赖验证结果，不得只看历史
   数据副本状态。
7. **SSH/进程安全（M3 决策门已含）**
   known_hosts=None、空 Token、仅凭 PID 杀进程均为旧系统实测缺陷；M3 的
   TOFU 指纹固定、Runner fail-closed、进程身份核验是硬性要求。新 Runner
   当前已做到：非 test mode 空 Token 拒绝启动、停止前核验 PID+启动令牌。

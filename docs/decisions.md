# 工程决策记录

每条新增的表、状态、依赖、偏离交接包建议的决定都记录在这里，并给出对应的具体理由。
规范优先级：`docs/reboot-source/source-assets/contracts-v1/` > 需求/架构文档 > 08 开工方案 > reference 代码。

## D-001 · Multica 许可路径（M5 决策门提前落档）

- 日期：2026-09-17
- 决定：项目所有者选择按「内部使用 + 保留归属」路径使用 Multica 派生前端资产
  （09 号文档路径 2）：不对外分发、不重新命名为 AutoLab 品牌资产的前提下，
  允许解压 `ui-reference` 快照并从自有 `frontend-next` 派生副本迁移组件。
- 理由：所有者确认仅个人内部使用；实际参考对象是 FREEHANDS 工作区中已大幅偏离
  上游的自有 `frontend-next` 副本，而非上游原样代码。
- 义务：迁移时保留 Multica LICENSE/NOTICE/出处文件；对外分发前必须重新评审。
- 影响：M5 UI 工作不再被许可门阻塞，但 M0/M1 顺序不变，本轮仍不启动 UI。

## D-002 · M1 的 Control 不启动 HTTP 服务

- 日期：2026-09-17
- 决定：M1 中 Control 以「调度进程 + 共享 SQLite(WAL)」形态运行；CLI 直接经
  事务层写库，不暴露 loopback HTTP API。
- 理由：M1 的真实调用方只有人类 CLI 和调度循环；HTTP API 的第一个真实消费者
  （薄控制台 / MCP）要到 M4/M5 才存在。没有当前用户动作就不新增监听端口。
- 边界：`autolab_control` 保持模块边界（repo/transport/scheduler 分离），
  M4 增加 FastAPI 层时不需要改事务代码。

## D-003 · 服务器凭据存 0600 凭据文件，不进 SQLite

- 日期：2026-09-17
- 决定：`servers.runner_token_ref` 存不透明引用；`runner_token` 与
  `manifest_hmac_key` 的实际值存 `~/.autolab/credentials.json`（0600），
  测试用临时凭据文件。
- 理由：满足「密钥不进 Git/日志/API 响应」且不改动规范定义的 servers 表结构。

## D-004 · 全仓 requires-python >= 3.10，开发解释器 3.12

- 日期：2026-09-17
- 决定：所有包（含 Control）声明 `>=3.10` 并避免 3.11+ 语法；开发与 CI 基线
  解释器为 3.12。
- 理由：Runner 必须 3.10+ 可部署；统一语言子集让单 lockfile 覆盖全仓，
  避免两个并行约束集。CI 覆盖 3.10 属于后续工作（见 README 已知限制）。

## D-005 · JCS 规范化使用 rfc8785 库

- 日期：2026-09-17
- 决定：canonical JSON（RFC 8785）通过 `rfc8785` 包实现，不自研浮点序列化。
- 理由：自实现 ECMAScript Number::toString 兼容序列化极易出错；
  `rfc8785` 是规范参考实现且无传递依赖。

## D-006 · E2E 注入「已受理但响应失败」的测试钩子

- 日期：2026-09-17
- 决定：Runner 显式 test mode 下识别 `X-Autolab-Test-Drop-Response: 1` 请求头：
  先持久化受理，再返回 500（模拟响应丢失）。Control test mode 下
  `AUTOLAB_TEST_DROP_FIRST_DISPATCH=1` 使 transport 仅对首次派发携带该头。
- 理由：E2E 场景 7 要求真实双进程下注入「Runner 已受理但 Control 响应超时」，
  需要一个双方都显式声明的注入点；5xx 語义上即「结果未知」，与真实超时同路径。

## D-007 · servers 表增加 gpu_slots 列

- 日期：2026-09-17
- 决定：`servers.gpu_slots INTEGER NOT NULL DEFAULT 1`（仍在 7 表边界内）。
- 理由：GPU Lease 获取需要知道服务器索引空间；把容量强制写进每个 Plan 的
  `eligible_gpu_indices` 会把调度权从 Control 泄漏到 Plan 作者。该值是登记
  服务器时的一次性描述，与 Runner 配置的 `gpu_slots` 对应。

## D-008 · jobs 表增加 dispatch_attempts 列

- 日期：2026-09-17
- 决定：`jobs.dispatch_attempts INTEGER NOT NULL DEFAULT 0`，上限 3 次。
- 理由：08 号方案要求「明确、有限的恢复策略，不做无限自动重试」；重试计数
  是实现该上限的最小运行事实，放在 jobs 行内而不是新表。

## D-009 · Manifest 的 created_at 由 job 行派生，保证重放字节稳定

- 日期：2026-09-17
- 决定：`build_manifest` 的 `created_at` 取 `jobs.queued_at`（解析失败才用
  当前时间），派发重放与 E2E 手工重放都会重建出 payload_hash 相同的 Manifest。
- 理由：E2E 暴露的真实缺陷——同一 dispatch_id 重放时若 created_at 取当前
  时间，载荷 hash 变化，Runner 按协议返回 409 dispatch_conflict，把幂等重放
  误判为冲突。字节稳定是 dispatch 幂等正确性的前提。

## D-010 · conda/venv 环境在 M1 显式拒绝

- 日期：2026-09-17
- 决定：Manifest 构建遇到 `environment.kind ∈ {conda, venv}` 时抛出明确错误，
  引导使用 `inherit` 或 `prefix`。
- 理由：环境名到远端解释器路径的解析依赖 M3 的 SSH/服务器语义；M1 本地
  transport 下静默忽略或猜路径都比显式失败更危险。

## D-011 · Runner 重启后收养进程的退出码语义

- 日期：2026-09-17
- 决定：Runner 启动时对「RUNNING 且有 PID」的任务做进程身份核验：进程仍在则
  收养（按身份令牌轮询直到消失），进程已不在则直接定论。两种情况的退出码
  都无法回收（孤儿进程已被 init 收割），如实记为 `FAILED`，
  `failure_class=runner`，摘要注明 "exit code not captured"。
- 理由：Runner 崩溃后无法 `wait()` 别人的孤儿进程；伪造退出码或写成
  SUCCEEDED 都比诚实记录更危险。控制端 observation 机制保证这不会被当成
  用户代码失败。
- 附带：停止请求与看护线程的竞态用进程内 `_stopping` 标志解决——停止在途时
  看护线程让位，终态提交权归停止处理器（TERM 导致的 -15 退出码不会被记成
  user_code 失败）。

## D-012 · 事件消费按 (instance, seq) 去重，不按裸 seq 过滤

- 日期：2026-09-17
- 决定：Control 拉取 Runner snapshot 时不再使用 `events_after` 游标过滤，而是
  全量拉取该 job 的事件，依赖 `(runner_instance_id, event_seq)` 唯一约束去重。
- 理由：Runner 重启轮换 instance id 后事件序号从 1 重新计数，裸 seq 游标会把
  新实例的低序号终态事件全部滤掉（E2E 暴露：重启后任务在 Control 侧永远
  无法到达终态）。单任务事件量极小（<20 条），全量拉取代价可忽略。

## D-013 · Runner 不可达时不消耗重试次数

- 日期：2026-09-17
- 决定：调度器在派发前检查服务器可达性缓存；已知不可达时用
  `return_to_queued` 退回队列并退还本次 attempt（dispatch_attempts-1）。
- 理由：服务器临时离线不是任务失败；让 outage 烧完 3 次重试把任务打成
  FAILED 违反「未知结果不得直接标 FAILED」的精神。

## D-014 · Web 壳的迁移基础与边界（M5，许可路径 D-001）

- 日期：2026-09-17
- 决定：apps/web 是独立 Next.js 15 + Tailwind v4 工程；从自有 frontend-next
  副本按 07 号清单只迁移设计资产（packages/ui/styles/tokens.css、base.css
  连同 LICENSE/NOTICE），Shell/看板/GPU/Run 组件按 AutoLab 的
  BoardCardView/ServerCardView 投影自研；不引入 @multica/core、shadcn 链、
  Issue/成员/Presence 内核。生产 bundle 由 E2E 断言不含 Multica 领域符号。
- 理由：保留已验证的信息密度与视觉语言（用户喜欢的部分），同时满足
  「看板直接消费 BoardCardView[]、一次请求、无全局失效」的验收。

## D-015 · Control HTTP API 的鉴权与状态边界（M5）

- 日期：2026-09-17
- 决定：loopback FastAPI 挂在 serve 进程内（--http-port），bearer token 分
  owner/agent scope（credentials 文件 `control:api` 条目）；所有端点都是
  SQLite 事实的纯投影，唯一写入口是 owner-scope 的 approve。MCP 保持
  agent 写路径，Web 是 owner 读+批准入口。

## D-016 · Iteration 模块的生成边界与研究文件补建（M6）

- 日期：2026-09-19
- 决定：自动迭代 Agent 只产 DRAFT Plan（`iteration.py`）；LLM 输出受严格
  JSON schema 约束（失败带错误重试一次），服务端补齐 experiment_ref/hash、
  code_anchor、resources 等字段后走既有 `create_draft_plan`。项目没有实验
  文件时先补建基线实验记录 EXP-0001（Baseline 模块跑的基线原本不经
  Plan/Job 体系），再写本轮迭代实验；spec.baseline 必须精确为被对照实验的
  EXP id，看板 iterate 列由既有 `derive_project_stage` 判定，零改动。
- 结论记录：写回实验文件复用 `research.record_conclusion` 全部校验
  （SSH 工作区经临时目录中转）；失败或 rejected 时追加
  knowledge/failures.md 条目；归档仍走既有 confirm→done 阶段流转，
  研究事实与阶段事实分离。
- 附带修复：scheduler 指标提取原先只 `open()` 本地路径，SSH 服务器上的
  Job 指标永远提取不到且失败后每 tick 重试；现在本地失败回落 SFTP 读取，
  并以 metric_attempts（上限 3）防止重试风暴。conda 环境经 Baseline 配置
  自动包进 `["sh","-c","source … && conda activate … && exec …"]`
  （manifest 层面 D-010 仍拒绝 kind=conda/venv）。

## D-017 · 实现 Agent：LLM 直接改工作区代码（Iteration 闭环补全）

- 日期：2026-09-19
- 决定：在「制定计划」之前新增「Agent 实现方案改动」阶段
  （`iteration.implement_proposal_changes`）：读评审报告 + 工作区真实源码
  （入口/py/yaml，≤10 文件）→ LLM 输出完整文件内容（create/modify，
  ≤6 文件、单文件 ≤24KB）→ 原文件备份到 `.autolab/patches/<时间戳>/orig/`
  → 写入 → 对改动 .py 做 `py_compile` 语法检查；失败带错误重试一轮，
  仍失败整体回滚（不留坏代码）。计划生成的上下文注入实现 manifest，
  实验命令基于改后代码。多轮迭代：上一轮结论记录后 UI 提供「开新一轮」，
  新实验以最近一轮 EXP 为 baseline。
- 理由：不改代码就无法真正实现评审后的方案（此前的半自动路径只是对旧
  代码重跑参数矩阵）。claude code 会话集成仍是后续增强（可替换本阶段的
  LLM 单轮改写为迭代式改码+自测）；当前方案零新依赖、可审计（备份+
  语法门+计划批准门三重防线），先闭环产品语义。
- 边界：LLM 单轮全文重写只适合中小工作区；引入新依赖/大改动仍应走人工
  或后续的会话式实现。

## D-020 · Baseline 修复 Agent 与接入摩擦的系统性消除（M6 真实接入期）

- 日期：2026-09-20
- 决定：新增 `baseline_agent.agent_fix_baseline`——Baseline 失败后，Agent
  自己 读失败日志（工作区 pid 文件回落 DB 记录）→ 从 traceback 提取涉及
  源文件 → 连同接入体检事实、本地资源清单（models/、checkpoints/）喂给
  LLM → 最小补丁（modify/create/delete，≤4 文件，完整内容）→ 备份到
  .autolab/patches/*-fix + py_compile 语法门（失败回滚并带错误重试一轮）
  → 自动重跑 Baseline 并落 running 状态。UI：Baseline 页失败时显示
  「Agent 修复并重跑」。
- 同步层消除 AppleDouble 类故障：tar 打包 COPYFILE_DISABLE=1 + filter
  排除 ._*/.DS_Store，解包后远端清理残留。
- 理由：MedCLIPSeg 真实接入中，"诊断→打补丁→重跑"循环全部由维护者人工
  完成（timm shim、BiomedBERT 本地化、指标适配、幽灵文件），体检只报
  缺口不解决。该循环恰是 LLM 强项（读 traceback→定位文件→最小修复），
  且与迭代实现 Agent 共用补丁/备份/语法门基建。用户边界收窄为：提供
  代码/数据/权重/环境，其余接入摩擦由产品消化。

## D-018 · 一键结论：改进路径跳过手动 confirm→done（M6）

- 日期：2026-09-19
- 决定：conclusion 端点支持 `auto=true`：conclusion/next_decision 按对比
  verdict 推导（improved→supported+promote），分析由 Agent 总结生成
  （LLM，端点偶发失败重试一次，仍失败回落指标模板，不阻塞结论记录），
  且 improved 且全终态时自动执行 confirm→done 归档。UI 上对比
  verdict=improved 时「记录结论」对话框替换为「Agent 总结并归档」一键按钮。
- 理由：成功路径的结论与去向没有分歧（证据都在表里），让用户走两步
  手动流转是纯摩擦；失败/未改进路径保留人工对话框——结论类型与失败
  经验去向需要人判断（回方案评审 or 重试）。
- 归档失败（如阶段已被并发流转）不阻塞结论本身，返回 archived=false，
  UI 保留手动「归档到 Done」作为兜底。

## D-019 · 自动迭代循环：有界委托授权下的连续改码-执行-分析（M6）

- 日期：2026-09-19
- 决定：新增 `iteration_loop.py` 状态机（implement→plan→await_jobs→conclude
  每步落库，Control 重启后自动续跑）+ `iteration_loops` 表 + serve 内
  LoopManager 线程。owner 启动循环即授权一个有界委托：循环内 Plan 以
  `approved_by="iteration-loop"` 自动批准（批准事实与授权来源落 plan 行），
  边界为启动参数（最大轮数 1-10、连续整轮失败上限 1-5、随时手动停止）；
  循环外创建 Plan 仍走 owner 的 spec_hash 批准门，两条路径互不影响。
- 循环语义：每轮 实现 Agent 改代码 → 计划 → 自动批准 → 真机执行 →
  指标对比 → 结论留痕；第 2 轮起把上一轮对比结果与失败摘要注入实现/
  计划上下文（"换思路，不要原样重复无效改动"）。停止条件：任一配置
  超越项目基线（completed，自动结论+归档）/ 轮数耗尽 / 连续整轮失败 /
  人工停止（执行中的轮跑完并记录结论后停）。执行等待有超时保护
  （spec 超时×串行深度+15min，超时取消剩余 Job）。
- 理由：用户定义的"自动迭代"= 跑完一轮自动分析、Agent 自己改再跑，
  直到提升或终止；逐轮人工点批准与该语义冲突。授权从"每计划"上移到
  "每循环"，同时保留完整审计链（每轮 EXP/Plan/patch/失败经验）与
  随时停止权，与"Agent 不能自己批准"的原始规则在委托语义下兼容。

## D-020 · 策略自主 B 期：方向权归 owner，agent 产出轮末 Directive（M6+）

- 日期：2026-09-24
- 决定：授权分三层——**方向权永远 owner**（方案评审给论文/代码定大方向，
  `start_loop` 可显式钉 direction 边界，换方向=owner 动作）；**方向内
  策略归 agent**（每轮 conclude 由策略 Agent 会话产出 RoundDirective：
  下轮单一假设/改法/避免项/放弃子路径/证据引用，经 submit_result 结构化
  提交）；**执行硬顶永远 Python**（轮数/失败链/GPU/墙钟/终止权，现状
  不动）。B 期 Directive 在所有模式自动注入下轮上下文（替代固定模板
  反馈），不逐轮确认；确认概念只存在于"越方向边界"（in_scope=false 或
  机判 drift 信号——改动落点离开前几轮文件集合——B 期只标记告知）。
- 落库：`loop_decisions` 表（issued/adopted/fallback 执行状态，B 级即
  落，与 agent_sessions 平级）；`experiment_backlog` 实验积压表
  （implementer 的 variants 提议跨循环累积，与 failures.md 对称：一个
  记失败、一个记待试，最简清单 UI）；`iteration_loops.direction_text`
  列（缺省=已采纳方案摘要）。
- 理由：P1–P6 后单步能力与过程体验闭环，瓶颈回到轮间策略——机械
  反馈在 EMA/OOM/增强崩盘三个真实案例里烧掉多轮 GPU 时间。B 期不动
  任何决策代码（Directive 只是 extra_context 的更聪明产地），失败回落
  固定模板，循环永不因新层挂死。
- C 期（动 L0，单独开门）：Direction/matrix_hint 在包络（gpu_hours/
  tokens_budget/matrix_scale_cap）内生效；越方向或越包络按三粒度问
  owner（collab/intermediate 暂停问询，heavy 仅越界暂停过夜）；
  keep_baseline_patch 禁止回滚最好改动（patch 系统无分支语义，成本
  不对称）。C 期开工前置条件：B 期 Directive 在真项目上质量达标。
- 详见 `docs/AGENT-STRATEGY-AUTONOMY.md`（v1，七个决策点拍板记录）。

## D-021 · 策略自主 C 期：包络裁决 + awaiting_owner + 最好成绩保护（M6+）

- 日期：2026-09-24（owner 指令"开始下一步"，明示豁免 D-020 的"B 期真机
  验证后再开 C"前置门；若后续 Directive 质量不达预期可随时用包络收紧）
- 决定：在 B 期之上落地三层护栏，L0 状态机的硬规则（轮数/失败链/终止权）
  全部不动：
  ① **策略包络** `envelope_json`（gpu_hours / tokens_budget /
  matrix_scale_cap，启动时可选声明）。GPU 口径 = Σ(已派发计划矩阵规模 ×
  spec 超时) 估算累计；矩阵规模 = job_definition.parameters 各取值数之积。
  裁决点：plan 相位批准（=派发）前查矩阵与 GPU；conclude 继续前查 tokens
  （策略会话 tokens 计入循环累计）。越包络 → awaiting，草案挂起不重起草，
  放行后批准同一草案。
  ② **awaiting_owner 非执行态**：实现为 iteration_loops 标志位（不改
  status CHECK 约束），LoopManager 跳过推进、DB 落库重启不丢、等待中的
  停止仍生效。触发条件：越包络、策略 Directive in_scope=false、drift 机判。
  owner 裁决（POST loop/resolve）：resume 放行（一次性 owner_override，
  防重标记 concluded_plan/strategy_done/resume_plan_id 保证重入不重复
  记录结论、不重跑策略、不重新起草）/ redirect 改方向并放行（换方向=
  owner 动作）/ stop。
  ③ **keep_baseline_patch（D-7）**：conclude 见 anyImproved → 本轮实现
  改动文件并入 protected_files_json（只增不减）→ 下一轮 implementer 会话
  write_file 硬拒回滚（AgentSessionError 提示）；循环级保护，新循环不受
  旧保护约束（git 分支级工作区后再放开为项目级）。
- UI：LoopCard 等待决策横幅（原因 + 放行/改方向并继续/停止）。
- 测试：test_strategy_loop.py 增 5 例（矩阵 cap 挂起→放行批准同草案、
  tokens 挂起→放行策略仅一次、in_scope=false→redirect 改方向、awaiting
  重启不丢且可停、保护文件硬拒+只增不减）。全量 166 绿。

## D-022 · Chat↔迭代整合:chatagent 经 MCP 观察/介入 L1 会话与循环（M6+）

- 日期：2026-09-24
- 决定：autolab MCP 增 8 工具（23→31），Chat 页 chatagent 与 L1 会话世界
  打通：list_agent_sessions / get_agent_session_events（观察会话时间线，
  可总结"Agent 在干什么/为什么这么改"）、inject_agent_session /
  stop_agent_session（实时介入，与 UI 同权）、get_iteration_loop
  （循环轮次/方向/包络/awaiting + 决策链 + 实验积压）、
  start_iteration_loop / stop_iteration_loop / resolve_iteration_loop
  （owner 委托动作）。
- 边界（09 号决策门不变）：仍无 Plan 批准入口（测试断言 agent_tools 不出现
  approve 操作）；循环启动与 awaiting 裁决是 owner 委托动作，MCP 工具带
  confirm 门——confirm=false 只回显参数/暂停原因供用户过目，
  用户明确同意后 confirm=true 才执行；chatagent 提示词同步告知新能力
  与确认纪律。
- 理由：Chat（本地 claude-code，流式可交互）是产品里最像 Agent 的界面，
  但与迭代会话完全割裂——"不能跟正在跑迭代的 implementer 说话"。整合后
  统一为：对话里看会话时间线、把用户的话注入会话、按指令驱动/裁决循环。

## D-023 · 证据链完整性三件套：日志直达 / 指标域门 / 参数敏感性检查（P0）

- 日期：2026-09-24（全部源自当日 MedCLIPSeg 真机教训，见
  docs/ACCEPTANCE-P5-P9-2026-09-24.md）
- 决定与实现：
  ① **失败证据直达**：runner 终态事件（job.failed/timed_out，含 adopt 路径）
  携带 `log_tail`（尾部 2KB，traceback/OOM 随事件进 Control，免 SSH）；
  日志归档到 `{output_root}/{job_id}/runner.log`（与产物同址，重放不覆盖）；
  control `failure_log_tail` 按可靠性顺序取：事件 log_tail →
  train_stdout.log（baseline 约定）→ runner.log。策略层不再被 exit code 瞎眼。
  ② **指标合理性门**（`metric_sanity.py`）：NaN/Inf 拒收；dice/iou/auc/
  acc/f1/precision/recall/map 等比率类指标必须 ∈[0,1]（容差 1e-6），
  percent 命名 ∈[0,100]。接线两处：baseline 数据集 ok 判定（跑通但超域
  = 不通过，Agent 会去修）与 scheduler 指标提取（超域值不作有效指标 →
  比较无 Δ → 结论 inconclusive，保守正确）。真机事故形态（Val Dice=1.9）
  会被自动拦下。
  ③ **参数敏感性检查**（`compute_comparison.paramInsensitivity`）：同组
  Job 仅一个参数取值不同（其余含 seed 全同）且指标逐位相同 → 标记
  「参数疑似未生效，对照结论不可信」；警示进结论分析文本、循环消息与
  策略证据。真机事故形态（hflip 0/0.5 同分）会被自动点名。
- 原则：**宁可无结论，不可信错结论**——三道门都只降低证据等级
  （inconclusive/不通过），不产生错误的研究事实。
- 测试：test_evidence_gates.py 6 例（log_tail/归档/事件优先、域检矩阵、
  baseline ok 门、同分检出、分叉放行）；全量 277 passed（唯一 failed 为
  已知 m5 e2e 端口冲突）。

## D-024 · P1:计划契约核验 + GPU 包络实测估算（证据链上游）

- 日期：2026-09-24（P0 三件套的次日，同源真机教训）
- 决定与实现：
  ① **planner 会话化 + 契约双道核验**：计划起草升级为会话模式
  （planner 角色，只读工具；系统提示词写入 CLI 契约硬纪律——模板每个
  --参数必须在入口 argparse 真实存在，不确定就 grep/read 核实）。产出后
  代码级硬核验 `_verify_cli_contract`：(a) 模板占位符 {{p}} 与矩阵
  parameters 双向一致；(b) 模板每个 --flag 必须在入口脚本文本中出现
  （argparse 定义或字面引用；demo 型 sys.argv 手工解析天然兼容）。
  核验失败带错误重试一次，两次不过 IterationError；会话层失败回落
  单发路径（纪律不变）。--aug 事故（编造参数 4 Job 报废）自此在
  起草期拦截，配合 D-023 门①失败证据直达形成双保险。
  ② **GPU 包络按实测估算**：单 Job 估算 = 项目历史 SUCCEEDED Job 实测
  时长（job_events started→succeeded）中位数 × 1.5 安全系数，封顶
  spec 超时；无历史回落 spec 超时。已用量同样优先按实测累加。
  消除 80 倍高估（13ep 实跑 3 分钟被估 4h）导致的包络形同虚设，
  awaiting 理由标注估算口径（实测中位×1.5 / spec 超时上限）。
- 测试：test_evidence_gates 增 3 例（契约五形态、起草链拦截-重试-放行、
  实测估算与不再误挂）；既有测试 fixture 就地真实化（train.py 写成
  demo 型 sys.argv 入口——契约核验按字面放行该风格，测试矩阵第一次
  与真实契约一致）。全量 280 passed（唯一 failed 为已知 m5 端口冲突）。

## D-025 · P2:envelope 入口补全 + 成本可见性诚实化 + e2e 端口守卫

- 日期:2026-09-25
- 决定与实现:
  ① envelope 入口补全:iteration 启动卡新增「包络(可选)」输入(矩阵
  上限 / GPU 小时,任一 >0 启用);MCP start_iteration_loop 平铺
  matrix_cap / gpu_hours 参数(confirm 门回显包络,确认后落 loop 行)。
  envelope 三入口(UI/MCP/API)齐备。
  ② 成本可见性诚实化:断点续跑循环 tokens_used=0 的疑点闭环——真因是
  GLM key 无效期 implement 全部回落单发,而单发路径无 token 计量(非
  resume bug)。回落分支的 review 消息现标注「[tokens 未计量]」,
  会话路径精确计量、单发路径如实标注,不再出现无解释的 0。
  ③ e2e 端口守卫:test_m5_web 固定 8710/3430 撞常驻 demo 的红色假
  失败消除——端口被占时 pytest.skip(带指引),可用
  AUTOLAB_E2E_WEB_PORT / AUTOLAB_E2E_API_PORT 覆盖(注明换 API 端口
  需重建 web,API 地址为 build 期内联)。
- 测试:MCP envelope 门+落库一例;全量 281 passed + 1 skipped
  (m5 e2e 被 demo 占用时干净跳过,0.02s)。

## D-026 · 根除 REMAINDER 参数顺序陷阱(两次真机事故的共性根治)

- 日期:2026-09-25
- 事故档案:同一根因两次咬人——① 9-24 --aug 事故(REMAINDER 吞参,
  4 Job exit 2);② 9-25 --hflip 事故(恢复备份时顺序回退,参数被
  opts 清洗静默丢弃,两组逐位同分,门③拦下)。本质:argparse
  REMAINDER 入口(yacs opts 风格)把位置参数之后的具名参数吞进
  opts,清洗逻辑"为避免 merge_from_list 报错而丢弃"使其彻底失效。
- 三层根治:
  ① **入口脚本治本(工作区)**:opts 清洗从"丢弃 --flag value"改为
  "**转换**"(int/float 尝试转型后 setattr 回 args)——CLI 位置无关,
  顺序不再是危险面。补丁脚本就绪,待网络恢复推送到 MedCLIPSeg
  train.py(备份 train.py.pre_d026)。
  ② **产品层静态检查**:`_verify_cli_contract` 新增 REMAINDER 顺序
  审计——入口源码含 REMAINDER 定义且命令模板的 --flag 出现在
  yacs 位置参数(如 TRAIN.NUM_EPOCHS)之后 → 起草期拒收。新项目
  直接-entry 形态从此被拦。
  ③ **planner 纪律**:PLANNER_SYSTEM 写入顺序硬规则(两次事故
  教训)。
- 与门③的关系:门③是事后症状拦截(两次实战两次命中,保留);
  本决策把拦截前移到起草期 + 入口治本,三层纵深。
- 测试:test_evidence_gates 增顺序三形态(事故形态拦/顺序正确放/
  无 REMAINDER 不误报);control 全量 181 passed。
- 遗留:服务器侧 train.py 补丁 + 真机验证(故意把 --hflip 放在
  TRAIN.NUM_EPOCHS 之后仍应生效)——网络恢复后执行,验证后本条
  追记闭环。

### D-026 追记:服务器侧闭环(2026-09-25)
train.py 转换补丁已推送(备份 train.py.pre_d026);真机铁证:事故原样
形态(TRAIN.NUM_EPOCHS 1 --hflip 0.5,flag 在位置参数之后)下 recipe
仍打出 optional hflip=0.5——CLI 位置无关达成,根除闭环。工作区
failures.md 同步记录。三层纵深(入口治本/起草期静态检查/门③事后
拦截)全部就位。

## D-027 · 改动冻结:code_ready 循环模式 + implementer code_unchanged 软通道

- 日期:2026-09-25
- 事故档案:复验轮 implement 会话无视 direction「代码已就绪无需再改」声明,
  重做 hflip 为 sitecustomize 钩子(有害,Δ-0.05,翻案成本一晚);同时
  「确认轮只想跑对照不改码」在旧机制下不可能——implement 不写文件即
  报错回落,回落单发必改码。两个缺口同根:产品没有"不改码"的合法路径。
- 决定与实现:
  ① **循环级 code_ready**(DB v18 列;UI 复选框/MCP 参数/API 三入口):
  整循环实验专用——implement 相位直接跳过(轮次仍计数),直进计划;
  LoopCard 显示「改动冻结(实验专用)」徽标。代码问题导致失败时停循环
  走常规循环改码(语义写入 start_loop docstring 与 MCP 描述)。
  ② **implementer code_unchanged 软通道**:submit_result 支持
  code_unchanged:true(验证通过、零改动的合法产出);提示词写入改动
  冻结纪律(「验证而非重做」,事故教训原文);run_implementer_session
  对该声明零改动不再抛错回落。未声明的零改动仍按原语义拒绝。
- 测试:test_evidence_gates 增 2 例(code_ready 跳过实现+循环推进+
  消息留痕;软通道合法返回+零改动文件)。全量 284 passed + 1 skipped。

## D-028 · 方向确立替代方案评审:论文+代码→方向,owner 确认即执行依据(2026-10-06)

- 决定:研究流程收敛为 立项 → Baseline(选环境/数据集)→ **方向确立** → 自动迭代
  (仅 baseline 确认池内数据集)。原方案评审(A/B/C 方案卡、双评分 Agent、报告、
  采纳)入口下线:前端 /review 重建为方向页,后端 proposals/proposal_reviews/
  review_messages 表与端点保留(老项目数据不丢、可回退),进入待废弃。
- 方向确立形态:LLM 一键提炼(论文 PDF(pypdf 前 8 页)+ 代码核心文件 +
  confirmed_datasets 指标 + 研究记忆 → ≤800 字方向陈述:目标/假设/约束/不做
  什么)→ owner 编辑 → 确认。`projects.direction_text/direction_confirmed_at`
  两列(有 text 无 confirmed_at = 草稿态)。
- 双门(OR):`direction_confirmed_at` 或 `accepted_proposal_id` 任一满足即可
  迭代。门槛替换点:start_loop、draft_iteration_plan、implement_proposal_
  changes、agent_session 上下文、_direction_default(优先项目方向)。
  direction_basis() 返回与 proposal 行同构的 dict,下游零改动。
- MCP 增 3 工具(31→34):get_project_direction / draft_project_direction /
  set_project_direction(confirm 门与 start_iteration_loop 同款);chat 的
  MCP env 补 AUTOLAB_CREDENTIALS(方向提炼/探活需要凭据文件)。
- 每轮思路反馈:迭代页新增「本轮思路」卡(改了什么/为什么/结果/下一轮,
  数据=implementation+decisions[0]+comparison+conclusion,附前几轮折叠);
  看板迭代卡摘要改为「第 N 轮思路:策略 Directive 一句话」。
- 理由:owner 反馈方案评审「鸡肋」;V1 实测印证——chatagent 为过这道门自行
  补齐方案/采纳/报告三件事,真正有信息量的输入只有论文与代码。评审的仪式在
  给系统打工;方向 + 每轮思路反馈 + 包络护栏覆盖了同样的安全语义。
- 测试:test_direction.py 增 4 例(草稿→确认流、仅方向可启动循环、未确认被拒、
  看板方向格三态);test_review 看板断言换新文案。全量 203 passed。

## D-029 · 接入型一键 Baseline:工程接入 Agent(onboard 角色,2026-10-07)

- 背景:owner 真实测试暴露缺口——新建项目选好工作区后点「提 Baseline」,只得到
  「进入 Baseline 列」,没有任何 Agent 接手;而工作区里遗留的 autolab_baseline.py
  适配器(口径参数化/指标出口/--hflip 顺序坑)正是一键该自动产出的东西。原
  Baseline Agent 是「修复型」(失败后给补丁),缺「接入型」。
- 决定:agent_session 新增 onboard 角色(写能力会话:read/list/grep/probe/
  write/smoke/submit,预算 16 轮/900s/6 写/2 冒烟):识别工程形态 → 口径核查
  (数据档位参数 + $AUTOLAB_OUTPUT_DIR/metrics.json 指标出口)→ 生成或**复用**
  适配器(优先不动工程源码)→ 口径清单(数据集 × 档位,≤24 条,--epochs 3 冒烟
  口径)→ 选 conda 环境 → smoke_run 自验 → submit_result。
- 编排:POST /api/owner/projects/{id}/baseline/onboard(owner)——拒绝 running/
  无工作区;落库 merge baseline_config_json(datasets/command/conda_env/
  onboarded/adapter)→ 自动刷新体检 → review_messages 留痕摘要。**接入只做
  准备不跑正式基线**——跑与确认仍是 owner 动作(确认语义不破坏)。
- UI:Baseline 页「Agent 一键 Baseline」的第②步由只读 analyzer 升级为 onboard
  (未接入时);已接入显示「已由接入 Agent 适配 · 适配器 …」徽标;会话时间线走
  LiveSessionCard(onboard: 接入 Agent)。失败回落原 analyzer 只读分析。
- 分工定型:analyzer(只读推断)→ onboard(写能力接入)→ fix(失败修复);
  baseline-request 提示改为指向一键接入。
- 测试:test_onboard.py(mock 会话:适配器生成/复用、清单校验、running 拒绝)。

## D-030 · Baseline 接入 = 固定 Workflow + 提议制:onboard 退役(2026-10-08)

- 背景:D-029 的自主接入会话(onboard,16 轮探索+直写适配器+无条件落库)在
  medclipseg-test 实测暴露产品定义错误——接入的本质是**补资料**,每个工程缺的
  资料各不相同,自主探索等于把开放问题交给运气;且写文件无暂存、落库无确认,
  owner 看不到它在哪一步、缺什么。用户重定义:流程写死,agent 只做总揽。
- 决定:Baseline 接入改为**六步固定 workflow**(代码就位 → 资料就位 → 环境就位
  → 入口与口径 → 跑通基线 → 基线确认),状态全部派生(体检+config+run),
  无持久化 workflow 表。三个 agent 角色收敛为:
  1. **提议**(analyzer,只读):读代码 → 入口命令/口径清单(数据集×档位,
     3ep 冒烟档)/环境建议/证据;端点 `POST /baseline/intake/propose`
     **只返回提案不落库**,owner 编辑后「采用并保存」= `POST /baseline/config`
     (保存 datasets ≥1 行即置 `onboarded=true`,确认是用户动作)。
  2. **诊断**(advisor,单发非会话):`POST /baseline/advisor` 读体检+配置+
     工作区顶层 → ≤120 字「缺什么/放哪/下一步」;前端常驻流程助手条
     (六步点列 + 缺口计数)按需调用。
  3. **修复**(fix,不变):跑挂了读日志打补丁自动重跑——适配器生成回归此路。
- 退役:`run_onboarding_session`/`ONBOARDING_SYSTEM`、`POST /baseline/onboard`、
  web `onboardBaseline`、挂载即 merge 的自动分析;「一键 Baseline」按钮改为
  **「一键走流程」**:顺序推进确定性步骤,遇缺口停在对应步骤(资料缺→②,
  口径未确认→自动提议后停④等编辑采用),不再有自主执行段。
- 资料槽 = 体检派生(dataset:N / weights / data_layout + supply 归位路径),
  供给两通道不变:服务器目录符号链接(provideAsset)/ zip 上传(uploadAsset),
  路径输入升级为 FolderBrowserDialog 服务器目录树选择。

## D-031 · 每步 Set 门:owner 显式确认才进下一步,可回退补料(2026-10-08)

- 背景:D-030 的六步状态纯派生(体检/配置/运行事实),缺"owner 确认过这步"的
  语义——用户定义:每步都做 **set** 再进下一步;数据集步可**取消 set** 补充
  更多数据集后重新 set,跑阶段把清单里的数据集都跑一遍。
- 决定:`baseline_config_json.workflow = {code/assets/env/intake: set 时间戳}`;
  `POST /baseline/workflow {action: set|unset, step}`:
  - set 前置 = 前序步全 set + 本步事实校验(code=代码在位 / assets=**重跑体检
    且无失败项**(缺资料即未就位)/ env=已选环境 / intake=口径清单已确认),
    校验失败 409 带原因;
  - unset = 该步及之后全部回退(顺序不变式),Baseline 运行中禁变更。
- 前端:四内容步各带 SetBar(已 set 绿条 + 取消按钮,未 set 显式
  「Set·确认这一步」);前序未 set 的后续步降透明度锁交互(路线图可见);
  「一键走流程」= 顺序自动 Set(服务端逐项校验,不满足即停)。
  run/confirm 是动作门不参与 set;「补充数据集」= unset assets → 补 → 重 set,
  起跑按清单全跑(未就绪自动跳过、补齐后追加的既有语义保留)。

## D-032 · 重构:三段 workflow + 阶段 agent,按职责分层重组(2026-10-12)

- 背景:owner 定调「workflow 与 agent loop 并行」——外层只保留三段(立项 →
  Baseline → 研究),段内交给一个 agent 会话自由推进,不再逐步状态机。旧实现
  (六步 Baseline Set 门 / 四相位迭代循环 / 多角色拆分 / 单发回落 / await 旗标群)
  复杂度失控:每起一次真机事故就长一层 Python 补丁。
- 新形态:`agent/runtime`(等人 / 等 GPU 两种暂停,统一由外部事件唤醒;Python
  验收 finish;跨轮翻篇 NewContext)+ `stages/baseline`(缺材料 ask_human,
  确认即写基线快照)+ `stages/research`(end_round 收束,Python 对照基线判定
  提升;轻量每轮停下等 owner,重量 GPU 小时兜底)。实验提交 `execution/experiments`
  以 agent-run 名义自动批准,派发/GPU 租约/重启恢复沿用 scheduler+Runner。
- 分层:core < infra < workspace < execution < agent < stages < api,依赖只往下;
  test_architecture 守卫依赖方向与跨模块私有导入。`workspace.Host` 是本机/SSH
  的唯一执行与文件抽象;API 单 app + 5 个 router,UserError→409 / NotFound→404
  统一映射;看板改三段、由事实派生(工作区/基线快照/最近会话)。
- 删除:iteration / iteration_loop / agent_session / baseline_queue / baseline_agent /
  chat / llm / server_admin / 旧 http_api、MCP server 与 agent_tools、方案评审/
  方向/Chat/Agents 页面与看板 vendor;依赖去掉 langgraph/langchain/mcp。旧库迁移:
  review_refs_json→refs_json、确认快照→baseline_json(原地,退役表保留不读)。
- 遗留:GLM 真机验证(长工具循环 + 跨暂停续跑)未做,先在真项目上跑通 Baseline
  与轻量研究再合并回 main。

## D-033 · 可评测架构 Phase A:AgentSpec 快照 + 结构化 Trace + 长程安全(2026-10-28)

- 背景:agent 层能力够但无评测面——配置写死在代码里、`agent_run_events` 只给人看,
  `messages_json` 同时当上下文与审计;运行时缺 wall-clock、错误连击、stop 不取消 Job,
  过夜 RSI 会把小故障放大成事故。
- A1 `agent/spec.py`:prompt/tool/model/budget/verifier 在首次模型调用前固化为
  AgentSpec(spec_hash=sha256(sort_keys JSON)),存 `agent_runs.spec_json/spec_hash`,
  之后只读;绝不落 api_key。`GET /api/runs/{id}/spec`。
- A2 `agent/trace.py`:llm/tool/verify 三类 span + 内容寻址 blob(原文不截断,
  llm 输入超 512KB 降级 contextHash+messagesTail);llm input 必须是当步
  `_compact_view` 确切视图。`GET /api/runs/{id}/trace[.jsonl]`。
- A3:`wall_clock_seconds`(running 才计活跃,等人/等 GPU 不计)、
  `max_consecutive_model_errors=8`、终结路径统一 `_cancel_active_jobs`(走
  `repo.request_cancel`,不直改 Job 状态)、max_tokens 截断不执行工具、为每个
  tool_use 补 is_error 的 tool_result 后追加 user 提示、
  `AgentLoopManager(max_concurrent=8)`。旧库逐列 ALTER 迁移(v22)。
- 遗留:research 三档 wall-clock 细分未设,当前统一 48h 安全上限(档位配置在
  run config,细分留 Phase B);真机仍需截断探针验证 GLM 端点兼容性。

## D-034 · Phase B1:统计验证器 + 指标契约 + 防篡改证据(2026-10-28)

- 背景:均值判定无方差口径,heavy「均值过线」即可停;主指标由 agent 在
  `run_experiment.metrics` 里声明且取「第一个命中基线」——agent 可换软指标;
  次指标回退不受罚;评测代码/数据划分无保护,round 结论无结构化证据。
- B1.1 variant/seed 显式元数据:`_clean_jobs` 校验(variant ≤80、seed 非 bool、
  全有或全无、(variant,dataset) seed 去重);`variant_means` 按
  (variant|planId, dataset) 分组,seeds 非空时 n=distinct seed。
- B1.2 `bootstrap_ci`(百分位法、Random(seed) 确定);每组带 stdGain/95% CI/
  significant;heavy 成功 = significant 且均值 ≥ 阈值,light/medium 维持均值语义;
  verdict/journal/_status_suffix 全部带 CI 口径。
- B1.3 指标契约:`baseline_json.metric_contract`(primary/direction/guardrails/
  eval),owner 经 PATCH 固化(校验指标存在、方向合法、max_regress ≥ 0、路径
  工作区相对),`compare_plan` 优先契约 primary,不回落别的指标;
  `evaluate_guardrails` 按 (variant,dataset) 组均值判回退,违规则 end_round
  success=false。无契约行为与旧版完全一致(B1 不强制默认契约)。
- B1.4 `execution/evidence.py`:`hash_paths` 指纹保护路径;end_round 校验
  code/data hash,被改则 ToolError 拒绝收束并记 `protected_paths_changed` 事件;
  report.evidence = {code, plans, jobs, protected, contract, variantMeans,
  evidenceHash(sha256 sort_keys)},journal 末行带证据摘要。confirm/adopt 保留
  契约并重算指纹。
- 遗留:B2 再做 eval 任务套件/CI/平台评测 provenance;guardrail 违规目前不阻断
  会话只判失败,是否强制停止待真机反馈。

## D-035 · Phase B2.1:重复实验拒绝 + 无进展停止(2026-10-28)

- 背景:B1 实测 heavy 在 fixture 上连续 12 轮重复提交相同 3-seed 实验,均值
  +0.0154 但 CI 下界=0,Python 判「证据不足」,agent 却不知道停,烧 GPU 时预算。
- 重复实验拒绝(硬门):`experiments.check_duplicate_jobs` 在 `submit` 的
  `_clean_jobs` 之后调用;签名 = 排序后的 (dataset, command.strip(), variant,
  seed) 元组集合,只与同一 `agent-run:<id>` 名下 **SUCCEEDED** 的旧 Job 组比较,
  完全相同才拒(失败/取消允许重试;换 seed/变体/命令放行)。CLI/人工路径
  (approved_by 非 `agent-run:*`)不拦。
- 无进展停止(软预算):`TIERS.stagnant_rounds`(light 0 / medium 3 / heavy 3 /
  validation 0)。`end_round` 在 champion 计算后比本轮最优均值 vs 历史冠军均值:
  无新提升(`> prev + 1e-12` 才算进展)则累计 `stagnantRounds`,有提升清零并
  随轮次报告持久化;计满 cap 由 Python `Finish` 收束,reason 带连续轮数与当前
  冠军增益,verdict/研究日志附「连续 N 轮无新提升」。
- 取舍:重复门只挡「完全相同的一组」,不挡子集重跑(修一个失败 Job 需整组
  重交);无进展计满时 success=False 结束而非 Pause——过夜无人值守,等人即搁死。

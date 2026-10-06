# AutoLab Lite 交接文档:开启 Iteration 模块

> 生成于 2026-09-19。前情:方案评审模块已完成闭环,本文档供新会话无缝接手 Iteration 模块开发。
>
> **✅ 2026-09-19 当天已完成 Iteration 模块并全链实测通过**(设计决策见 docs/decisions.md D-016):
> - 后端 `apps/control/autolab_control/iteration.py` + 3 个端点(GET /iteration 状态、POST /iteration/plan 生成草案、POST /plans/{id}/conclusion 记录结论)
> - scheduler 指标提取补上 SSH(SFTP)读取 + metric_attempts 防重试风暴——SSH 服务器项目的看板指标因此首次可用
> - 前端 `/iteration` 页(左报告/右五步流程:生成计划→批准→执行→对比基线→记录结论→归档);侧栏 Iteration 指向新页
> - 测试:apps/control/tests/test_iteration.py 7 个用例;全量 control 76 / packages+runner 92 绿
> - 真机闭环已验证:GLM 生成 seed 1/2/3 矩阵计划 → owner 批准 → A6000 执行 → SSH 指标回读 → 超越基线(0.81/0.82 vs 0.80)→ 结论写回 EXP-0002 → 失败经验入 failures.md → 归档 Done
> - 遗留:①"Agent 起实例改代码"已用 LLM 直接改写落地(D-017,`implement_proposal_changes`:读代码→改写→备份→py_compile→失败回滚),claude code 会话式改码(可迭代自测)仍是后续增强;②runner 重装依赖 PyPI(服务器无外网时可从远端 ~/.autolab-runner/runner.json 读 token/hmac 回填 server 行,demo 一次重启不需要重装);③LLM 幻觉兜底只有 schema 校验+一次重试,长报告下 matrix 规模可能偏大,可按需收紧。
>
> **✅ 同日补全「Agent 实现方案改动」并完成第二轮真机闭环**:新方案「接入 configs/config.yaml」→ 实现 Agent 真实修改服务器工作区代码(train.py 改为配置驱动,备份在 .autolab/patches/)→ 计划 Agent 基于新代码生成 `python3 train.py --config configs/config.yaml --seed {{seed}}` 矩阵 → 批准 → A6000 执行(Job 日志可见 `[config] seed=3 lr=0.001 ...`,确证跑的是新代码)→ 对比超越基线 → EXP-0003 结论 supported → 归档。步骤条六步:实现改动→生成计划→批准→执行→对比基线→记录结论。
>
> **✅ 一键结论(D-018,第三轮真机验证)**:对比 improved 时,「Agent 总结并归档」一个按钮完成:结论 auto 推导(supported+promote)+ Agent LLM 总结写回实验文件(偶发失败重试一次,回落模板)+ 自动 confirm→done 归档;不再手动走 confirm/done。未改进路径保留人工对话框(结论类型与失败经验去向需要人判断)。
>
> **✅ 自动迭代循环(D-019,第四轮真机验证)**:「启动自动迭代」后 Agent 自主闭环:改代码→计划→(循环授权内)自动批准→A6000 执行→指标对比→结论留痕→带上一轮结果反馈进入下一轮,直到 指标超越基线(实测第 1 轮 val_dice 0.8348>0.8 即停并自动归档)/轮数耗尽/连续整轮失败/人工停止。状态机每步落库(iteration_loops 表),Control 重启自动续跑;UI 有循环状态卡+随时停止;单步手动路径(实现/计划/批准)仍并存可用。
>
> **✅ 真实项目接入(MedCLIPSeg@CVPR2026,A6000/zwm,2026-09-20)**:Baseline 接入体检上线(依赖缺口/torch版本/预训练权重/HF来源/指标产出/数据结构六项检查,`server_admin._collect_readiness_facts`);真实接入暴露并修复:①conda 激活链 dash 不兼容(服务器 /bin/sh 无 source → 全部改 bash -c 包装);②修复 list_remote_papers 未定义。人工补丁(待产品化):timm<0.9 LayerNorm2d shim、Mac tar 的 AppleDouble(.*) 幽灵文件、BiomedBERT 本地路径化(models/ 目录,trainers 两处)、autolab_baseline.py 适配脚本(训练→解析 stderr 日志 Dice→写 metrics.json)。资源接入:服务器 ~/zwm/MedCLIPSeg-main 已有权重(2.2G)/全数据集(23G)/BiomedBERT,软链复用零传输。冒烟 Baseline(10%×3ep)已通过;正式版(100%×100ep)训练中。**产品待办**:①Baseline 阶段的 Agent 适配能力(体检缺口→Agent 自动打补丁);②代码同步分块/断点续传(慢链路 50MB+ 超时);③tar 打包含 COPYFILE_DISABLE=1;④baseline passedAt 端点显示旧字典。
>
> **docs 全量审计后补齐/确认的遗留(2026-09-19)**:①已补:迭代上下文现在带 `.autolab/context.md` 节选与 `knowledge/failures.md` 最近记录(FR-10"新一轮前读取 context/上一轮实验/失败经验");②未实现 backlog:QUEUED Job 手动调序(FR-4)、DRAFT Plan 批准前展开 Job 预览(HTTP/UI 只显矩阵 JSON,CLI 有 preview)、MCP 工具面缺 probe_server/preview_plan/cancel_job/retry_job/get_run_results 五个、项目页不显示 Git 锚点/无版本锚(FR-2);③已知偏差:NFR"实时连接时无 5s 轮询"现为 4-8s 轮询(无 SSE,M5 已接受形态);业务表已达 15 张(超 04 号止损线 10 张,评审/Chat/循环模块演化所致);01 号"AutoLab 不直接调 GLM API"已被评审/Baseline/迭代/Chat 的直调演进取代(见 D-016~019);④M6 真实使用门:demo 已完成 4 轮连续迭代,目标 10 轮。

## 一、产品一句话

个人研究者的 GPU 实验工作台:GLM Agent 出方案、用户确认、系统在真实 GPU 服务器上排队执行、保留证据用于下一轮判断。UI 壳源自 Multica(D-001 书面许可:内部使用+保留 LICENSE/NOTICE)。

## 二、已完成的研究流水线(全部实测通过)

```
项目登记(/projects,可极简:只名称+服务器;来源=本地工作区|GitHub clone)
→ 编辑补全(PATCH:github_url / local_workspace / 论文 PDF 上传)
→ 提 Baseline(POST /api/owner/projects/{id}/baseline-request)→ 看板进 Baseline 列
→ Baseline 页一键(/baseline):代码就位(自动同步)→ 环境构建(pip回退链)→ Agent 分析(推断命令/数据集/conda/指标文件,带证据)→ 数据集检查 → 起实例跑通
→ 通过边界:exit 0 + 读到指标 → baseline_passed_at → 自动进方案评审列
→ 方案评审(/review,左右分栏):左=对话(贴GitHub/本地路径自动入库+clone,材料真实读入上下文)→ Agent 给【方案A/B/C】卡 → 用户勾选 → 右侧=已选方案:勾选评分Agent→评分(各自提示词+同材料,LLM真实调用)→ 生成评审报告MD(写入工作区 .autolab/reviews/)→ 评审通过
→ accept 落 accepted_proposal_id + 报告状态刷"已采纳(待自动迭代)" + 看板徽章
→ ★ 下一环:Iteration 模块(本文档目的)
```

看板六列(与 Work 栏目一致):prepare→baseline→**review(方案评审,无Plan时基线通过即进)**→iterate→confirm(实验全终态)→done(owner 流转)。
迭代实验有 Plan 后:DRAFT→prepare(待确认);批准+运行→iterate;全终态→confirm;记录结论→done。

## 三、Iteration 模块要做什么(用户原话归纳)

自动迭代 Agent 读**已采纳方案的评审报告**(`projects.accepted_proposal_id` → proposal.report_md,含"下一轮迭代执行指引":目标/基线对照/训练命令参考/指标文件/矩阵建议/成功判据):
1. 基于报告创建实验 Plan(进入现有 Plan/Job 体系:create_draft_plan → 看板 prepare 列等用户批准 → approve 生成 Job → A6000 执行 → 指标回来与基线对比)
2. "至少在1个数据集上跑通并获得基准模型的对应指标算通过"(Baseline 边界,Iteration 类似:超越基线)
3. Agent 可修改工作区代码实现方案(报告即执行计划)——涉及"Agent 起实例去服务器改代码+训练",可复用 Baseline 模块的 SSH 基建

**重要边界(用户多次强调)**:Agent 不能自己批准 Plan(规则3:只有 owner 确认 revision+spec hash 才生成 Job);失败也是资产(保留日志/结论,不删历史)。

## 四、关键现状

### 环境
- repo:`/Users/juan/Documents/autolab/autolab-lite`(uv workspace + apps/web Next15)
- demo:`bash scripts/demo-web.sh`(web 3430/control 8710);重启后跑 `bash scripts/dev-restore.sh` 一键恢复(注册 gpu-serveruser + medseg-demo + Baseline 通过 + 双评分 Agent + GLM key 迁移)。**坑:单杀 web 会连带杀 control,重启 web 后务必跑 restore 脚本**
- 真机 SSH:<A6000 共享机,IP/账号/密码见本机凭据>(共享机,别动别人目录);anaconda3=Python3.11;系统无裸python/pip(已适配);PyPI 时通时断(部署 runner 需网络,失败重试)
- GLM key 已存凭据 `chatagent:api-key`(共享 chat 模块的 key 槽);LLM 客户端:`autolab_control/llm.py`(Anthropic 兼容,open.bigmodel.cn/api/anthropic,model glm-5.3)
- Runner 已部署在 A6000 端口 7810(避开旧 FREEHANDS agent 的 7800);demo 重启后需重装(POST /api/owner/servers/{id}/agent/install)

### Plan/Job 体系(Iteration 直接复用)
- `repo.create_draft_plan(project_id, spec: PlanSpecV1, summary, parent_plan_revision_id)`;approve 需 owner+spec_hash(幂等);展开矩阵→Job;scheduler 派发(0.2s tick,FIFO+lease)
- PlanSpecV1 字段见 `packages/protocol/autolab_protocol/models.py`:hypothesis/change_from_previous/baseline/metrics(direction)/success_criteria/risk_and_fallback/code_anchor/command_template/job_definition(matrix|explicit)/resources/timeout/outputs
- CLI 参考:`autolab plan create --project-id X --experiment-id EXP-000N --command ... --matrix '{"seed":[1,2,3]}' --timeout 120 --max-parallel 1 --artifact metrics.json`;实验文件 `.autolab/experiments/EXP-NNNN.md`(CLI `autolab experiment create`)
- 迭代判定已有:spec.baseline 指向更早实验 → iterate 列;否则 baseline 列(derive_project_stage)

### 方案评审产物(Iteration 的输入)
- `projects.accepted_proposal_id` → proposals 表(title/content/report_md/report_path/status)
- 报告在服务器工作区 `.autolab/reviews/{prop-id}.md`,含执行指引章节
- baseline 指标在 `projects.baseline_run_json`(.metrics)

### LLM 编排参考(方案评审已验证的模式)
- 多轮对话:`llm.chat_multi(credentials, system, messages, agent_key_ref)`;系统提示注入真实材料(`server_admin.build_review_context`:refs clone 到 ~/.autolab/refs + README/核心模块/PDF 文本/工作区入口文件)
- 解析结构化输出的坑:模型会照抄模板尖括号(已剥);【方案X】分割后按空行切分,尾部闲聊归气泡
- 评分编排:POST /api/owner/proposals/{id}/feasibility {agent_ids},逐 Agent 顺序调用,第一行判定 feasible/risky/infeasible

## 五、Iteration 模块建议设计(待用户确认)

1. **触发**:/iteration 页(或 Baseline 式一键)只列 accepted_proposal_id 非空的项目;按钮「Agent 制定迭代计划」
2. **Agent 生成 Plan 草案**(LLM):输入=评审报告全文+baseline_run_json+工作区核心文件+上一轮实验文件(.autolab/experiments/);输出=PlanSpecV1 JSON(严格 schema,失败重试一次);写入 draft plan(不批准!)
3. **用户批准**:走现有看板 prepare 卡→Plan 页 approve(spec hash 确认)——零改动复用
4. **执行**:Job 派发到 A6000(现有 runner;workdir 即项目工作区)——注意 demo 的 train.py 是公式出分脚本,真实验需 Agent 改代码(Iteration Agent 会话=claude code 集成,仍属"最后一块拼图",可先做"读报告→生成 Plan"半自动,改代码留给 Agent 会话接入)
5. **对比基线**:Job 终态后指标 vs baseline_run_json.metrics;成功→建议下一轮(回方案评审);失败→失败经验入 .autolab/knowledge/failures.md

## 六、开发注意事项(踩过的坑)

- **uv run 里嵌套 uv build**:必须 `--out-dir` 显式指定,否则静默无产物(runner 部署用,已修)
- **pytest 泄漏进程**:test_m2_runner_restart_adopts 会留 autolab_runner/control;跑全量测试后 pkill 清理
- **跑 E2E 前先杀 demo**(端口 8710 冲突,出现莫名 KeyError/board 空多半是这个)
- **前端 Playwright actionability 在此 IAB 偶发超时**:用 `el.click()` via evaluate;React 受控输入用原生 setter+input 事件
- **截图我方不可见**:自测用 DOM snapshot + 几何测量(scrollWidth>clientWidth 找溢出)
- **chat.py 是另一会话的 WIP**(Chat 模块,凭据共用 chatagent:api-key),测试失败时 `--ignore=test_chat.py`
- Flex 子项必加 `min-w-0`,长无空格文本会撑爆布局(刚修过)
- demo 每次重启=全新 DB;`scripts/dev-restore.sh` 恢复一切(见上)

## 七、验证基线

- 后端 74+ 测试全绿(`uv run pytest apps/control/tests --ignore=.../test_chat.py`)
- 前端 `npm run build` 过;E2E `tests/e2e_local/test_m5_web.py`(先杀 demo)
- 浏览器全链自测范式:见上一轮"自测完成"消息的 7 步表

## 八、当前 demo 状态(随时可续)

medseg-demo:方案「接入 configs/config.yaml 做参数化实验管理」已采纳+报告已生成,看板亮"等待自动迭代创建实验计划"。owner token 在 /tmp/autolab-demo-r3.log(重启后以新 log 为准)。工作区 ~/autolab-dev/medseg-fake(本地)已同步到服务器 ~/autolab-projects/demo。

## 九、联合基线(baseline 跑了哪些数据集,迭代就能选哪些)——2026-09-20

产品语义(owner 定的硬规则):
- **配置**:`baseline_config_json.datasets = [{name, command, dataset_path}]` 是真相源;
  没有该字段时从旧单命令字段合成一条(名字=dataset_path 末段,run 目录保持旧布局
  `.autolab/baseline`——已跑过的基线记录原样可用)。新数据集的 run 目录是
  `.autolab/baseline/<slug(name)>/`。
- **跑**:`baseline_queue.start_run` 按清单顺序把未跑通的数据集各跑一遍;
  失败即停(队列保留,修复后续跑);serve 进程有 BaselineQueueManager 线程(5s tick)
  负责推进+落库,HTTP 轮询只读不写(避免双写导致重复起跑)。
- **确认门**:全部数据集 finished+exit0+metrics 才 confirmable;`confirm_pass` 时把
  各数据集指标/命令快照进 `projects.baseline_confirmed_json`(v13 迁移)——
  这份快照就是自动迭代的**数据集可选池**。
- **迭代**:draft_iteration_plan(datasets=子集 ⊆ 快照池;缺省全部)。多数据集时 LLM 必须
  输出 `commands: {数据集名: 命令}`(解析+覆盖校验,缺了重试);job 展开成 ExplicitJobs
  (每个 job 自带 dataset+渲染完参数的完整命令),command_template 是
  `bash -c "…conda activate…; exec {{command}}"` 占位形式。对比按数据集各自基线分组判
  improved(improved=任一数据集超越其基线)。循环 start_loop 同样收 datasets 并逐轮透传。

关键代码:`baseline_queue.py`(全部队列/确认门/快照逻辑)、`server_admin.baseline_datasets/
start_baseline_run(run_dir, command)/baseline_run_state(run_dir)`、`iteration.py` 的
`_explicit_jobs_for_datasets/build_parametrized_command_template/compute_comparison`。

前端:Baseline 页有「数据集清单(联合基线)」编辑器+逐数据集进度表(重跑按钮)+
1/N 跑通计数;Iteration 页在确认池>1 时显示数据集勾选 chips(循环与单轮计划共用)。

测试:tests/test_baseline_queue.py(5 个:合成/顺序队列/确认门/失败停/单数据集重跑)
+ test_iteration.py 联合迭代段(5 个:commands 解析/ExplicitJobs 展开/池外拒绝/
循环校验/分数据集对比)。全套 126 测试。

## 十、会话状态快照(2026-09-22,对话压缩前留档)

### 运行环境
- control: `nohup .venv/bin/autolab-control serve --db /tmp/autolab-web-tJ7tA0/control.db --credentials /tmp/autolab-web-tJ7tA0/credentials.json --poll-interval 0.2 --http-port 8710`(cwd=autolab-lite 根)
- web: apps/web 下 `npm run start`(3430);改前端后需 build+重启 web,改后端需重启 control
- 测试:apps/control 下 `uv run pytest tests/` = **139 全绿**;DB schema **v14**
- 根目录 tests/e2e_local 是旧 M1-M5 遗留(端口冲突会失败,与代码无关,勿删)

### 真项目 MedCLIPSeg(proj-547cba85d-3870903b)当前态
- 基线清单 3 行:Kvasir(全量,已确认 val_dice 0.9016)/ **Kvasir10 已跑完 0.7381 待「纳入基线」** / Kvasir25 待跑(点行内「跑」或主按钮)
- TG-Fusion 方案已废弃:循环停止、**工作区已回滚到基线代码**(restore-code API:`POST /iteration/restore-code`)
- 下一步:跑 Kvasir25 → 纳入基线 → 评审页聊新方案采纳 → Iteration 勾 Kvasir10+Kvasir25 开循环(每轮两数据集各一 job,各比各的基线)
- 共享 GPU 常被同事占用(38G/49G);OOM 已进失败记忆,Agent 会自适应降 batch

### 本会话落地的功能(全部已部署+测试)
1. 环境探测 _MAX_ENVS 10→50(字母序截断丢 zwm 的 bug)
2. 联合基线:config.datasets 清单/顺序队列(BaselineQueueManager 5s tick,HTTP 轮询只读)/确认门=全部跑通/追加确认合并快照(baseline_confirmed_json)
3. 逐数据集体检(绝对路径+文件数+大小,缺失给放置指引);保存清单自动触发检测
4. 评审页:流式对话(/review/chat/stream,NDJSON)+正在思考计时气泡+打字机;评分 Agent 分步引导(1选Agent→2评分→3报告→4通过)
5. GLM 修复:thinking disabled(思考块吃光 max_tokens 导致空回复)+4 次重试;失败/空回复回滚用户消息
6. seed 对齐(3 层:范围约束/提示词规则9/草案校验+重试)——迭代矩阵只能用基线跑过的 seed
7. 循环默认不限轮(max_rounds=0);三粒度模式 collab(2轮)/intermediate(5轮)/heavy(不限轮+Δ≥0.01 才停,小幅提升继续);终局 LLM 分析(为什么提升/没提升)存 loop.summary
8. 失败日志尾部注入:轮内反馈 + failures.md(OOM/traceback 不再丢失;iteration.failure_log_tail 共用)
9. analyze_workspace:物理数据目录扫描+同名 config 匹配自动填清单;合并保存(人工 conda_env/清单优先);页面清单空时自动检测填入
10. Baseline 页:大号「跑 Baseline(N 个待跑)」主按钮;每行常驻 跑/重跑 chip;回滚横幅已移除(能力留 API)

### 关键语义(勿回退)
- Agent 只产 DRAFT 不批准;循环内 approved_by=iteration-loop 可审计
- 迭代 seed 必须与基线对齐;数据集只能选确认快照池内的
- 失败也是资产:结论/失败经验/补丁备份全留痕;确认通过是人工动作
- GLM key 在 chatagent:api-key;共享服务器别动别人目录

### Backlog
- analyze 接 LLM(嵌套 config 字段识别,MedCLIPSeg 的 yacs 结构规则法抓不到)
- MCP 工具缺 5 个;代码同步分块断点续传;重型模式阈值 UI 可调;迭代对比用项目级基线(部分已做)

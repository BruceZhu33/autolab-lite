# 实现偏离日志（deviation-log）

格式：日期 | 契约位置 | 冲突/缺口描述 | 实践决定 | 理由。攒批修订契约，不为迁就契约扭曲实现。

---

| # | 日期 | 契约位置 | 冲突/缺口描述 | 实践决定 | 理由 |
|---|------|----------|----------------|----------|------|
| 1 | 2026-09-13 | 设计 §1/D14（SSH 隧道为唯一 sidecar↔agent 通道） | 开发/测试机无局域网 GPU 服务器时无法端到端验证纵切 | 传输层双模：`ssh`（生产，asyncssh 隧道+SFTP）与 `local`（同机直连，subprocess+本地复制） | 生产路径不受影响；local 模式使"每完成一个纵切段在真实运行上验证"成为可能 |
| 2 | 2026-09-13 | contracts/schema.sql `servers` | 契约无 transport_mode / agent_port / agent_token_ref 列 | sidecar migration `0002_server_transport.sql` 增列 | agent 端口与连接模式无处安放；攒批修订 schema.sql |
| 3 | 2026-09-13 | 选型 D48（平台适配器：凭据三平台） | M1 仅实现 macOS 钥匙串（security CLI） | FileStore 兜底（数据目录 0600 JSON，`AUTOLAB_CRED_STORE=file` 强制） | Win/Linux 钥匙串适配器随三平台验收落地（M1 后置） |
| 4 | 2026-09-13 | openapi `/final-confirmation/read`、batches 域 | M1 无 test scope 评测与批次（M4 域） | API 不暴露；不变量②的库级 trigger（evaluation_results gate / test_result_releases insert-only）建库即生效 | 里程碑边界；不变量未降级 |
| 5 | 2026-09-13 | 需求 D20（无 conda 自动补装 Miniconda） | local 模式（开发机 macOS）装 Miniconda 不合理 | ssh 模式实现 Miniconda 自动补装；local 模式回退 `python3 -m venv` 等价环境（environment_versions.spec_json 记 tool） | 真实服务器均有 conda；开发机用 venv 即可跑通链路 |
| 6 | 2026-09-13 | 状态机 run 层（TRAINING→EVAL_QUEUED 附 evaluation_requests 创建） | M1 无受保护评测执行器（D29 执行器 M2 落地） | attempt_finished 边推进 run 至 EVAL_QUEUED，但不创建 evaluation_requests；attempt 失败亦不做 D23 自修复（run 停留 TRAINING 等人工） | 状态机合法边不变；评测实体引入时补全 tx 附加写入 |
| 7 | 2026-09-13 | 需求 [5] R02（agent 自计量 GPU 秒） | agent v0 无 GPU 计量接口 | RUNNING 期间墙钟秒代理 GPU 秒（`metering: wall_clock_m1`；execution_cap 硬截止语义不变，ADD-5 路径已实现并测试） | M2 接 nvidia-smi/DCGM 真计量；预算不变量口径不受影响 |
| 8 | 2026-09-13 | 需求 [1] BASE 来源（GitHub URL 或本地） | M1 仅实现本地目录复制快照 | github 克隆后置（`base_source_kind` 枚举与库列已就位） | 用户 M1 演示用本地目录；克隆实现 trivial 随需要补 |
| 9 | 2026-09-13 | 状态机 run 层 ANALYZING/PATCHING/SMOKING/SNAPSHOTTING | M1 无 LLM（M2 引入 RD-Agent） | 手动触发时沿合法边快速通过（state_events.reason.mode=m1_manual, skipped_llm=true） | 不绕过状态机（全部经 sm_legal_transitions 合法边+留痕） |
| 10 | 2026-09-13 | 设计 §5（manifest_hmac 校验、§4.5 三执行身份沙箱） | M1 手动触发场景先明文跑通（开工指令授权） | HMAC 生成/校验函数就位并联调（agent `--require-hmac` 强制开关默认关）；沙箱（systemd transient unit）后置 | 开工指令："manifest HMAC 签名先做生成与校验函数，agent 侧完整沙箱可后置" |
| 11 | 2026-09-13 | 选型 TD12（Tauri×Next 静态导出） | 本会话机器无 Rust 工具链；浏览器直连 sidecar 有跨源 | 开发期：Next dev server + sidecar loopback CORS（token 仍把守）+ `/api/autolab-config` 桥；Tauri 壳保留 starter 脚手架未编译 | 生产仍为 Tauri 壳同源静态导出（sidecar 已预留 `/app` 静态挂载点） |
| 12 | 2026-09-13 | 需求 D43（划分有效性校验） | M1 无独立校验器实现 | split_validations v1 = 「全部副本 manifest 校验 PASSED + 划分登记入库」（四元组结构与 tx_freeze F2 查询即契约形态） | 完整校验器（对象交叉/预处理拟合范围）M2 随评测执行器落地 |
| 13 | 2026-09-13 | 需求 [0]（nvidia-smi 探测 GPU） | local 开发机无 nvidia-smi | 增手工登记端点 POST /servers/{id}/gpus | 真实服务器走 nvidia-smi 自动探测（已实现） |
| 14 | 2026-09-13 | 需求 D20（无 conda 自动补装 Miniconda / conda create 建环境） | M1 实测目标服务器（192.0.2.46）**无出网**：conda create / pip install 均不可用，机器上已有 25 个科研环境 | migration `0003_project_env_reuse`：`projects.env_reuse_name` 列 + 立项参数 `conda_env`；环境搭建支持复用服务器现成 conda 环境（`conda env list` 本地解析 bin_dir，跳过建环境与 pip） | 无出网是既成事实；复用现有环境是该机器上唯一可行路径，D20 语义（"检测到即用"）自然延伸 |
| 15 | 2026-09-13 | 设计 §5（gpu-agent 部署形态） | 服务器无出网 → agent 依赖（fastapi/uvicorn）无法在线安装 | 离线部署程序：控制机 `pip download --platform manylinux2014_x86_64` 取轮子 → SFTP 推送 → `pip install --no-index --find-links --target pylibs` → `PYTHONPATH=pylibs` 运行；依赖注入不污染既有 conda 环境 | 与 #14 同因；该部署路径已实测成功（真机 A6000 验收即用此法） |

## 与契约一致（零偏离）的核心面

- **事务模板**：`s_cas / tx_usage / tx_settle / tx_drain_* / tx_authorize_dispatch / tx_ingest / tx_ingest_snapshot / tx_freeze` 逐字移植 harness（guard/错误码/幂等语义一致）——`tests/test_tx_contract_parity.py` 9/9
- **建库**：契约 `migrations/0001_init.sql` 原样执行（49 表 + 129 合法边 + 全部 trigger，含防御纵深；未做 trigger 降级）
- **T-FREEZE**：F1–F5 全量校验（jsonschema Draft202012 + app constraints + 显式绑定 + 反连接副本检查）
- **预算**：双层额度（execution_cap + margin 计 R）、C+R≤B、overage 禁新授权、DRAINING 禁 reserve、结算释放——parity + e2e 双验证
- **幂等**：dispatch_id 权威哈希比较重放/冲突；agent_inbox 同 seq 载荷 hash 比较；usage_events 单调 trigger

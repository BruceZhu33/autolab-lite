# Step 1 多卡真机与本地模拟双模验收指南（`local-sim` / `ssh-lab`）

本指南说明如何使用内置双模验收套件（`autolab-control acceptance` / `scripts/run_step1_acceptance.py`）对 AutoLab 控制面、多卡调度器、Runner 进程隔离及**零 Token 确定性全量复核（Zero-LLM Validation）**流水线做一键闭环验收。

---

## 一、快速选择运行模式

| 模式 | 适用场景 | 依赖条件 |
|---|---|---|
| `--mode local-sim`（默认） | 开发机离线联调、CI 回归、不在实验室时验证全套并发状态机 | 本机 `.venv`（Python 3.11+），**无需物理 GPU** |
| `--mode ssh-lab` | 回到实验室连接真实多卡 Linux GPU 服务器做终验 | 控制机可 SSH 登录服务器且可访问服务器 Runner 端口（默认 `7810`）；服务器具备 NVIDIA 驱动（`nvidia-smi`）与 Python ≥ 3.10（若需 `PYTHONSAFEPATH=1` 沙箱，评测环境需 Python ≥ 3.11） |

---

## 二、现在即可运行：无 GPU 本地 4 卡槽位真进程模拟（`local-sim`）

在仓库根目录执行：

```bash
.venv/bin/python scripts/run_step1_acceptance.py \
  --mode local-sim \
  --gpu-slots 4 \
  --report-out /tmp/step1-sim-check.md \
  --json-out /tmp/step1-sim-check.json
```

也可直接通过 `autolab-control` CLI 调用：

```bash
.venv/bin/autolab-control acceptance \
  --mode local-sim \
  --gpu-slots 4 \
  --report-out /tmp/step1-sim-check.md
```

预期终端输出 `PASS (6/6)` 且退出码为 `0`。

---

## 三、回实验室一键真机多卡实测（`ssh-lab`）

### 1. 前置检查
1. 控制机（本机）已安装 `uv` 与项目虚拟环境 `.venv`（`runner/install` 会在本机打包 `autolab-protocol` 与 `autolab-runner` wheel 并通过 SFTP 部署到远端 `~/.autolab-runner/venv`）。
2. 远端 GPU 服务器：
   - SSH 可连通，`nvidia-smi` 正常输出物理卡列表；
   - 防火墙或局域网放行控制机访问远端 `--runner-port`（默认 `7810`，避开旧 `7800` 端口）；
   - 目标训练/评测 Python 环境为 **Python 3.11+**（`S1-K5` 依赖 `PYTHONSAFEPATH=1` 阻断工作区恶意模块劫持）。

### 2. 单命令实测（SSH 私钥认证，推荐）

```bash
.venv/bin/python scripts/run_step1_acceptance.py \
  --mode ssh-lab \
  --ssh-host <GPU服务器IP或域名> \
  --ssh-port 22 \
  --ssh-user <SSH用户名> \
  --ssh-key ~/.ssh/id_ed25519 \
  --conda-env <远端训练Conda环境名,可选> \
  --remote-root ~/autolab-step1-acceptance \
  --runner-port 7810 \
  --report-out ./step1-lab-acceptance-report.md \
  --json-out ./step1-lab-acceptance-report.json
```

> **可选参数说明**：
> - `--gpu-slots <N>`：不传时自动按 `nvidia-smi` 探测到的物理卡数全槽位并发测试；若实验室机器有 8 张卡但只想划 4 个槽位压测，可显式传 `--gpu-slots 4`。
> - `--ssh-password` / 环境变量 `AUTOLAB_SSH_PASSWORD`：服务器未配公钥时可用密码认证代替 `--ssh-key`。
> - `--python-bin /path/to/python`：不依赖 `conda activate`、直接指定远端解释器绝对路径时使用。
> - `--skip-restart`：当实验室服务器正与他人共享、不希望在 `S1-K4` 中重启远端 `autolab-runner` 守护进程时传入（此时仍会完整测试本地 Control `Scheduler` 断线重连对账）。
> - `--gates S1-K1,S1-K2`：按需只跑指定关卡用于快速复测定位。

---

## 四、六大验收关卡（`S1-K1` ～ `S1-K6`）核验内容

1. **`S1-K1`（`env_and_runner_probe`）**：
   - SSH TOFU 主机指纹固定 + `runner/install` 自动建独立 venv 部署 + `/healthz` 探活 + `nvidia-smi` 物理卡 UUID/显存盘点；
   - 验证训练子进程环境彻底剥离控制面敏感变量（`ANTHROPIC_API_KEY`、`AUTOLAB_API_TOKEN`）并准确注入 `AUTOLAB_JOB_ID` / `AUTOLAB_SEED`。
2. **`S1-K2`（`multi_gpu_slot_isolation`）**：
   - 同时派发 `N = gpu_slots` 个并发带同步屏障的 Job，验证单调时钟运行区间真实重叠（`overlap_window_seconds > 0`）；
   - 验证 `CUDA_VISIBLE_DEVICES` 严格不交集（`0..N-1`），并在 `ssh-lab` 下校验 `nvidia-smi -L` / `libcuda` 可见的物理 GPU UUID 互不冲突且单卡隔离。
3. **`S1-K3`（`pgid_reaping_and_busy_upgrade_guard`）**：
   - 启动派生后台孙进程（`sleep 60`）的运行态 Job；
   - 验证在途任务期间非强制 `POST /api/owner/servers/{id}/runner/install`（`{"force": false}`）被 `HTTP 409` 拦截；
   - 调 `/cancel` 后验证状态收敛为 `CANCELLED`、GPU 租约归零，且主进程与孙进程 PID 在宿主上均已彻底消亡。
4. **`S1-K4`（`mid_run_restart_reconciliation`）**：
   - **(4a)** Job 运行中途杀死并重启 Control `Scheduler`，验证凭原 `dispatch_id` 无重派幂等收敛至 `SUCCEEDED`；
   - **(4b)** Job 运行中途重启 Runner 进程，验证 `_watch_adopted` 跟踪存活孤儿进程直至退出、完整保留 `runner.log` 与 `metrics.json`、释放 GPU 租约，随后经 `/retry` 恢复至 `SUCCEEDED`。
5. **`S1-K5`（`anti_tamper_and_locked_platform_eval`）**：
   - 双数据集 Git 项目（`ds_alpha`、`ds_beta`）绑定只读平台评测契约（`.locked_eval`）；
   - 在 `remote_workdir` 植入一被导入即抛异常的恶意 `sitecustomize.py`，验证训练与自动 `platform-eval`（`PYTHONSAFEPATH=1`）完全免疫工作区劫持，且篡改 `eval_locked.py` 会被 `protected_state` 拦截；同时验证产物拉取（SFTP/本地）SHA-256 严格一致。
6. **`S1-K6`（`champion_to_zero_llm_validation_and_adopt`）**：
   - 在凭据库**完全不含 `"llm"` 条目（0 API Key）**的前提下，生成冠军提交并故意破坏 `remote_workdir/train.py`；
   - 调 `/research/validate`（2 数据集 × 3 独立留存种子 `101..103`），验证自动从干净 `validation_workdir` 一次性派发 6 训练 + 6 平台评测 Job，以 `steps == 2`、`tokensIn == 0`、`tokensOut == 0` 完成复核，并通过 `/research/adopt` 晋级至基线 `v2`、写入 `.autolab/research_journal.md`。

---

## 五、常见故障排查表

| 现象 / 报错关卡 | 常见原因 | 排查与修复建议 |
|---|---|---|
| `S1-K1` 报 `Runner 健康检查失败` / `Connection refused` | 服务器防火墙拦截 `--runner-port`（默认 `7810`）或端口被占用 | 在服务器确认 `ss -tlnp \| grep 7810`；若端口冲突改传 `--runner-port 7815`，并放行局域网入站规则 |
| `S1-K1` 报 `pip 安装失败(需要服务器能访问 PyPI)` | 实验室机器无外网或 PyPI 镜像不可达 | 在服务器配置 `~/.pip/pip.conf` 指向校内/内网镜像源后重跑 |
| `S1-K2` 报 `overlap_window_seconds <= 0` 或槽位数不符 | `--gpu-slots` 设为 `1` 或大于实际空闲能力 | 确保多卡机器 `--gpu-slots >= 2`（或不传以自动按 `nvidia-smi` 物理卡数探测） |
| `S1-K5` 报 `PYTHONSAFEPATH` / `sitecustomize` 异常 | 远端 `--conda-env` 或 `--python-bin` 指向了 Python 3.10 及更早版本（不支持 `PYTHONSAFEPATH`） | 通过 `--conda-env` 或 `--python-bin` 指定 Python **3.11+** 环境 |
| `S1-K4` 在共享服务器上不想中断他人任务 | `S1-K4b` 默认会重启远端 `autolab-runner` 守护进程以实测孤儿接管 | 追加 `--skip-restart` 参数即可跳过远端 Runner 重启，仅执行本地 Control 重启对账 |

# RD-Agent 适配性评估（尽职调查 spike）

- 日期：2026-09-12；对象：RD-Agent-main（官网 zip，MIT，Python≥3.10，534 个 py 文件 / 约 6.5 万行）
- 问题：能否作为"科研训练自动化产品"的底子？库依赖够不够，哪里要动刀？

## 一、源码事实（证据）

| 我们的关切 | RD-Agent 的机制 | 证据 |
|---|---|---|
| 怎么接入新领域 | `Scenario(ABC)` 是**描述性接口**（background/source_data/output_format 等，主要为 prompt 供给），真正行为在 Workspace/Strategy 实现里 | `rdagent/core/scenario.py:6` |
| 执行在哪里换 | `Workspace(ABC).execute()` 是执行抽象；自带 `create_ws_ckp/recover_ws_ckp`（工作区检查点） | `rdagent/core/experiment.py:99,117,123` |
| 实际进程怎么跑 | 官方场景用 **docker 工作区**（app/utils/ws.py 等）；subprocess 集中在 utils/env、core/utils | `rdagent/app/utils/ws.py`、`rdagent/utils/env.py` |
| 循环与终止 | `EvolvingStrategy.evolve_iter()` 逐步演化 + `EvoStep`；终止信号在 `Feedback.finished()/is_acceptable()` | `core/evolving_framework.py:61,66`、`core/evaluation.py:15,22` |
| 评估可否替换 | `Evaluator(ABC).evaluate() → Feedback`，完全可替换 | `core/evaluation.py:42,53` |
| LLM 多供应商 | OpenAI 兼容 base_url/key 全可配（chat/embedding 分开配） | `oai/llm_conf.py:63-85` ✓D11 |
| Web UI | **Vue3 + Element Plus + vue-echarts**（注意：与 TD1 的 React+AntD 不同） | `web/package.json` |
| GPU/队列/预算 | **无任何现成实现**（components/runner 为空，资源模型是 docker 单机） | `rdagent/components/runner/__init__.py` |

## 二、需求映射评分（库依赖 + 自定义场景模式）

| 需求 | 可行性 | 说明 |
|---|---|---|
| 执行换成 SSH→GPU 服务器（D14/D15） | 4/5 | 自定义 `Workspace.execute()`：rsync 快照 → gpu-agent 派发 → 轮询回收。不碰核心；官方 docker 路线仅是其自带场景的选择 |
| 受保护评测执行器（D29） | 5/5 | 实现 `Evaluator` 接口即可整体替换，接口干净 |
| 自定义终止/预算（D32/D27/Q02） | 4/5 | `Feedback.finished()` 表达终止；预算账本（C+R≤B）放在我们包住 `evolve_iter` 的产品层循环控制器里 |
| 每轮版本绑定（D10/D40） | 4/5 | 自带 ws_ckp + 链式工作区；run↔commit/env/data/plan 四绑定在我们 wrapper 的 DB 层补齐 |
| [3] 评审对话 / P1–P7 页面 | — | 它不提供，本来就是自建 |

**必须 fork 才能做的**：暂未发现硬性需求。唯一风险点：若直接继承它的 finetune/kaggle 场景（其 app 层绑 docker），需绕开自写场景——从零写自己的 Workspace 反而干净。

## 三、结论

1. **库依赖起步可行**（评分 4+），fork 风险后置到"插件表达不了时"。
2. **架构定位**：RD-Agent = 迭代引擎内核（proposal→coder→workspace.execute→feedback）；我们的产品壳（FastAPI+React、gpu-agent、预算账本、评测协议、DB）包在外面。
3. **一个待决项——前端**：它自带 Vue3+Element Plus 的 web，与 TD1（React+AntD）冲突。建议 M1 直接先用它自带的 web/trace 起步验证引擎链路；正式产品壳（P1–P7 它覆盖不了）仍按 TD1 自建 React，它的 Vue web 只当 M1 脚手架和参考。

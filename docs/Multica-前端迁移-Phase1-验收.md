# Multica 前端迁移 · Phase 1 验收记录

- 日期：2026-09-16
- 依据：`docs/Multica-前端源码迁移方案-v0.3.md` §8 Phase 1、§9 第一个 Sprint、§10 停止条件
- 结论：**Phase 0 ✅ / Phase 1 通过 ✅（附带遗留清单）**，源码迁移路线成立，可进 Phase 2

## 1. 通过门核验（§8 Phase 1）

> 在完全没有 Multica server 的情况下，能完成「看板 → 项目 → 评论 → Agent 更新 → Proposal → Run」的演示闭环。

**✅ 达成。** 2026-09-16 全链路 GUI 黑盒走查（1440×900，sidecar + Next dev，无 Multica Go/PG/daemon）：

| 环节 | 结果 | 证据 |
|---|---|---|
| 六列看板（准备/基线验证/方案评审/自动迭代/最终确认/已完成） | ✅ 卡片按阶段路由 | `01-board-clean.png` |
| 准备：代码库确认过门 + 评论发布 | ✅ LAB-5 过门进基线；评论上时间线 | `02-projects-prep.png` |
| 基线：REPAIRING「需要你」卡态 + 结果文件夹完整性 + 对话意图 | ✅ deviation #32②③ 呈现完整 | `04-baseline-lab3.png` |
| 评审：对话/方案 v1/三评分过线/逐节终审/过门 | ✅（过门方向 bug 当日修复，见 §3） | `05-review-lab4.png` |
| 迭代：轮次表/指标趋势/预算/定向指令；EVAL_QUEUED 停滞卡态 | ✅ LAB-1/LAB-6 | `06-iterate-lab1.png` |
| 确认：final-read 一次性释放 → **二次释放被 guard 拒绝** → 报告 → 归档 | ✅ 不变量②前端呈现 | `07/08-*.png` |
| 已完成：Δ vs BASE 报告卡 + 归档信息 | ✅ | `09-reported-lab2.png` |
| 真 sidecar 页：GPU 遥测（两台真机）/ 平台助手（真 LLM + SSH 白名单探测 nvidia-smi@4090D）/ 设置 | ✅ 只读无副作用 | `10/11-*.png` |

## 2. Sprint 1 清单核验（§9）

| 清单项 | 结果 |
|---|---|
| 选定并记录 Multica commit、许可证和来源 | ✅ commit a3318b68 冻结，provenance 随切片入库 |
| 导入前端可构建切片到 `frontend-next/` | ✅ |
| install / dev / typecheck / production build | ✅ 全部通过（build：编译 29.8s、静态页 24/24；typecheck 7/7；当日含 P1 修复后复跑） |
| 建立 `FixtureResearchPlatform` | ✅ |
| 移除对远端 auth/workspace 的启动时强依赖 | ✅ 可独立运行（遗留：WS 仍尝试连 :8080，见 §4） |
| 跑起 Multica-derived Shell | ✅ |
| Board 改六列科研流程 | ✅ |
| Issue detail 改 Project Workspace | ✅（超出清单：六个阶段模块全部落地） |
| 保留评论 composer 和 transcript 交互 | ✅ |
| fixture 演示 Agent question / progress / Proposal / Run /「需要你」 | ✅ |
| 1440×900 与 1024×700 两种桌面尺寸验收 | ✅ 两种尺寸走查看板/双栏工作区/确认页，无重叠溢出（1024 下看板 3 列可见 + 横向滚动，与 1440 同设计取舍） |

## 3. 验收当日发现并修复（P1）

1. **Multica 营销问卷弹窗**（SourceBackfillModal）Skip/Submit 打不存在的 `PATCH /api/me/onboarding`，Close/ESC 不消失——已从 `dashboard-layout.tsx` 摘除挂载（v0.3 §4.3 onboarding/marketing 删除范围，组件文件物理删除归 Phase 5）。
2. **评审过门方向反了**（送去基线验证，违反 deviation #34 顺序）——`review/[id]/page.tsx` 文案/toast + `fixture/handlers.ts` 状态改 `"iterate"`，浏览器复验 LAB-4 过门落自动迭代、迭代工作区正常起步。

修复后复验：typecheck 7/7、auth callback 测试 20/20、弹窗不再出现（清 localStorage 计数后验证）。

## 4. 硬评审（§10 停止条件）

五条停止条件**均未触发**：无需模拟大量 server 行为（fixture 边界清晰）、auth 删除后视图边界清晰、核心页面无大规模重写、构建调试成本可承受、许可证与单人自用形态不冲突。**源码迁移路线判定成立。**

## 5. 遗留清单（不阻塞 Phase 1，去向明确）

| # | 问题 | 去向 |
|---|---|---|
| L1 | WS 永久重连 `localhost:8080`（Multica realtime 遗留） | Phase 2（sidecar 失效通知替换） |
| L2 | fixture 状态整页刷新即重置（浏览器内存态） | Phase 2 换 SidecarResearchPlatform 后自然消失 |
| L3 | 两个 fixture schema 校验失败（inbox/unread-summary、issues/child-progress） | Phase 2 替换 query 时顺带清理 |
| L4 | 看板顶栏「0 agents working」与卡片「Agent 工作中」口径矛盾 | Phase 2（真数据后重新对口径） |
| L5 | 助手徽标「0/3 GPU 空闲」登记态 vs 实测态口径差 | Phase 2/攒批（遥测轮询设计） |
| L6 | dev server 内存阈值自动重启（走查中发生一次，页面白屏） | 观察项；生产 build 不受影响 |
| L7 | 1440×900 下六列需横向滚动（4 列可见） | 产品取舍确认（Linear 式看板惯例） |
| L8 | Agent 消息 Markdown 星号原样显示（`**0.803**`） | 小修（fixture 剧本字符串或渲染层） |
| L9 | title 仍为 Multica、`/autolab` 根路径 404、Discord 链接、设置页 Multica 全套 tab、归档页可用 composer | Phase 5（删减与品牌） |
| L10 | sidecar 状态机阶段顺序未翻转（deviation #34 仅前端 fixture） | **Phase 2 前置 RFC（阻塞聚合端点形状）** |

## 6. 环境备忘

- 验收环境：sidecar `:39876`（AUTOLAB_HOME 默认）+ `frontend-next` Next dev `:3000`（`pnpm dev` 含 warm-dev 路由预热）
- 截图证据：`gui-test-screenshots/`（01–13）
- 验收涉及的代码改动（P1×2）尚未提交，与 sidecar 未提交改动（deviation #35/#36 后续）同在工作区

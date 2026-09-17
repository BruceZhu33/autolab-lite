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

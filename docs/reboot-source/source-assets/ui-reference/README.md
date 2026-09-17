# Multica Shell and UI reference · license-gated

本目录用于来源留档和许可评估，不是编码 AI 的默认迁移输入。M5 必须先完成 `07-Multica壳迁移清单.md` 与 `09-v1.3审查修订与决策门.md` 规定的许可决策。

- `multica-shell-source-reference.tar.gz`：排除了依赖、构建缓存、营销图片、本地环境文件、测试文件和旧 fixture 数据的前端源码参考快照，包含 LICENSE、NOTICE 和 provenance。
- 三张 PNG 的用途有限：
  - `10-gpu-servers.png` 只有加载态，不能作为服务器卡片完成态设计依据；
  - `15-issue-detail-real.png` 包含评论、Properties、Pull Request 等新产品非目标，只观察布局密度；
  - `19-phase4-proposal.png` 包含 Agent 对话和自动迭代等非目标，只观察主从布局和指标摘要。

只有在已授权迁移路径下，新工作区才可以从快照中提取 design tokens、UI primitives、Sidebar/PageHeader、Board/Card 和 GPU 页面，并且仍必须改用 AutoLab Lite 的轻量 ViewModel。独立实现路径不得解压或阅读源码快照，应根据 AutoLab 自有需求和自主设计稿实现。

不要恢复 Multica 的 Workspace/Member/Agent 领域模型、IssueSurface 查询规划、fixture fetch、全局 Providers 或 realtime 客户端。

`source-assets/brand/` 中的图标同样来自 Multica，只作来源留档，不是默认 AutoLab 品牌资产。

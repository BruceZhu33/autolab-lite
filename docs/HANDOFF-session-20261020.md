# 交接文档 — 2026-10-20(会话:autolab CDS 改版 + PaperPulse 论文站 + 集成交接)

> 给下一个会话/接手人:本文覆盖三个项目的架构、服务器形态、部署手册、凭据位置、
> 已知坑与挂账事项。写本文时三仓库全部 clean 且与远端同步,服务全部在线。

## 0. 一页速览

| 项目 | 本地路径 | 位置 | 访问 |
|---|---|---|---|
| autolab-lite(实验台) | `/Users/juan/Documents/autolab/autolab-lite` | gpu-server `~/autolab-lite` | `http://192.0.2.46:3430` |
| PaperPulse(论文站) | `/Users/juan/Documents/AIHOT/paperpulse` | gpu-server `~/paperpulse` | `http://192.0.2.46:3310` |
| autolab-kb(LABNOTE) | `/Users/juan/Documents/autolab/autolab-kb` | gpu-server `~/labnote`(文件同步,非 git) | `https://192.0.2.46` |

- 三仓 git:origin=GitHub(BruceZhu33/*);autolab-lite 与 paperpulse 另有 `server` 远端(gpu-server,updateInstead 即推即部署);autolab-kb 仅 GitHub,服务器靠 rsync(已验证零差异)
- gpu-server = 192.0.2.46,共享 GPU 机(用户 serveruser);三服务局域网 IP 直达,无需 SSH 隧道(隧道仍可用:`ssh -N -L 3430:localhost:3430 -L 3310:localhost:3310 gpu-server`)
- 浏览器缓存坑:改版后看不到变化→强刷 Cmd+Shift+R 或换 `http://127.0.0.1:3430`(与 localhost 缓存隔离)

## 1. 服务器服务形态(gpu-server)

### systemd 用户单元(linger 已开,开机自启)
```bash
systemctl --user status|restart autolab-control   # FastAPI, 8710(已绑 0.0.0.0)
systemctl --user status|restart autolab-web       # Next.js, 3430
journalctl --user -u autolab-control -f           # 日志
```
- 单元:`~/.config/systemd/user/`,drop-in:`labnote.env`(LABNOTE_URL/PAT)、`lan.conf`(AUTOLAB_HTTP_HOST=0.0.0.0 / AUTOLAB_API_ALLOWED_LAN=192.0.2.46)
- **数据已迁出 /tmp**:`~/autolab-data/control.db` + `credentials.json`(owner/agent token 在 credentials 的 `control:api` 条目;owner token 即 `owner-edc…`,不要写进文档/代码)
- control 的 uvicorn host 由 `AUTOLAB_HTTP_HOST` 控制(默认 127.0.0.1);CORS 放行本机+LAN IP

### PaperPulse(docker compose,五容器,restart: unless-stopped 自愈)
```bash
cd ~/paperpulse && docker compose ps|logs -f|up -d --build
```
- web 0.0.0.0:3310;**API 仅回环 127.0.0.1:3311**(compose ports 里写死,供 autolab control 代理论文数据)
- `.env`:GLM key(Anthropic 协议端点 open.bigmodel.cn/api/anthropic)、分步模型(glm-5.3 默认/flash 预筛/flash-selection 评分)、PORT=3310、ADMIN_PASSWORD
- SQL 快捷:`~/pp-dbq.sh "select …"`(docker compose exec db psql)

### LABNOTE(注意:root 进程)
- `backend/app.py` 由 root 跑(Caddy 443 TLS + 后端 127.0.0.1:8000);无 systemd(挂账见 §6)
- 升级方式:本地 autolab-kb 改完 rsync 到 `~/labnote`(排除 data/inbox/backups/volumes/.venv),重启 root 进程需 root 权限

### 备份(每日自动,保留 14 天)
- `~/bin/autolab-backups.sh`(cron 3:30):control SQLite 快照 → `~/backups/autolab/`;**PaperPulse 整库 pg_dump → `~/backups/paperpulse/`**(分析结果是付费产出,库丢了要重花钱)
- labnote 自有 cron 3:00(backup.sh)

## 2. autolab-lite 要点

- 分支 `main`(2026-10-21 起统一,原 refactor/stage-agents 已并入)。部署:`scripts/deploy-to-server.sh`(push origin+server)→ 服务器 `cd apps/web && NEXT_PUBLIC_AUTOLAB_API=http://192.0.2.46:8710 npm run build && systemctl --user restart autolab-web`;control 改动需 `systemctl --user restart autolab-control`
- **web 必须带 NEXT_PUBLIC_AUTOLAB_API 构建**(IP 口径,localhost/IP 两种访问都通)
- UI:CDS(Claude 风格)暖纸亮色默认/墨黑暗色,令牌单一事实源 `apps/web/app/tokens.css`
- 侧栏 IA:Work 组=实验看板/项目/Baseline/研究/实验运行日志;全局组=论文雷达/知识对话/空间与文档/GPU 服务器
- **统一登录**:`/api/auth/login`(用户名密码→LABNOTE 校验;labnote admin→owner token,普通成员自动开只读 member 账号,幂等);`/api/whoami` 查身份;accounts 表(schema v21);Settings 页=账户与访问(登录/成员表/停用)+服务连接(令牌高级入口)+模型+外观
  - 两层会话:控制台 token(`autolab.api_token`)+ 知识库 JWT(`autolab.kb_token`)。令牌直登没有后者→KB/空间页进不去,设置页有琥珀提示引导重新账号登录
- **论文雷达页** `/papers`:单页滚动=专栏卡→热点条→时间线(按天分组)→信源河动画(移植自论文站,579 行 canvas,取色映射 CDS 令牌)→信源目录→提议补充入口。数据经 control 代理 `/api/papers/*`(columns/columns/{slug}/timeline/stats/sources)
- **Agent 工具 search_papers**(`agent/paper_radar.py` + `tools.paper_radar_tool()`):研究循环查最新论文,v1 API,7 天窗,baseline/research 两阶段已接线;论文站不可达自动不注册
- e2e 依赖:board 页需含「AutoLab」「实验看板」文案(别删);`tests/e2e_local/test_m5_web.py` 需本地 demo(杀 8710 端口冲突)
- **git 已根治**:tsbuildinfo/.next 移出跟踪(否则服务器构建弄脏工作区→updateInstead 推送被拒,症状是 push 报 "and the repository exists")

## 3. PaperPulse 要点

- AIHOT 框架 fork(Node/TS monorepo);**行业差异全在 `industry/`**:site/taxonomy/sources/topics/columns/prompts
- 信源 20 个:arXiv 7 分类(RSS+API 双源)×2、HF(大陆被墙,degraded 退避中)、5 核心期刊(MedIA/TMI/TPAMI/CCE/AIChE,Crossref API 按 ISSN,间隔错开 360-460min 防限流)
- 管道:预筛→评分×2(flash-selection)→中文写作→归组(SAME_WORK 同一工作聚类)→专栏(收录线 **35**)/全站精选(60);期刊论文常无摘要=标题分诊(评分提示词有专门规则)
- **专栏体系**:旗舰 3 个(医学图像分割/化工计算机交叉/分割与检测=聚合专栏,标签并集)+图像分割等;`columns.ts` FLAGSHIP_COLUMNS 是导航/首页卡来源;加专栏=topics.json+taxonomy 词表+columns.ts
- **深读(PDF 第二遍)**:过线(selected 或 ≥40)且 arXiv 源→自动下载 PDF(unpdf,≥3.5s 间隔并发 1)→glm-5.3 提炼方法/设置/结果/消融/局限/可借鉴→`deep_reads` 表→详情 API `deepRead` 字段→论文站详情页「深读·全文提炼」区块;回填脚本 `scripts/enqueue-deepread.ts`;**已完成 275 篇,存量回填进行中**
- Anthropic 协议支持:`LLM_PROTOCOL/ZHIPU_PROTOCOL=anthropic`(llm.ts,thinking 块 +4000 token 补偿);GLM key 是 Coding Plan(只有 /api/anthropic 端点,/api/paas/v4 报 1113)
- 公开接口:`/api/site/sources`(信源目录)、`/api/v1/items`(搜索,search_papers 用)
- 后台 `/admin`(密码在 .env):信源管理/试抓/内容诊断(每篇完整判断链)/模型切换/花费统计

## 4. 关键坑记录(踩过的,别再踩)

1. **服务器 psql 查询**:ssh 嵌套引号必炸→用 `scp` 传 .sql 文件 + `docker compose exec -T db psql -Atf/-At <` 或 `~/pp-dbq.sh`
2. **后台进程**:ssh 里起常驻进程用 `setsid nohup … < /dev/null &`,且该 ssh 会话会挂住(结果拿到后 TaskStop;autolab 现已 systemd 化不涉及)
3. **control 进程匹配**:`pgrep -f autolab_control` 会匹配到 ssh 自身的 bash 包装→用 `ps -eo pid,cmd | grep … | grep -v grep | awk` 或直接 systemctl
4. **部署后"没变化"**:①服务器忘了 `npm run build` ②浏览器缓存(强刷/127.0.0.1)③worktree 脏导致 push 被拒(已根治,若复发查 `git status`)
5. **arXiv**:周末(六/日)RSS/API 均无新论文(skipDays),别当故障排查;announce 恢复=北京周一约 08:00
6. **Crossref**:并发 5 个同相位请求会 429→间隔已错开;429 自动退避恢复
7. **postgres.js**:动态列拼 SQL 用 `sql.json(obj as never)` 存 jsonb;JSON.stringify+::jsonb 会双重编码(深读踩过,存量已修)
8. **aihot-web 端口**:论文站 API 回环 3311 是给 control 的,勿改公开

## 5. 凭据与安全

- owner/agent token:`~/autolab-data/credentials.json`(control:api);GLM key:同文件 model:api-key + paperpulse `.env`;LABNOTE PAT:`~/labnote/.env` 的 PAT_AUTOLAB_TOKEN
- 暴露面:3430/8710/3310/443 局域网可达(owner token / 后台密码 / labnote 各自鉴权);要收紧=删 lan.conf+还原 NEXT_PUBLIC 构建
- member 账号登录时自动开通;吊销在 Settings→成员访问;**曾有一次账号路由漏鉴权的事故已修**(教训:新路由必须带 dependencies=[ctx.owner/reader],上线跑权限矩阵 401/403/200)

## 6. 挂账/待办(按优先级)

1. **35 分线校准**(约周三):用户带实例(漏报/误报论文标题)→ 调 `industry/selection.ts` 门槛或评分提示词权重;实例在后台 /admin/content 查判断链
2. **深读质量抽查**:明天看方向论文的深读(方法对不对/数字准不准),不对→调 `industry/prompts/deep-read.md`
3. **HF 源**:被墙,要么配 EGRESS_PROXY_URL 要么放弃(社区精选信号缺失,arXiv 可兜)
4. **聚类"多篇合并"路径未被真实触发**(HF 缺失+arXiv 双源入库层判重);等期刊+arXiv 同工作双版本出现自然验证
5. **LABNOTE root 进程无 systemd**(重启需 root);可协调管理员做,或保持现状(有备份+自启不做但常驻)
6. PaperPulse 热度条按新闻口径(多来源才算热),论文场景可能长期空;若要"今日最值得看"可改为评分×新鲜度(用户未拍板)
7. 论文站域名/HTTPS(现为 IP:3310;对外可加 Caddy,已有 https profile)

## 7. 新会话快速上手

1. 读本文档 + `autolab-lite/docs/HANDOFF-iteration.md`(更早的业务交接)
2. 三仓在 Mac 本地,全部 clean 同步;服务器服务全在线(IP 直达见 §0)
3. 部署命令逐条在 §1/§2;改 industry/ 提示词后 paperpulse 需 `docker compose up -d --build`
4. 用户当前关注点:专栏质量校准、深读质量、课题组同学使用反馈

## 8. 仓库谱系(2026-10-21 补记)

- `archive/v0-freehands` 分支:v0 时代(FREEHANDS 工作区,2026-09-13~16,44 个提交,
  邮箱已统一重写为本账号邮箱,原始日期保留)
- main 经 ours 合并挂入该历史(文件树零变更)
- 2026-09-19~10-05 为真机联调期,HANDOFF-iteration.md 有该期工作记录,无提交(产物并入 10 月提交)

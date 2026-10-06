# P5–P9 真机验收记录

- 日期:2026-09-24(owner 参与协商,全程真机:GLM glm-5.3 / A6000 SSH
  <A6000 服务器> / medseg-demo 仿真项目)
- 范围:AGENT-LAYER-DESIGN P5(可观测/介入/断点)→ P6(submit_result/
  留痕读回)→ P7(策略 B 期)→ P8(策略 C 期)→ P9(Chat 整合),
  外加 Plan 页 UI 重做
- 结论:**九项全过**;过程发现并修复 4 个真实缺陷;遗留 6 项(均非阻塞)

---

## 一、验收结果

| # | 场景 | 结果 | 关键证据 |
|---|---|---|---|
| 1 | 实时会话观看 | ✅ | LiveSessionCard 自动跟随,2s 级时间线;Agent 探索路径完整可见(列目录→搜 config→读入口→探磁盘) |
| 2 | 注入引导 | ✅ | inject 事件落库 + inbox 消费(DB 双证),消息真实进入模型上下文 |
| 3 | 停止会话 | ✅ | 循环「已停止:人工在实现会话中停止」,**不回落单发**;会话留痕(工具 6 次后收尾) |
| 4 | submit_result | ✅ | 三个实现会话(15/9/6 工具调用)全部 submit→受理→收尾;无 ```json``` 抽取依赖 |
| 5 | 策略 Directive | ✅ | 产出→落库→注入→第 2 轮 Agent 口中「按指令」+行为逐条对应(approach 的冒烟确认与本地 fallback 分支、matrix 2×2、avoid 遵守);variants 4 条入实验积压 |
| 6 | 包络暂停 | ✅ | 矩阵 cap(4>2)两轮各触发一次 awaiting,放行后**批准同一草案**(不重烧计划 tokens);「一次性」语义兑现;drift 机判触发(新建验证脚本落点无交集)→ 决策卡「落点漂移信号」徽标 |
| 7 | Chat 整合 | ✅ | chatagent 自治探索(项目名→list_projects→ID→会话时间线);回答含状态表/积压逐条/会话复盘/瓶颈诊断;**confirm 门**:start 以 confirm=false 试探→念参数→等 owner 确认,不偷跑 |
| 8 | 执行段 | ✅ | runner 部署(7810)→ 4 Job 真机执行 → 指标回读 → 对比 → 结论;含一次完整自愈(见二.3) |
| 9 | 断点续跑 | ✅ | 会话级:implement 中 kill -9 → 重启 → resumed 事件 + 断点上下文续走至 submit;循环级:断点相位自动续推,最终第 3 轮 val_dice=0.837 超基线 completed 收官 |

## 二、过程发现并修复(真实缺陷)

1. **GLM 端点 IPv6 半通**(很可能即「GLM 端点波动」病史成分):域名双栈,
   本机 IPv6 到阿里云 GA 间歇 SSL 中断;httpx 命中 IPv6 即挂。修复:
   `net_preferences.py` 进程级强制 httpx 绑 IPv4 + 连接重试;
   `chat_multi` 网络异常纳入 4 次重试循环。
2. **LiveSessionCard 两处 UI 缺陷**:会话结束后收尾事件不刷新;无法回看
   历史会话(注入那条时间线不可见)。修复:结束补拉 + 会话 chip 选择器。
3. **implement↔plan 一致性缺口(现场自愈,未代码修复)**:计划模板生成
   `--aug` 参数而 train.py CLI 无此参数(argparse exit 2)。策略层基于
   「exit 2+输出空+本地全通过」正确判「执行层故障而非假设被证伪」;第 2 轮
   implementer 自行复现定位真因,提交「train.py 加 --aug + 顶层异常兜底
   (traceback 落盘)」→ 第 2 轮 4 Job 全 SUCCEEDED。根因(planner 无工具
   核验命令模板)列入遗留。
4. **崩溃重入轮数漂移**:implement 相位 crash 后重入把 round_no 重复 +1
   (collab 2 轮上限被加到 3)。修复:有可续跑会话=同轮续跑不递增
   (`_phase_implement` + 单测 `test_implement_reentry_resumes_same_round`)。
5. **MCP mode 枚举未显式**:chatagent 把 intermediate 叫成 medium。
   修复:docstring 显式列 `collab | intermediate | heavy`。

另有两处**机制自证**(未修,设计如此):analyzer 无 key 时会话层失败→
规则法回落成功;实现会话网络死→回落单发→两层全挂循环才终止并留痕。

## 三、遗留(非阻塞,按价值排序)

1. **runner Job stderr 未落盘**(输出目录空,失败只剩 exit code)——
   本轮靠 Agent 自加兜底掩盖;应在 runner 侧修。
2. **plan 起草缺工具核验**:draft_iteration_plan 单发,不核验命令模板与
   代码 CLI 一致(reviewer 有此纪律,planner 无)。
3. resume 路径 tokens 计量疑点:本轮断点续跑循环 tokens_used 显示 0。
4. MCP `start_iteration_loop` 未暴露 envelope 参数;UI 启动卡无 envelope 输入。
5. Chat 页有一个等待「确认」的会话(测试 confirm 门遗留,忽略或回「算了」)。
6. `test_m5_web` e2e 固定端口 8710,与常驻 demo 冲突(环境问题,非代码)。

## 四、环境终态

- demo 三进程在跑(web 3430 / control 8710,数据目录
  `/tmp/autolab-web-m7vSPJ`,owner token `<demo 打印,已失效>`);
  GLM key 已装;SSH runner 部署在 7810(共享机,标准位)。
- medseg-demo 已完成一轮成功迭代并归档(val_dice 0.837),实验积压 4 条
  变体待真实数据。
- 测试:271 passed(唯一 failed 为遗留 6 的端口冲突)。

---

## 五、追加:MedCLIPSeg 真项目全流程(2026-09-24 晚)

环境:现成工作区(软链 zwm 权重/数据)+ conda env zwm + 基线口径
100%×13ep(**val_dice 0.831**,量级与当年正式基线 0.9016 一致)。

### 过程:历史欠账连环爆发与清理
9-23 会话的增强改动(未冒烟提交,smoke_runs=0)潜伏两天连环爆发:
目录名 images/masks 错、F.to_tensor 用错模块、text_prompt 传整条 dict、
random_rotate 类型假设错——手修 2 处 + debugger 会话修 2 处后仍
Val Dice=1.9(超上界)/loss=-3.8e7,数据语义已漂移 → **整体回滚至
patch orig 备份**,教训入 failures.md("只看 exit code 不够,必须验证
指标合理性")。回滚后基线一次跑通。

### 真实迭代自愈闭环(collab 2 轮 + envelope{cap2, gpu 1.5h})
- r1:implement(41 工具)加 --hflip 时丢 return → 2 Job 崩(collate
  NoneType)→ **Directive 精准破案**(引用 Job id + 根因定位);
- r2:implement 补 return + **建 _verify_hflip.py 最小复现脚本验证**
  (严格执行 direction 中 EMA 教训纪律)→ 2 Job SUCCEEDED;
- **真实研究结论**:10% 数据下 hflip=0 与 hflip=0.5 同分(0.6633,
  Δ=-0.168 vs 全量基线),终局分析判定「轻度 hflip 增强有益假设在
  10% 小矩阵上不成立,可信证据来自第 2 轮」——基于证据否定假设而非
  机械重试;循环按 2 轮上限正确终止。
- envelope 两次触发(GPU 估算口径:spec timeout 7200s×2Job=4h>1.5h,
  实际 13ep 仅 3 分钟——**估算口径显著偏保守**,列入观察)。

### 遗留观察
- hflip=0/0.5 两组 val_dice 四位小数完全一致,疑似增强路径实际未生效
  (参数未传入/分支未触发)——下一轮迭代的真实观察点;
- GPU 包络估算按 spec 超时上限,与实际时长偏差大,可改为按历史同类
  Job 实测时长估算;
- Directive(r1)对「修复者」的时间线归因略有偏差(把 r1 会话读作
  修复者),方向与 avoid 判断不受影响。

### 追记:hflip 同分疑点已修复(2026-09-24 深夜)
根因是两处叠加的隐蔽接线断路:train.py 的 opts 用 argparse REMAINDER,
`TRAIN.NUM_EPOCHS` 位置 token 触发后把尾随的 `--hflip 0.5` 一并吞进
opts,随后 51-62 行的清洗逻辑把 opts 里 `--` 开头的 token 剥掉丢弃
→ args.hflip 永远是默认 0(中途在 train.py 补的 `cfg.hflip = args.hflip`
接线行因此无效——位置对,但 args 本身没收到)。修复:适配脚本把
extra 具名参数拼到 TRAIN.NUM_EPOCHS 之前(让 argparse 正常解析),
cfg.hflip 接线行保留作双保险;备份 autolab_baseline.py.pre_hfix /
train.py.pre_hfix。
验证(10%×13ep,seed 1):对照组 0.6633 vs 增强组 **0.6871(Δ+0.024)**,
轨迹分叉,增强真实生效——**上轮循环「hflip 增益不成立」的结论被推翻**,
真实结论是轻度 hflip 在 10% 数据上有益(与评审报告假设一致)。
方法论注记:Agent 的 r2 验证(_verify_hflip.py)只验了张量合法性,
没验「参数变化是否改变训练轨迹」——冒烟验证的盲区,值得写进
failures.md 的验证纪律(后续迭代顺手做)。

---

## 六、追记:D-023/D-024 真机复验(2026-09-25)

环境:SSH runner 重装(新代码,含事件 log_tail/归档)+ control 升 D-024。
MedCLIPSeg 复验循环(collab 2 轮 + envelope{cap2, gpu 1.5h}),第三跑
成(前两跑暴露并修复 planner 会话产出的两个形状缺口:缺
datasets_and_metrics 字段、metrics 给成 dict——修法为 planner 输出
统一走 parse_plan_draft 归一化,与单发同构)。

### 护栏实战结果(全中)
- **包络实测估算**:两轮 4 个真 GPU Job 全程零误挂(旧口径会两次
  高估 4h>1.5h 挂起)——D-024② 直接见效。
- **门③ 参数敏感性**:对照组 0.6818 vs 增强组 0.6292,分数分叉,
  insensitive=False(与昨日逐位同分形成鲜明对照)——增强真实生效。
- **planner 会话 + 契约核验**:计划模板的 --hflip/--hflip-prob 均为
  入口真实定义的参数(核验过),计划↔代码自洽。
- **Directive 研究质量**:r1 见单次 Δ=-0.1492 后提出的 goal 是
  「同命令重复运行估计 run-to-run 噪声」;r2 完全复现 → 终局分析
  正确判定「确定性下降,疑似钩子与管线不兼容,非随机波动」——
  这是教科书级的对照实验推理。

### 新发现(下一迭代候选)
1. **implement 越权重做**:direction 明示「代码已就绪无需再改」,
   implement 会话仍新建 sitecustomize.py 零侵入钩子方案并改造 adapter
   (--hflip 变布尔开关 + --hflip-prob),该钩子有害(0.6818→0.6292),
   且导致工作区存在**两套 hflip**(钩子版 vs 此前修复的原生
   train.py 链路 +0.024,本轮未被跑到)。方向指令对 implementer 的
   约束力弱 + 循环间工作区状态漂移——值得做「改动冻结/已就绪声明
   的硬约束」机制。
2. 循环终止语义:collab 轮数耗尽记 failed(沿用旧语义),研究上
   其实是「完成且结论明确」,可考虑区分。

### 追记:确认轮与 adapter 顺序二进宫(2026-09-25 深夜)
决策 A 执行:sitecustomize 钩子废弃归档(→ .autolab/patches/
20260925-discarded-sitecustomize/),恢复原生链路,教训入 failures.md。
确认轮中门③再次抓住逐位同分(0.6818/0.6818)——根因:**恢复 .pre_hfix
备份时把 epochs 拼接顺序带回旧版,hflip 转发被排到 REMAINDER 触发词
TRAIN.NUM_EPOCHS 之后**,被 train.py 清洗剥掉。同一根因第二次咬人
(第一次是 9-24 的 --aug)。修正顺序后:
- 手动验证:recipe 打出 hflip=0.5,0.6732 vs 0.6818 分叉;
- 产品链路确认轮(EXP-0026):对照 0.6818 / 增强 0.6732,
  paramInsensitivity=False,结论 inconclusive 已记入实验文件。
**科学结论**:hflip@10% 效应量 ±0.02 且随 dataloader 实现版本翻转
(三轮:钩子 -0.05 / 旧原生 +0.024 / 新原生 -0.009),单 seed 不足
定论;对照组 run-to-run 逐位复现(管线确定性良好)。
**产品启示**:REMAINDER 参数顺序是复发型坑——建议列入下迭代:
计划模板审计参数顺序,或 train.py 清洗把已知具名参数转赋值而非丢弃;
另确认了门③对该类事故的可靠拦截(两次实战两次命中)。

---

## 七、追记:code_ready 首战——多种子定案(2026-09-25)

code_ready 循环(D-027)首次实战:MedCLIPSeg 多种子 hflip 对照,
全程零代码改动。

### 机制实战
- implement 相位秒跳(启动即 plan);循环全程 tokens 仅 1959
  (策略会话;对比常规循环每轮 implement 4-5 万 tokens——冻结模式
  的经济性直观可见)。
- 策略层自适应扩阵:r1 planner 小矩阵探路(seed1 两格)→ r1 Directive
  明确「补齐 seed 2/3 完成 6-Job 矩阵,统计组内 Δ」→ r2 全矩阵。
- 包络实测估算零误挂(8 Job 全程无 awaiting)。

### 研究结论(hflip@10% 悬案定案)
| seed | 对照 | hflip=0.5 | Δ |
|---|---|---|---|
| 1 | 0.6709 | 0.6787 | +0.008 |
| 2 | 0.6942 | 0.6913 | -0.003 |
| 3 | 0.6857 | 0.6761 | -0.010 |

组均 Δ≈-0.002,**效应为中性(seed 间方差 ±0.012 远大于组间差)**;
此前的 +0.024 / -0.009 / -0.05 均为单 seed 噪声+实现差异的假象。
对照组跨轮逐位复现(0.6709)再次确认管线确定性。终局分析给出
机制假设(医学影像方位语义:器官位置有解剖学先验,hflip 破坏位置
一致性)与方向切换建议(弃几何翻转,改测 color jitter 等强度类增强)
——研究级判断。
### 观察点
r1 与 r2 的同参数增强组结果差 0.018(0.6611 vs 0.6787)——翻转路径
存在非确定性来源(对照组则逐位稳定),值得后续排查;不影响
"平均中性"结论,反而强化多种子必要性。

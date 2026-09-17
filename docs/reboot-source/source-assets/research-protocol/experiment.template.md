---
schema_version: 1
id: EXP-0001
title: EXPERIMENT_TITLE
created_at: YYYY-MM-DDTHH:MM:SSZ
parent_experiment_id: null
plan_revision_id: null
conclusion: pending
next_decision: null
---

# Hypothesis

写出可以被本轮证据支持或否定的假设。

# Change from previous

说明相对上一轮只改变了什么，以及保持不变的部分。

# Baseline

写出比较对象并引用其实验、Job、Artifact 或指标。

# Datasets and metrics

列出数据集、split、指标、方向和聚合方式。

# Success criteria

在执行前写出可判定的成功条件；批准后不得为迎合结果修改本轮条件。

# Risk and fallback

说明主要风险、失败时先检查什么，以及回退方式。

# Evidence

结果产生后追加稳定引用，例如：

- job:
- artifact:
- metric:

# Analysis

区分直接观察、推断和仍不确定的部分。

# Conclusion

结果产生后把 front matter 的 `conclusion` 更新为 `supported`、`rejected` 或 `inconclusive`，并说明理由。

# Reconsideration condition

若结论为 `rejected` 或决定 `stop`，必须说明未来出现什么新证据、数据或资源时值得重新考虑。

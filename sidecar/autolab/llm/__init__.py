"""LLM 子系统：薄客户端（TD7 httpx，OpenAI 兼容）+ 提案引擎（M2-2）。

- 多供应商按使用点配置（D11：round_analysis / self_repair / review / report）
- 密钥存凭据存储（D30），库内只有引用
- RD-Agent 适配器位预留（deviation #19）：ProposalEngine 为统一接口，
  LLMProposalEngine 为内置实现；RD-Agent 版本锁定接入时新增实现类即可
"""

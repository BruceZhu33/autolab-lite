"""独立 LLM 审阅(Phase C2):GPU 花费前后各一道闸门。

- pre-flight critic:`run_experiment` 提交前审假设/实验设计/公平性,可要求 revise/reject;
- post-round reviewer:`end_round` 收束前审证据是否可信/是否作弊,可疑可拒收束。

两者都 fail-open:审阅器故障(无 key/端点失败/解析失败/输出非法)一律放行
(critic=approve、reviewer=valid)并带 unavailable 落进事件,绝不因审阅器
故障阻塞研究循环。不 import stages(分层守卫);输入由调用方组装。
"""

from __future__ import annotations

import json

from .model import call_model

_CRITIC_VERDICTS = ("approve", "revise", "reject")
_REVIEW_VERDICTS = ("valid", "suspect")
_SEVERITIES = ("low", "medium", "high")

_CRITIC_SYSTEM = """你是独立的研究实验审阅者,只审不执行。一批实验即将提交 GPU,判断它能否产出可信结论。逐项检查:
1. 训练口径是否与基线一致:epochs/数据比例/batch/seed 等只应按研究目的变化;
2. 是否改了评测代码/数据划分/指标定义(有 eval 契约时尤其注意);
3. dataset 标注与命令实际跑的数据口径是否一致;
4. 是否缺少 seed/变体/对照,导致结论不可信;
5. 是否与历史失败 case 原样重复。
只输出严格 JSON,不要任何多余文字:
{"verdict": "approve|revise|reject", "reasons": ["最多5条"], "suggestions": ["最多5条"]}
verdict 语义:approve=可跑;revise=可改后重交;reject=该方向/实验不应跑。"""

_REVIEWER_SYSTEM = """你是独立的研究证据审阅者,只审不执行。一轮研究要收束,审其结论是否可信、有无作弊。平台评测是权威:训练命令自报的 metrics 与 eval Job 指标不同不是作弊。检查:
- eval Job 是否存在且指标来自 eval artifact;
- protected code/data hash 是否一致;
- 是否有改动评测/数据划分、重复提交、数据集错标;
- 结论是否超出证据(例如无显著 CI 却说有提升)。
只输出严格 JSON,不要任何多余文字:
{"verdict": "valid|suspect", "severity": "low|medium|high", "reasons": ["最多5条"]}
severity 语义:high=证据不可信;medium=可疑需补充;low=轻微瑕疵仅记录。"""


def _clip(items, limit: int = 5) -> list[str]:
    return [str(i) for i in (items or []) if str(i).strip()][:limit]


def _ask_json(credentials, system: str, user: str, *, max_tokens: int = 800) -> dict:
    """一次无工具 call_model;从 text 块提取 JSON;失败返回 {"error":..., "unavailable": True}。

    去掉 ```json fence,取第一个 `{` 到最后一个 `}` 再 json.loads;解析结果附带
    响应 usage(供事件留痕)。所有异常都吞掉——fail-open 是硬要求。
    """
    try:
        response = call_model(credentials, system=system, max_tokens=max_tokens,
                              messages=[{"role": "user",
                                         "content": [{"type": "text", "text": user}]}],
                              tools=[])
        text = "".join(str(b.get("text") or "") for b in response.get("content") or []
                       if isinstance(b, dict) and b.get("type") == "text")
        text = text.strip()
        if text.startswith("```"):       # ```json … ``` fence
            text = text.strip("`")
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"回复中没有 JSON 对象:{text[:120]!r}")
        payload = json.loads(text[start:end + 1])
        if not isinstance(payload, dict):
            raise ValueError(f"顶层不是 JSON 对象:{type(payload).__name__}")
        payload["usage"] = response.get("usage") or {}
        return payload
    except Exception as exc:  # noqa: BLE001 - 审阅器故障必须 fail-open
        return {"error": str(exc)[:300], "unavailable": True}


def _norm(value) -> str:
    """verdict/severity 归一化:模型输出 "REJECT"、"Suspect" 等大小写/空白差异不挡判定。"""
    return str(value).strip().lower() if value is not None else ""


def _fail_open(payload: dict, *, verdict: str, note: str,
               model_reasons: list[str] | None = None) -> dict:
    # model_reasons:非法 verdict/severity 时保留模型原始 reasons,事件可追溯。
    reasons = [f"{note}:{payload.get('error') or 'unavailable'}", *(model_reasons or [])]
    out = {"verdict": verdict, "reasons": reasons, "unavailable": True}
    if payload.get("error"):
        out["error"] = payload["error"]
    return out


def critique_experiment(credentials, *, purpose: str, jobs: list[dict],
                        metrics: list[dict], contract: dict | None,
                        pool: dict, cases: list[dict]) -> dict:
    """返回 {"verdict": "approve|revise|reject", "reasons": [...],
             "suggestions": [...], "usage": {...}};基础设施失败 → approve+unavailable。"""
    eval_cfg = (contract or {}).get("eval") or {}
    user = json.dumps({
        "purpose": purpose,
        "jobs": [{"name": j.get("name"), "command": j.get("command"),
                  "dataset": j.get("dataset"), "variant": j.get("variant"),
                  "seed": j.get("seed")} for j in (jobs or [])],
        "metrics": metrics,
        "baselinePool": {name: {"metrics": info.get("metrics"),
                                "command": info.get("command")}
                         for name, info in (pool or {}).items()},
        "contract": {"primary": (contract or {}).get("primary"),
                     "direction": (contract or {}).get("direction"),
                     "guardrails": (contract or {}).get("guardrails"),
                     "evalCommand": eval_cfg.get("command"),
                     "metricFile": eval_cfg.get("metric_file")},
        "recentCases": [{"dataset": c.get("dataset"), "variant": c.get("variant"),
                         "hypothesis": c.get("hypothesis"), "analysis": c.get("analysis"),
                         "verdict": c.get("verdict"),
                         "failureClass": c.get("failureClass"),
                         "meanGain": c.get("meanGain")}
                        for c in (cases or [])[:5]],
    }, ensure_ascii=False)
    payload = _ask_json(credentials, _CRITIC_SYSTEM, user)
    if payload.get("unavailable"):
        return {**_fail_open(payload, verdict="approve", note="critic 不可用,放行"),
                "suggestions": [], "usage": {}}
    raw_verdict = payload.get("verdict")
    verdict = _norm(raw_verdict)
    if verdict not in _CRITIC_VERDICTS:
        return {**_fail_open(payload, verdict="approve",
                             note=f"critic 输出非法 verdict:{raw_verdict!r}",
                             model_reasons=_clip(payload.get("reasons"))),
                "suggestions": [], "usage": {}}
    return {"verdict": verdict, "reasons": _clip(payload.get("reasons")),
            "suggestions": _clip(payload.get("suggestions")),
            "usage": payload.get("usage") or {}}


def review_round(credentials, *, report: dict, evaluation: dict,
                 evidence: dict, contract: dict | None) -> dict:
    """返回 {"verdict": "valid|suspect", "severity": "low|medium|high",
             "reasons": [...], "usage": {...}};失败 → valid+unavailable。"""
    evd = evidence or {}
    code = evd.get("code") or {}
    user = json.dumps({
        "report": {k: report.get(k) for k in
                   ("hypothesis", "changes", "analysis", "nextIdea", "kept", "verdict")},
        "evaluation": {"rows": evaluation.get("rows"), "means": evaluation.get("means"),
                       "guardrails": evaluation.get("guardrails"),
                       "evalMissing": evaluation.get("evalMissing"),
                       "warnings": evaluation.get("warnings")},
        "evidence": {"protected": evd.get("protected"),
                     "evalJobs": [{"id": e.get("id"), "trainJobId": e.get("trainJobId"),
                                   "status": e.get("status"), "metrics": e.get("metrics")}
                                  for e in evd.get("evalJobs") or []],
                     "evidenceHash": evd.get("evidenceHash"),
                     "codeDirty": code.get("dirty"), "gitHead": code.get("gitHead")},
        "contract": contract,
    }, ensure_ascii=False)
    payload = _ask_json(credentials, _REVIEWER_SYSTEM, user)
    if payload.get("unavailable"):
        return {**_fail_open(payload, verdict="valid", note="reviewer 不可用,放行"),
                "severity": "low", "usage": {}}
    raw_verdict, raw_severity = payload.get("verdict"), payload.get("severity")
    verdict, severity = _norm(raw_verdict), _norm(raw_severity)
    if verdict not in _REVIEW_VERDICTS or \
            (verdict == "suspect" and severity not in _SEVERITIES):
        return {**_fail_open(payload, verdict="valid",
                             note=f"reviewer 输出非法 verdict/severity:"
                                  f"{raw_verdict!r}/{raw_severity!r}",
                             model_reasons=_clip(payload.get("reasons"))),
                "severity": "low", "usage": {}}
    return {"verdict": verdict, "severity": severity or "low",
            "reasons": _clip(payload.get("reasons")), "usage": payload.get("usage") or {}}

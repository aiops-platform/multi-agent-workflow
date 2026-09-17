"""Agent 输出 Schema（design §7 输出契约）。

JSON Schema 形态，供 prompt 注入 + 输出校验双重使用。
诊断侧（BugReport / LogEvidence / RootCause）是 S-011 实测通过的字段。
"""
from __future__ import annotations

BugReportSchema = {
    "type": "object",
    "properties": {
        "symptom_type": {"enum": ["hang", "crash", "slow", "degraded"]},
        "severity": {"enum": ["high", "medium", "low"]},
        "summary": {"type": "string"},
        "correlation_hint": {"type": "object"},
    },
    "required": ["symptom_type", "severity", "summary"],
}

LogEvidenceSchema = {
    "type": "object",
    "properties": {
        "error_type": {"type": "string"},
        "error_message": {"type": "string"},
        "summary": {"type": "string"},
        "found": {"type": "boolean"},
        #: **少见但可疑**的错误——占比小、但不属于"下游调用症状"的。
        #:
        #: 为什么必须单独留一栏：跨服务故障里**频率是反向指标**。一个下游故障会让
        #: 每个调用方都报超时（症状多），而真正的根因（如「必填参数 fin 没有传」）
        #: **可能只出现一次**。只输出一条的话，无论怎么挑都会把根因漏掉。
        #: 实测场景：494 条 `Feign Read timed out`（症状）+ 1 条
        #: `IllegalArgumentException: 必填参数 fin 没有传`（根因）。
        "notable": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "error_type": {"type": "string"},
                    "error_message": {"type": "string"},
                    "count": {"type": "number"},
                    #: 为什么它值得注意（如「非下游症状，且与工单的『报价单』沾边」）。
                    #: 有理由才叫"可疑"，没理由的稀有错误只是噪音。
                    "why": {"type": "string"},
                },
                "required": ["error_message", "why"],
            },
        },
    },
    "required": ["error_type", "error_message", "summary"],
}

TraceEvidenceSchema = {
    "type": "object",
    "properties": {
        # 工单没带 trace_id 时为 false —— **负证据，不是失败**（与 LogEvidence 的 found 同形）。
        # 这时 failing_service 允许为空：本节点**按请求**工作，没有请求标识就无从下手，
        # 如实说没有，好过退化成"再查一遍日志"（那与 log-analyst 是纯重复）。
        "found": {"type": "boolean"},
        "failing_service": {"type": "string"},
        "fault_span": {"type": "string"},
        "first_error": {"type": "string"},
        "latency_ms": {"type": "number"},
        "summary": {"type": "string"},
    },
    "required": ["failing_service", "summary"],
}

#: 候选的证据**来源**。加这个字段的直接起因是一次真实的伪归因：
#: 工单里明明没有服务名（`bug_report.cmdb_ci` 是空的），scope 却写「症状服务 order-service
#: **由 ticket 明确给出**」——它其实是从上游 triage 的**散文摘要**里读到的。
#: 来源不明会让下游拿着一个未经验证的假设继续跑，而且说得像已证实的事实。
#:
#: `upstream_summary` 不是"不能用"，是**必须如实标出来**：它是弱证据，
#: 与"工单字段里白纸黑字写着"完全不是一回事。
CANDIDATE_EVIDENCE_SOURCES = [
    "ticket_cmdb_ci",     # 工单 bug_report.cmdb_ci.name 明确给出
    "ticket_text",        # 工单正文里出现的服务名
    "graph_match",        # CMDB 图匹配（业务词命中 / app 关键词命中）
    "topology",           # 症状服务的依赖邻居（爆炸半径 / 上游根因）
    "upstream_summary",   # 上游 agent 的摘要里提到——**弱证据，不是工单给的**
]

CandidateServicesSchema = {
    "type": "object",
    "properties": {
        # 问题类型——决定"找什么"：故障处置找异常服务、变更升级找影响范围
        "intent": {"enum": ["fault", "change", "inquiry"]},
        # 高层抽象：把具体现象抬到业务概念（"打印结账单没反应" → ["打印结账单", "工单处理"]）
        "abstractions": {"type": "array", "items": {"type": "string"}},
        # **输入文本命中到的业务域**（原样取自 infer_candidate_services 的 matched_domains）。
        # 空数组 = 输入里没有业务域线索——**如实返回空，不要编一个**。
        "matched_domains": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"enum": ["enterprise", "journey", "portfolio", "domain"]},
                    "name": {"type": "string"},
                    "display_name": {"type": ["string", "null"]},
                    "app_count": {"type": "number"},
                },
                "required": ["type", "name"],
            },
        },
        # 头部候选**同分却分属不同业务域** → true。
        # 此时不要替用户挑一个：`insufficient` 置 true，并在 summary 里请他指明属于哪个业务域。
        "ambiguous": {"type": "boolean"},
        "candidate_services": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "confidence": {"enum": ["high", "medium", "low"]},
                    "impact": {"enum": ["high", "medium", "low"]},
                    # 命中的层次（app / domain / portfolio / journey / enterprise）与
                    # 独立路径数——分层匹配的产物，也是 confidence 的可核对依据
                    "matched_layers": {"type": "array", "items": {"type": "string"}},
                    "hit_paths": {"type": "number"},
                    "reasons": {"type": "array", "items": {"type": "string"}},
                    # 业务域路径（原样取自工具的 business_paths）。同名应用跨业务域时，
                    # **这是唯一能区分候选的依据**——不要自己改写或省略。
                    "business_paths": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "enterprise": {"type": ["string", "null"]},
                                "journey": {"type": ["string", "null"]},
                                "portfolio": {"type": ["string", "null"]},
                                "domain": {"type": ["string", "null"]},
                            },
                        },
                    },
                    # 在不在 matched_domains 的域内。
                    # **null ≠ false**：前者是"输入里没有域线索，无从判断"，后者是"确实不在"。
                    "in_domain": {"type": ["boolean", "null"]},
                    # 这条候选的证据从哪来。**必须如实标注**——把 upstream_summary
                    # 写成 ticket_* 就是伪造证据来源。
                    "evidence_source": {"enum": CANDIDATE_EVIDENCE_SOURCES},
                },
                "required": ["service", "confidence", "reasons", "evidence_source"],
            },
        },
        # 最高置信的那个（取数节点据此决定查几个）
        "primary_service": {"type": "string"},
        # 低置信 → true：把检索面放大到 top-N + 拓扑邻居
        "expand_search": {"type": "boolean"},
        # 信息不足以支撑后续推导 → true。**此时 candidate_services 应为空**，
        # 并把缺什么写进 summary——不要硬凑一个服务出来（design-v5.7 §3.6）。
        # `ambiguous: true` 是它的一个具体触发条件。
        "insufficient": {"type": "boolean"},
        # 缺什么才能继续（结构化，与 `insufficient: true` 配套）。
        # halt 节点就是靠它把"缺什么"带给人的——原先这个字段在图里被引用
        # （`$.nodes.scope.output.missing`）却**从没进过 schema**，于是永远解析成
        # None、halt 输出恒为 `missing: []`：读起来像"什么也不缺"，与同一份输出里
        # 的 `insufficient: true` 自相矛盾。
        "missing": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["intent", "candidate_services", "summary"],
}

MetricsEvidenceSchema = {
    "type": "object",
    "properties": {
        "cpu_percent": {"type": "number"},
        "memory_percent": {"type": "number"},
        "disk_percent": {"type": "number"},
        "error_rate": {"type": "number"},
        "p95_latency_ms": {"type": "number"},
        "anomalies": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["anomalies", "summary"],
}

InfraEvidenceSchema = {
    "type": "object",
    "properties": {
        "pod_name": {"type": "string"},
        "status": {"type": "string"},
        "restarts": {"type": "number"},
        "events": {"type": "array", "items": {"type": "string"}},
        "resource_usage": {"type": "object"},
        "summary": {"type": "string"},
    },
    "required": ["pod_name", "status", "summary"],
}

CodeLocationSchema = {
    "type": "object",
    "properties": {
        "service": {"type": "string"},
        "repo_url": {"type": "string"},
        "base_sha": {"type": "string"},
        "suspicious_files": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["service", "repo_url", "suspicious_files"],
}

KnowledgeEvidenceSchema = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "similar_incidents": {"type": "array", "items": {"type": "string"}},
        "suggested_actions": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["found", "summary"],
}

RootCauseSchema = {
    "type": "object",
    "properties": {
        #: **证据不足时为 null，不要编一个。**
        #:
        #: 它原先在 required 里、且只有四个取值——实测踩过：零证据的一条 run 里 rca 填了
        #: `config_issue`，而它自己的 hypothesis 写着「无代码缺陷证据，仅为剩余可能性中
        #: 最低跨度的一项」。**编出来的类型会被下游当真**：plan 会照着出修复计划、
        #: fix 会照着改代码。
        "root_cause_type": {
            "type": ["string", "null"],
            "enum": ["code_bug", "infra_issue", "config_issue", "dependency_issue", None],
        },
        #: **必填**——它是"这份结论可不可信"的唯一结构化出口（与 `service-scoper` 的
        #: `insufficient` 同形）。true 时 `root_cause_type` 应为 null。
        "insufficient": {"type": "boolean"},
        #: 缺什么才能定根因（与 `insufficient: true` 配套）。halt 转发给人与程序。
        "missing": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "hypotheses": {"type": "array", "items": {"type": "string"}},
        "ruled_out": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    #: `summary` 进 required：下游是按"有 summary"消费的（fix-planner 的入参契约、
    #: watch_run 的显示、halt 的中断理由）。它一度**不在** prompt 的 JSON 模板里，
    #: 于是两个场景的 run 里**每次都是缺失的**——见 prompts.py 里那条 ⚠️。
    "required": ["insufficient", "confidence", "summary", "hypotheses", "ruled_out"],
}

# 解决侧 Schema（M1 初版）
FixPlanSchema = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"enum": ["code_fix", "infra_action", "config_change"]},
                            "target": {"type": "string"},
                            "action": {"type": "string"},
                            "expected": {"type": "string"},
                        },
                        "required": ["type", "target", "action"],
                    },
                },
            },
            "required": ["summary", "steps"],
        }
    },
    "required": ["plan"],
}

FixDiffSchema = {
    "type": "object",
    "properties": {
        "diff": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"},
    },
    "required": ["diff", "files_changed"],
}

RemediationPlanSchema = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"enum": ["scale_deployment", "restart_pod", "patch_resources", "delete_temp_file"]},
                    "namespace": {"type": "string"},
                    "params": {"type": "object"},
                },
                "required": ["action", "namespace"],
            },
        }
    },
    "required": ["changes"],
}

TestResultSchema = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "tests_run": {"type": "integer"},
        "failed": {"type": "array", "items": {"type": "string"}},
        "coverage": {"type": "string"},
    },
    "required": ["passed", "tests_run", "failed"],
}

ReviewSchema = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "comments": {"type": "array", "items": {"type": "string"}},
        "risk": {"enum": ["low", "medium", "high"]},
    },
    "required": ["approved", "comments"],
}

CommitSchema = {
    "type": "object",
    "properties": {
        "pr_url": {"type": "string"},
        "pr_number": {"type": "integer"},
        "base_sha": {"type": "string"},
    },
    "required": ["pr_url", "pr_number"],
}

PostmortemSchema = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "root_cause": {"type": "string"},
        "actions": {"type": "array", "items": {"type": "string"}},
        "followups": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "root_cause"],
}

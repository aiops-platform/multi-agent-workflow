# -*- coding: utf-8 -*-
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
    },
    "required": ["error_type", "error_message", "summary"],
}

TraceEvidenceSchema = {
    "type": "object",
    "properties": {
        "failing_service": {"type": "string"},
        "fault_span": {"type": "string"},
        "first_error": {"type": "string"},
        "latency_ms": {"type": "number"},
        "summary": {"type": "string"},
    },
    "required": ["failing_service", "summary"],
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
        "root_cause_type": {
            "enum": ["code_bug", "infra_issue", "config_issue", "dependency_issue"],
        },
        "confidence": {"type": "number"},
        "hypotheses": {"type": "array", "items": {"type": "string"}},
        "ruled_out": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["root_cause_type", "confidence", "hypotheses", "ruled_out"],
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

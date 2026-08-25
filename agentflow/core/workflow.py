# -*- coding: utf-8 -*-
"""Workflow 模型 + 版本冻结（design §8.1 / §8.5）。

- ``Workflow.load_yaml``：从 YAML 文件 / 字符串 / dict 加载，构建 DAG。
- 版本冻结（§8.5）：Run 创建时计算 ``workflow_hash``（规范化 YAML 的 sha256），
  把完整 YAML + hash 存进 ``workflow_snapshots``；Resume 永远用原 snapshot，
  不受后续 YAML 修改影响。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .dag import DAG, Node


@dataclass
class Workflow:
    name: str
    version: str = "1.0.0"
    description: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    nodes: dict[str, Node] = field(default_factory=dict)
    dag: DAG | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    @classmethod
    def load_yaml(cls, source: str | Path | dict) -> "Workflow":
        if isinstance(source, dict):
            raw = source
        elif isinstance(source, Path):
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        else:
            # 字符串：是存在的文件路径 → 读文件；否则按内联 YAML 解析
            # （内联 YAML 可能很长，Path(source) 的 stat 会抛 ENAMETOOLONG，需 try 保护）
            text = source
            if "\n" not in source and len(source) < 512:
                p = Path(source)
                try:
                    if p.is_file():
                        text = p.read_text(encoding="utf-8")
                except OSError:
                    pass
            raw = yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise ValueError("Workflow YAML 顶层必须是对象")

        dag = DAG.build(raw.get("nodes", {}) or {}, raw.get("edges"))
        dag.check_params_refs()

        return cls(
            name=raw.get("name", "unnamed"),
            version=str(raw.get("version", "1.0.0")),
            description=raw.get("description", ""),
            inputs=raw.get("inputs", {}) or {},
            nodes=dag.nodes,
            dag=dag,
            raw=raw,
        )

    # ------------------------------------------------------------------
    # 版本冻结（§8.5）
    # ------------------------------------------------------------------
    def canonical_yaml(self) -> str:
        """规范化 YAML：固定 key 顺序，用于 hash 计算与快照存储。"""
        return yaml.safe_dump(self.raw, sort_keys=True, allow_unicode=True, default_flow_style=False)

    @property
    def workflow_hash(self) -> str:
        return hashlib.sha256(self.canonical_yaml().encode("utf-8")).hexdigest()

    def snapshot(self) -> dict[str, str]:
        """生成 snapshot 记录（存入 workflow_snapshots 表）。"""
        return {
            "workflow_name": self.name,
            "workflow_hash": self.workflow_hash,
            "workflow_yaml": self.canonical_yaml(),
        }

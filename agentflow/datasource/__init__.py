"""数据面适配层（**架构例外**）。

⚠️ **这里直连外部数据源，与既定姿态相悖，是有意为之。**

agentflow 的既定约定是「数据面查询（ES / Prometheus / K8s）全部搬出本进程，由独立仓库
``aiops-mcp-servers`` 的 ``aiops-datasource-mcp-server`` 提供」（design-v5.6，
见 CLAUDE.md「关键设计约束」）。本包是这个约定的一处显式破例：

- **动因**：遗留前端 Smart Inspection 页面（``service-intelligence-platform-ui/js/app.js``）
  每 5 秒拉一次服务指标，要的是**瞬时值 + UI 形状的信封**；而 MCP 侧现有的
  ``backends/prometheus.py`` 是为「喂给 LLM 当证据」设计的——只有 5 个领域语义指标、
  只有 ``query_range``（时间序列）、返回结构面向 agent。两者目标不同，硬套会两头别扭。
- **期限**：``TODO(v5.7)`` 把查询逻辑收编进 ``aiops-datasource-mcp-server``，届时删除本包。
- **收编时注意**：MCP 侧现有 ``_sel()`` 用的是 ``container!="POD"``，在测试床集群上
  **会算错**（sandbox 序列的 ``container`` 标签是缺失的，该写法会把 pod 级 + sandbox +
  应用容器三条序列全留下，CPU 接近翻倍）。正确写法是 ``container!=""``，详见
  ``app_indicators.build_queries`` 的注释。
"""
from __future__ import annotations

from .app_indicators import AppIndicatorsService, build_app_indicators_service
from .prometheus import DataSourceError, PrometheusClient, build_prometheus_client
from .service_meta import K8sServiceMetaSource, build_service_meta_source

__all__ = [
    "AppIndicatorsService",
    "DataSourceError",
    "K8sServiceMetaSource",
    "PrometheusClient",
    "build_app_indicators_service",
    "build_prometheus_client",
    "build_service_meta_source",
]

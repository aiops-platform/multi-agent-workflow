"""跨模块共享的异常类型。

单独成模块，避免调用方为了一个异常去 import 具体实现（原先 ``DataSourceError``
定义在 ``agents/datasources.py``，导致 ``sandbox/client.py`` 依赖那个已删除的数据源
适配器模块）。
"""
from __future__ import annotations


class DataSourceError(RuntimeError):
    """外部数据源/服务调用失败（沙箱 exec 服务、MCP 上游等）。"""

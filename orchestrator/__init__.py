"""统一适配层（Orchestrator / Gateway）。

自研代码只在本包内。第三方项目一律置于 `upstreams/`，通过进程 / 网络 /
文件边界交互，不 import、不修改、不复制（红线 R1–R3）。

对外能力：
- `orchestrator.cli`        命令行入口（14+ 条统一命令）
- `orchestrator.mcp_server` 把同一批命令暴露为 MCP 工具
"""

__version__ = "0.1.0-m0"

__all__ = ["__version__"]

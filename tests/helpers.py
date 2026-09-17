"""测试公共工具：构造隔离的 Context 与可控 Adapter。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.adapters.mock import MockAdapter
from orchestrator.bootstrap import Context, build_with_adapters
from orchestrator.registry import Manifest, manifests_dir


def mock_manifest(
    adapter_id: str = "mock",
    priority: int = 1,
    enabled: bool = True,
    min_interval_ms: int = 0,
    capability_levels: dict[str, str] | None = None,
    name: str | None = None,
) -> Manifest:
    """基于真实 mock.json 造一个 Manifest，只改需要改的字段。

    复用真实 manifest 而不是手搓，是为了保证测试里的能力声明与生产
    声明不会漂移 —— 否则测试通过但实际路由不到。
    """
    raw = json.loads((manifests_dir() / "mock.json").read_text(encoding="utf-8"))
    raw["id"] = adapter_id
    raw["priority"] = priority
    raw["enabled"] = enabled
    raw["limits"]["min_interval_ms"] = min_interval_ms
    if name:
        raw["name"] = name
    if capability_levels:
        for cap_id, level in capability_levels.items():
            raw["capabilities"][cap_id] = {"level": level}
    return Manifest.from_dict(raw, source_path=f"<test:{adapter_id}>")


class Sandbox:
    """一次测试用的隔离环境（临时根目录 + Context）。"""

    def __init__(self, adapters: list[Any] | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="orcho-test-")
        self.root = Path(self._tmp.name)
        self.ctx: Context = build_with_adapters(
            adapters if adapters is not None else [MockAdapter(mock_manifest())],
            root=self.root,
            echo=None,
        )

    def close(self) -> None:
        self._tmp.cleanup()

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # 便捷方法
    def orchestrator(self, confirmed: bool = False):
        self.ctx.orchestrator.confirmed = confirmed
        return self.ctx.orchestrator

    def run(self, command: str, confirmed: bool = False, **params: Any):
        self.ctx.orchestrator.confirmed = confirmed
        return self.ctx.orchestrator.run(command, params)

    def log_records(self, request_id: str) -> list[dict[str, Any]]:
        path = self.ctx.run_dir / f"{request_id}.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def raw_log(self, request_id: str) -> str:
        path = self.ctx.run_dir / f"{request_id}.raw.log"
        return path.read_text(encoding="utf-8") if path.is_file() else ""


def broken_adapter(
    adapter_id: str = "broken",
    priority: int = 50,
    faults: dict[str, Any] | None = None,
    **kwargs: Any,
) -> MockAdapter:
    return MockAdapter(mock_manifest(adapter_id, priority=priority, **kwargs), faults=faults or {})

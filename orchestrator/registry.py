"""能力注册表：Adapter 声明的汇总与查询。

Registry **只读 manifest，不读代码**。这样替换/新增 Adapter 不需要改动
Router——这是"某个项目失效时可单独替换 Adapter，不影响其他模块"
（用户原则 3）在代码层面的落点。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import capabilities
from .capabilities import CapabilityLevel


@dataclass
class CapabilityDecl:
    capability_id: str
    level: str
    note: str = ""
    params: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def supported(self) -> bool:
        return self.level in (CapabilityLevel.FULL, CapabilityLevel.PARTIAL)

    @property
    def is_full(self) -> bool:
        return self.level == CapabilityLevel.FULL

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"level": self.level}
        if self.note:
            payload["note"] = self.note
        if self.params:
            payload["params"] = list(self.params)
        return payload


@dataclass
class Manifest:
    """Adapter 声明文件的内存形态。"""

    id: str
    name: str
    kind: str = "subprocess"
    enabled: bool = True
    priority: int = 100
    upstream: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, CapabilityDecl] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: dict[str, Any], source_path: str | None = None) -> "Manifest":
        if not raw.get("id"):
            raise ValueError(f"manifest 缺少 id：{source_path}")
        decls: dict[str, CapabilityDecl] = {}
        for cap_id, decl in (raw.get("capabilities") or {}).items():
            if cap_id not in capabilities.CAPABILITIES:
                raise ValueError(
                    f"{source_path}: 声明了未定义的能力 {cap_id}；"
                    f"新增能力请先加到 capabilities.py"
                )
            level = str((decl or {}).get("level", CapabilityLevel.NONE))
            if level not in CapabilityLevel.ALL:
                raise ValueError(f"{source_path}: {cap_id} 的 level 非法：{level}")
            decls[cap_id] = CapabilityDecl(
                capability_id=cap_id,
                level=level,
                note=str((decl or {}).get("note", "")),
                params=tuple((decl or {}).get("params", ()) or ()),
                raw=dict(decl or {}),
            )
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name", raw["id"])),
            kind=str(raw.get("kind", "subprocess")),
            enabled=bool(raw.get("enabled", True)),
            priority=int(raw.get("priority", 100)),
            upstream=dict(raw.get("upstream") or {}),
            runtime=dict(raw.get("runtime") or {}),
            capabilities=decls,
            health=dict(raw.get("health") or {}),
            limits=dict(raw.get("limits") or {}),
            source_path=source_path,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "Manifest":
        p = Path(path)
        with p.open("r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh), source_path=str(p))

    @classmethod
    def load_dir(cls, directory: str | Path) -> list["Manifest"]:
        d = Path(directory)
        if not d.is_dir():
            return []
        return [cls.from_file(p) for p in sorted(d.glob("*.json"))]

    # ------------------------------------------------------------------
    @property
    def license(self) -> str:
        # JSON 里的 null 会变成 Python None；str(None) 会得到字符串 "None"，
        # 那会让"许可证未知"看起来像许可证叫 None。必须显式判空。
        value = self.upstream.get("license")
        return "unknown" if value is None else str(value)

    @property
    def pinned_commit(self) -> str:
        value = self.upstream.get("pinned_commit")
        return "" if value is None else str(value)

    @property
    def min_interval_ms(self) -> int:
        return int(self.limits.get("min_interval_ms", 1500))

    @property
    def max_concurrency(self) -> int:
        return int(self.limits.get("max_concurrency", 1))

    def level(self, capability_id: str) -> str:
        decl = self.capabilities.get(capability_id)
        return decl.level if decl else CapabilityLevel.NONE

    def supports(self, capability_id: str) -> bool:
        decl = self.capabilities.get(capability_id)
        return bool(decl and decl.supported)

    def supported_ids(self) -> list[str]:
        return sorted(c for c, d in self.capabilities.items() if d.supported)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
            "priority": self.priority,
            "upstream": self.upstream,
            "license": self.license,
            "pinned_commit": self.pinned_commit,
            "capabilities": {
                c: d.to_dict() for c, d in sorted(self.capabilities.items())
            },
            "supported_count": len(self.supported_ids()),
            "limits": self.limits,
        }


class CapabilityRegistry:
    """持有全部 Adapter，并回答"谁能干这件事"。"""

    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}

    # ------------------------------------------------------------------
    def register(self, adapter: Any, replace: bool = False) -> None:
        adapter_id = adapter.manifest.id
        if adapter_id in self._adapters and not replace:
            raise ValueError(f"Adapter 已注册：{adapter_id}")
        self._adapters[adapter_id] = adapter

    def unregister(self, adapter_id: str) -> None:
        self._adapters.pop(adapter_id, None)

    def get(self, adapter_id: str) -> Any | None:
        return self._adapters.get(adapter_id)

    def all(self) -> list[Any]:
        return list(self._adapters.values())

    def enabled(self) -> list[Any]:
        return [a for a in self._adapters.values() if a.manifest.enabled]

    def __len__(self) -> int:
        return len(self._adapters)

    # ------------------------------------------------------------------
    def candidates(self, capability_id: str) -> list[Any]:
        """返回可服务该能力的 Adapter，按优先级排序。

        是否入选问 **Adapter 的 `supports()`**（默认实现即读 manifest），
        而不是直接读 manifest —— 因为契约允许 Adapter 动态覆盖该判断，
        例如某个 Adapter 发现自己的上游依赖没装好，可以临时声明不可用，
        而不必去改声明文件。

        排序规则（决定 fallback 顺序）：
        1. **内置兜底 mock 永远最后**——它的数据是合成的，不能因为"声明得更完整"
           就压过真 Adapter
        2. `full` 优先于 `partial`——部分实现先上会掩盖更好的实现
        3. 同档内按 manifest.priority 升序
        4. 仍相同则按 id 稳定排序，保证结果可复现（测试依赖这一点）

        规则 1 是踩坑后加的（2026-09-18 实测事故）：C48 签到如实标 `partial`、
        mock 标 `full`，于是 `sign_in` **静默走了 mock**，返回"已签到"而平台上
        什么都没发生 —— 写操作上的假成功比报错危险得多。

        判据用 `id == "mock"`（内置兜底 Adapter 的固定 id）而不是 `kind == "mock"`：
        测试替身也常用 MockAdapter 承载自定义 manifest，用 kind 会误伤它们。
        """
        rows: list[tuple[int, int, str, Any]] = []
        for adapter in self.enabled():
            manifest: Manifest = adapter.manifest
            if not adapter.supports(capability_id):
                continue
            if manifest.id == "mock":
                tier = 2
            else:
                tier = 0 if manifest.level(capability_id) == CapabilityLevel.FULL else 1
            rows.append((tier, manifest.priority, manifest.id, adapter))
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        return [r[3] for r in rows]

    def providers(self, capability_id: str) -> list[dict[str, Any]]:
        return [
            {
                "adapter": a.manifest.id,
                "level": a.manifest.level(capability_id),
            }
            for a in self.candidates(capability_id)
        ]

    # ------------------------------------------------------------------
    def matrix(self) -> dict[str, dict[str, str]]:
        """能力 × Adapter 的支持度网格。"""
        grid: dict[str, dict[str, str]] = {}
        for cap_id in capabilities.all_ids():
            row: dict[str, str] = {}
            for adapter in self.all():
                row[adapter.manifest.id] = adapter.manifest.level(cap_id)
            grid[cap_id] = row
        return grid

    def coverage(self) -> dict[str, Any]:
        """覆盖率统计 + 缺口清单。"""
        covered: dict[str, list[str]] = {}
        gaps: list[str] = []
        for cap_id in capabilities.all_ids():
            providers = [a.manifest.id for a in self.candidates(cap_id)]
            covered[cap_id] = providers
            if not providers:
                gaps.append(cap_id)
        return {
            "total_capabilities": len(capabilities.CAPABILITIES),
            "covered": len(covered) - len(gaps),
            "uncovered": len(gaps),
            "gap_ids": gaps,
            "provided_by_adapters": sum(
                1 for c in capabilities.all_ids()
                if c in covered and covered[c] and c not in capabilities.SELF_BUILT
            ),
            "self_built": sorted(capabilities.SELF_BUILT),
            "by_capability": covered,
        }

    def summary(self) -> list[dict[str, Any]]:
        return [a.manifest.to_dict() for a in sorted(
            self.all(), key=lambda a: (a.manifest.priority, a.manifest.id)
        )]


def manifests_dir() -> Path:
    return Path(__file__).resolve().parent / "adapters" / "manifests"


def load_manifests(directory: str | Path | None = None) -> list[Manifest]:
    return Manifest.load_dir(directory or manifests_dir())


def build_from_factory(
    factory: Any, manifests: Iterable[Manifest] | None = None
) -> CapabilityRegistry:
    """用 `factory(manifest) -> adapter` 构造注册表。

    `factory` 由 bootstrap 提供，把 manifest 映射到具体 Adapter 类。
    这样 registry 不需要知道任何 Adapter 类名。
    """
    registry = CapabilityRegistry()
    for manifest in manifests if manifests is not None else load_manifests():
        adapter = factory(manifest)
        if adapter is not None:
            registry.register(adapter, replace=True)
    return registry

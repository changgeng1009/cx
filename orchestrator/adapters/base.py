"""Adapter 抽象基类。

契约（docs/03 §5.2）只有五个方法，其中只有 `invoke` 是必须实现的。
这个克制是有意的：**Adapter 作者的负担越轻，接入新项目的成本越低**，
而"某个项目失效时能单独替换"（用户原则 3）正依赖于接入成本足够低。

一条铁律：`invoke` **只负责把原生异常映射成 ErrorCategory**，
不得自行决定要不要 fallback —— 那是 Router 的职责。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..errors import AdapterError, not_supported
from ..models import (
    AccountContext,
    AdapterResult,
    ProbeResult,
    TaskContext,
)
from ..redact import redact
from ..registry import Manifest


class Adapter(ABC):
    def __init__(self, manifest: Manifest) -> None:
        self.manifest = manifest
        self._last_probe: ProbeResult | None = None
        self._setup_done = False

    # ------------------------------------------------------------------
    def setup(self, account: AccountContext) -> None:
        """幂等准备：生成配置文件、准备 workdir。

        **不得写入 `upstreams/`**（红线 R2）。配置一律写到账号工作区。
        """

    def probe(self) -> ProbeResult:
        """轻量探活。默认实现只看 manifest 是否启用。

        有真实上游的 Adapter 应覆盖此方法，但必须保证探活**无副作用**
        （读一次课程列表即可，不要触发任何写操作）。
        """
        if not self.manifest.enabled:
            return ProbeResult(healthy=False, detail="manifest.enabled = false")
        return ProbeResult(healthy=True, detail="manifest 已启用（未做真实探活）")

    def supports(self, capability_id: str) -> bool:
        """默认读 manifest。Adapter 可以动态覆盖（例如依赖外部服务时）。"""
        return self.manifest.supports(capability_id)

    @abstractmethod
    def invoke(
        self, capability_id: str, params: dict[str, Any], ctx: TaskContext
    ) -> AdapterResult:
        """执行能力。实现要求见类文档。"""

    def cancel(self, ctx: TaskContext) -> None:
        """协作式取消：置标志后等待当前任务点结束再终止子进程。"""

    def teardown(self) -> None:
        """清理临时资源。"""

    # ------------------------------------------------------------------
    def unsupported(self, capability_id: str) -> AdapterResult:
        return AdapterResult.failure(not_supported(self.manifest.id, capability_id))

    def failure(
        self, error: AdapterError, raw_output: str = ""
    ) -> AdapterResult:
        return AdapterResult.failure(error, raw_output=redact(raw_output))

    def success(
        self,
        data: Any,
        raw_output: str = "",
        warnings: list[str] | None = None,
    ) -> AdapterResult:
        return AdapterResult.success(
            data, raw_output=redact(raw_output), warnings=warnings
        )

    @property
    def adapter_id(self) -> str:
        return self.manifest.id

    # ------------------------------------------------------------------
    @staticmethod
    def timeout_for(capability_id: str, default_ms: int = 30000) -> float:
        """子类可覆盖：不同能力的合理超时不同（读接口快，跑课慢）。"""
        return default_ms / 1000.0

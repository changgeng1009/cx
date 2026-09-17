"""组装与依赖注入。

统一层用的是"显式装配"而不是框架/容器：一个 `build()` 函数把 registry、
router、storage、broker 拼起来。理由很实际——这层的可排障性优先于灵活性，
一个能一眼读完的装配函数比隐式容器更有价值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .adapters.chaoxing_cli import ChaoxingCliAdapter
from .adapters.mock import MockAdapter
from .adapters.xuexitong_mcp import XuexitongMcpAdapter
from .answer_broker import AnswerBroker
from .cookies import CookieStore
from .control import ControlChannel
from .registry import CapabilityRegistry, Manifest, load_manifests
from .router import TaskRouter
from .services import Orchestrator
from .session import AccountManager
from .state import StateStore

#: 真实上游的 Adapter 类在此注册。未注册的 manifest 即使 enabled
#: 也不会参与路由（比注册一个"永远失败"的占位实现更诚实）。
ADAPTER_FACTORIES: dict[str, Callable[[Manifest], Any]] = {
    "mock": MockAdapter,
    "xuexitong-mcp": XuexitongMcpAdapter,
    "chaoxing-cli": ChaoxingCliAdapter,
}


def project_root() -> Path:
    """仓库根目录（`orchestrator/` 的上一级）。"""
    return Path(__file__).resolve().parent.parent


@dataclass
class Context:
    root: Path
    run_dir: Path
    registry: CapabilityRegistry
    accounts: AccountManager
    state_store: StateStore
    control: ControlChannel
    broker: AnswerBroker
    cookie_store: CookieStore
    orchestrator: Orchestrator
    manifests: list[Manifest] = field(default_factory=list)

    def router(self, logger: Any = None) -> TaskRouter:
        return TaskRouter(self.registry, logger=logger)


def build(
    root: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
    adapter_classes: dict[str, Callable[[Manifest], Any]] | None = None,
    extra_adapters: list[Any] | None = None,
    manifests: list[Manifest] | None = None,
    broker_timeout_s: float = 120.0,
) -> Context:
    base = Path(root) if root is not None else project_root()
    run_dir = base / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)

    declared = manifests if manifests is not None else load_manifests()
    factories = dict(ADAPTER_FACTORIES)
    if adapter_classes:
        factories.update(adapter_classes)

    registry = CapabilityRegistry()
    for manifest in declared:
        if not manifest.enabled:
            continue
        factory = factories.get(manifest.id) or factories.get(manifest.kind)
        if factory is None:
            continue
        registry.register(factory(manifest), replace=True)
    for adapter in extra_adapters or []:
        registry.register(adapter, replace=True)

    accounts = AccountManager(base / "accounts")
    state_store = StateStore(base / "accounts")
    control = ControlChannel(run_dir / "control")
    broker = AnswerBroker(run_dir / "answer", timeout_s=broker_timeout_s)
    cookie_store = CookieStore(base / "accounts")

    orchestrator = Orchestrator(
        registry=registry,
        accounts=accounts,
        state_store=state_store,
        control=control,
        broker=broker,
        run_dir=run_dir,
        cookie_store=cookie_store,
        echo=echo,
    )

    return Context(
        root=base,
        run_dir=run_dir,
        registry=registry,
        accounts=accounts,
        state_store=state_store,
        control=control,
        broker=broker,
        cookie_store=cookie_store,
        orchestrator=orchestrator,
        manifests=declared,
    )


def build_with_adapters(
    adapters: list[Any],
    root: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> Context:
    """测试用：完全接管 Adapter 集合，不读 manifests。"""
    return build(
        root=root,
        echo=echo,
        manifests=[],
        extra_adapters=adapters,
    )

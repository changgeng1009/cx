"""能力注册表与 GPL 隔离（验收项 V10）。

V10 是法律而非技术项，所以必须由**静态检查**来守：不能靠"我们记得不要
import 上游"。一旦有人顺手写了 `import chaoxing`，统一层就被 GPL-3.0
传染了，而代码评审未必能发现。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator import capabilities
from orchestrator.bootstrap import ADAPTER_FACTORIES
from orchestrator.registry import Manifest, load_manifests, manifests_dir

from .helpers import Sandbox

ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR = ROOT / "orchestrator"

#: 一旦出现这些写法就说明进程边界被打破了。
#: 例外：BOUNDARY_WORKERS 里声明的 worker 胶水文件 —— 它们在**子进程**
#: 里通过运行时注入的路径动态加载上游，这正是进程边界方案本身（R1）。
FORBIDDEN_PATTERNS = (
    "sys.path.insert",
    "sys.path.append",
    "from upstreams",
    "import upstreams",
    "from chaoxing",
    "import chaoxing",
    "from xuexitong_mcp",
    "import xuexitong_mcp",
)

#: 允许动态加载上游的边界文件（相对于 orchestrator/）。这些文件必须满足：
#: 上游 import 出现在函数体内（子进程运行时才执行），且路径来自 stdin 注入。
BOUNDARY_WORKERS = {
    "adapters/xtmcp_worker.py",
    "adapters/cxcli_worker.py",
}


class ManifestTests(unittest.TestCase):
    def test_all_manifests_load(self) -> None:
        manifests = load_manifests()
        self.assertGreaterEqual(len(manifests), 6)
        ids = {m.id for m in manifests}
        for expected in ("mock", "chaoxing-cli", "advanced-bot", "xuexitong-mcp"):
            self.assertIn(expected, ids)

    def test_declared_capabilities_must_exist(self) -> None:
        raw = {
            "id": "bad",
            "capabilities": {"C999": {"level": "full"}},
        }
        with self.assertRaises(ValueError) as ctx:
            Manifest.from_dict(raw)
        self.assertIn("C999", str(ctx.exception))

    def test_invalid_level_rejected(self) -> None:
        raw = {"id": "bad", "capabilities": {"C06": {"level": "maybe"}}}
        with self.assertRaises(ValueError):
            Manifest.from_dict(raw)

    def test_missing_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Manifest.from_dict({"capabilities": {}})

    def test_gpl_projects_declare_process_isolation(self) -> None:
        for manifest in load_manifests():
            license_name = manifest.license.upper()
            if "GPL" in license_name:
                with self.subTest(adapter=manifest.id):
                    self.assertEqual(
                        manifest.upstream.get("isolation"),
                        "process",
                        f"{manifest.id} 是 {manifest.license}，必须声明 process 隔离",
                    )

    def test_explicit_none_declaration_is_preserved(self) -> None:
        """A1 显式声明不支持 C24，Router 才能直接把含图题目路由到 A2。"""
        chaoxing = next(m for m in load_manifests() if m.id == "chaoxing-cli")
        self.assertFalse(chaoxing.supports("C24"))
        self.assertEqual(chaoxing.level("C24"), "none")
        advanced = next(m for m in load_manifests() if m.id == "advanced-bot")
        self.assertTrue(advanced.supports("C24"))

    def test_real_adapters_are_not_enabled_in_m0(self) -> None:
        """真实 Adapter 可以启用（M1 起），但必须同时满足：
        ① 工厂已注册；② upstream 已锁 commit；③ 许可证允许当前接触方式。
        三个条件缺一个，enabled 就等于"声称支持但实际不可用"。
        """
        for manifest in load_manifests():
            if manifest.id == "mock":
                continue
            if not manifest.enabled:
                continue
            with self.subTest(adapter=manifest.id):
                self.assertIn(
                    manifest.id,
                    ADAPTER_FACTORIES,
                    "manifest.enabled 但工厂未注册",
                )
                self.assertTrue(
                    manifest.upstream.get("pinned_commit"),
                    "enabled 的真实 Adapter 必须锁 commit",
                )
                self.assertIsNotNone(manifest.upstream.get("license"))


class CapabilityRegistryTests(unittest.TestCase):
    def test_capability_taxonomy_matches_v2_scope(self) -> None:
        self.assertEqual(len(capabilities.CAPABILITIES), 51)

    def test_groups_are_complete_and_ordered(self) -> None:
        grouped = capabilities.by_group()
        total = sum(len(items) for items in grouped.values())
        self.assertEqual(total, 51)
        self.assertEqual(list(grouped), list(capabilities.GROUP_ORDER))

    def test_self_built_set_matches_design_doc(self) -> None:
        expected = {
            "C03", "C04", "C09", "C37", "C40", "C41", "C42",
            "C43", "C44", "C45", "C46", "C47",
            "C48", "C49", "C50", "C51",
        }
        self.assertEqual(set(capabilities.SELF_BUILT), expected)
        self.assertEqual(len(expected), 16)

    def test_mock_covers_all_but_browser_automation(self) -> None:
        with Sandbox() as sandbox:
            coverage = sandbox.ctx.registry.coverage()
            self.assertEqual(coverage["gap_ids"], ["C38"])

    def test_matrix_shape(self) -> None:
        with Sandbox() as sandbox:
            matrix = sandbox.ctx.registry.matrix()
            self.assertEqual(len(matrix), 51)
            for row in matrix.values():
                self.assertIn("mock", row)


class GplIsolationTests(unittest.TestCase):
    def test_no_forbidden_import_or_path_manipulation(self) -> None:
        offenders: list[str] = []
        for path in sorted(ORCHESTRATOR.rglob("*.py")):
            rel = str(path.relative_to(ORCHESTRATOR)).replace("\\", "/")
            text = path.read_text(encoding="utf-8")
            if rel in BOUNDARY_WORKERS:
                # 边界文件豁免静态扫描，但要保证：上游 import 不在模块
                # 顶层（否则统一层主进程 import 该模块就会连带加载上游）。
                top_prefixes = (
                    "from xuexitong_mcp", "import xuexitong_mcp",
                    "from chaoxing", "import chaoxing",
                    "from upstreams", "import upstreams",
                    "from api.", "import api.",  # cxcli_worker 的上游包
                )
                for line in text.splitlines():
                    if line.startswith(top_prefixes):
                        offenders.append(f"{rel} 顶层含 {line.strip()!r}")
                continue
            for pattern in FORBIDDEN_PATTERNS:
                if pattern in text:
                    offenders.append(f"{path.relative_to(ROOT)} 含 {pattern!r}")
        self.assertEqual(offenders, [], "统一层破坏了进程边界")

    def test_only_mock_adapter_factory_is_registered(self) -> None:
        """工厂注册表只允许 mock + 显式接入的真实 Adapter。

        新增条目时必须同步：① manifest 锁 commit；② 本文件确认其许可证
        允许当前接触方式（subprocess = 任何许可证均可；import 仅 MIT 类）。
        """
        self.assertEqual(
            set(ADAPTER_FACTORIES),
            {"mock", "xuexitong-mcp", "chaoxing-cli"},
            "接入新 Adapter 时请更新本断言，并核对许可证与隔离方式",
        )
        # xuexitong-mcp 是 MIT + subprocess：允许的接触方式组合
        m = {m.id: m for m in load_manifests()}["xuexitong-mcp"]
        self.assertEqual(m.upstream.get("license"), "MIT")
        self.assertTrue(m.upstream.get("pinned_commit"))
        # chaoxing-cli 是 GPL-3.0：**只允许 subprocess**，且 worker 必须在
        # BOUNDARY_WORKERS 豁免清单里（子进程内动态加载=进程边界）
        c = {m.id: m for m in load_manifests()}["chaoxing-cli"]
        self.assertEqual(c.upstream.get("license"), "GPL-3.0")
        self.assertEqual(c.upstream.get("isolation"), "process")
        self.assertTrue(c.upstream.get("pinned_commit"))

    def test_config_injection_targets_account_workdir_not_upstream(self) -> None:
        with Sandbox() as sandbox:
            path = sandbox.ctx.accounts.write_adapter_config(
                "acc_01", "[common]\nusername = 13800000000\n"
            )
            self.assertIn("accounts", str(path))
            self.assertNotIn("upstreams", str(path))
            self.assertTrue(path.is_file())

    def test_upstreams_directory_is_not_required_to_exist(self) -> None:
        """M0 不 clone 任何上游；缺 upstreams/ 不应导致任何失败。"""
        with Sandbox() as sandbox:
            self.assertTrue(sandbox.run("adapters").ok)
            self.assertTrue(sandbox.run("list_courses").ok)


if __name__ == "__main__":
    unittest.main()

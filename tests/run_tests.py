"""M0 验收测试入口。

零依赖：只用 stdlib（unittest），因为统一层本身也是零第三方依赖。
用法：
    python tests/run_tests.py            # 全部
    python tests/run_tests.py -v         # 详细
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    verbosity = 2 if ("-v" in args or "--verbose" in args) else 1
    args = [a for a in args if a not in ("-v", "--verbose")]

    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=str(Path(__file__).resolve().parent),
        pattern="test_*.py",
        top_level_dir=str(ROOT),
    )
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)

    print()
    print("=" * 68)
    print(f"运行 {result.testsRun} 项 ｜ 失败 {len(result.failures)} ｜ "
          f"错误 {len(result.errors)} ｜ 跳过 {len(result.skipped)}")
    print("=" * 68)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""M6 探针：摸清平台"活动列表"接口的真实字段（只读，纯 GET）。

为什么要探针而不是猜字段：签到活动的原始 JSON 由平台决定，上游只原样透传，
统一层必须按真实字段做规范化。猜字段名 = 上线即错。

用法：
    .venv/Scripts/python.exe scripts/sign_probe.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "upstreams" / "chaoxing"
ACCOUNT_DIR = ROOT / "accounts" / "acc_01" / "upstream" / "chaoxing"

# 复用 worker 的环境准备（字体表 + 代理绕过），否则 session 建不起来
sys.path.insert(0, str(ROOT))
from orchestrator.adapters.cxcli_worker import prepare_upstream_env  # noqa: E402

prepare_upstream_env(str(UPSTREAM))
os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
os.environ["no_proxy"] = os.environ["NO_PROXY"]
os.chdir(ACCOUNT_DIR)  # 上游从 CWD 读 cookies.txt

from api.base import Account, Chaoxing  # noqa: E402

COURSES = [
    ("233418133", "电机与电气控制"),
    ("266899827", "建筑防排烟技术（第4期）"),
    ("228241788", "建筑构造与识图"),
    ("267147955", "工业设备安装技术"),
    ("249696403", "中共党史"),
]


def main() -> None:
    client = Chaoxing(Account("", ""), tiku=None)
    ok, state = client.login(login_with_cookies=True)
    print(f"登录: ok={ok} state={state}\n")
    if not ok:
        return

    for course_id, name in COURSES:
        course = {"courseId": course_id, "clazzId": "", "cpi": ""}
        # clazzId 从课程列表补齐
        for c in client.get_course_list() or []:
            if str(c.get("courseId")) == course_id:
                course = c
                break
        if not course.get("clazzId"):
            print(f"[{name}] 未在课程列表找到，跳过")
            continue
        try:
            acts = client.get_activity_list(course)
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] 取活动失败: {type(exc).__name__}: {exc}")
            continue
        print(f"===== {name}（clazz={course.get('clazzId')}）活动数: {len(acts or [])} =====")
        for act in (acts or [])[:3]:
            print(json.dumps(act, ensure_ascii=False, indent=2)[:1200])
            print("  ---")
        if not acts:
            print("  （无活动）")


if __name__ == "__main__":
    main()

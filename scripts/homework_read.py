"""只读作业阅读器：拉取指定课程的作业题目（不提交、不作答）。

用法：
    .venv/Scripts/python.exe scripts/homework_read.py "工业设备安装技术"

原理：ChaoxingClient（xuexitong-mcp，MIT）fetch_homework 拿作业 URL，
GET 作答页后解析题干/小问。只发 GET 请求，零写副作用。
"""

from __future__ import annotations

import html as html_mod
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "upstreams" / "xuexitong-mcp"))

from xuexitong_mcp.client import ChaoxingClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def read_homework(keyword: str) -> None:
    client = ChaoxingClient(config_dir=str(ROOT / "accounts" / "acc_01" / "upstream" / "xtmcp"))
    course = client.find_course(keyword)
    if course is None:
        print(f"未找到课程：{keyword}")
        return
    print(f"课程：{course['name']}（{course['courseid']}）")
    hw = client.fetch_homework(course)
    for item in hw["items"]:
        print(f"\n===== 作业 {item['index']}: {item['title']} [{item['status']}] =====")
        resp = client.get(item["url"])
        body = re.sub(r"<script[\s\S]*?</script>", "", resp.text)
        body = re.sub(r"<style[\s\S]*?</style>", "", body)
        text = html_mod.unescape(re.sub(r"<[^>]+>", "\n", body))
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        start = next(
            (i for i, l in enumerate(lines) if re.match(r"^[一二三四五六七八九十]\.\s", l)),
            None,
        )
        if start is None:
            print("（未解析到题目区）")
            continue
        for line in lines[start:]:
            if line.startswith("提示") or re.fullmatch(r"\d+", line):
                continue
            print("  " + line[:200])


if __name__ == "__main__":
    read_homework(sys.argv[1] if len(sys.argv) > 1 else "工业设备安装技术")

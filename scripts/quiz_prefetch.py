"""只读取题器：把一个课程所有「章节检测」的题目一次捞出来（纯 GET，零写副作用）。

为什么要它：上游 AI provider 是**逐题**问的（一问一答，答案靠 CacheDAO 缓存）。
一门课 24 个章节检测 ≈ 上百道题，逐题实时问答要上百次往返。
这里的做法是：只读地把题目全捞出来 → 一次性作答 → 写进上游的 cache.json →
正式跑批时全部命中缓存，秒过（M5 实测 2.1 就是 17 秒跑完 4 题）。

取题接口与上游 `api.base.study_work` 完全一致（同一个 URL + 同一组参数）：
    GET https://mooc1.chaoxing.com/mooc-ans/api/work
该接口只返回题目 JSON，不产生作答记录（作答记录由提交接口创建）。

用法：
    .venv/Scripts/python.exe scripts/quiz_prefetch.py <course_id> [-o 输出文件]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "upstreams" / "chaoxing"
ACCOUNT_ID = "acc_01"
ACCOUNT_DIR = ROOT / "accounts" / ACCOUNT_ID / "upstream" / "chaoxing"

sys.path.insert(0, str(ROOT))
from orchestrator.adapters.cxcli_worker import (  # noqa: E402
    prepare_upstream_env,
    scan_course_points,
)

prepare_upstream_env(str(UPSTREAM))
os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
os.environ["no_proxy"] = os.environ["NO_PROXY"]
os.chdir(ACCOUNT_DIR)  # 上游从 CWD 读 cookies.txt 与 cache.json

from api.base import Account, Chaoxing, multi_cut  # noqa: E402
from api.decode import decode_questions_info  # noqa: E402

WORK_URL = "https://mooc1.chaoxing.com/mooc-ans/api/work"


def fetch_quiz_questions(client, course: dict, job: dict, job_info: dict) -> dict:
    """复刻上游 study_work 的取题请求（同一 URL、同一参数）。"""
    session = client.session if hasattr(client, "session") else None
    if session is None:
        from api.base import SessionManager  # noqa: PLC0415

        session = SessionManager.get_session()
    resp = session.get(
        WORK_URL,
        params={
            "api": "1",
            "workId": str(job["jobid"]).replace("work-", ""),
            "jobid": job["jobid"],
            "originJobId": job["jobid"],
            "needRedirect": "true",
            "skipHeader": "true",
            "knowledgeid": str(job_info["knowledgeid"]),
            "ktoken": job_info["ktoken"],
            "cpi": job_info["cpi"],
            "ut": "s",
            "clazzId": course["clazzId"],
            "type": "",
            "enc": job["enc"],
            "mooc2": "1",
            "courseid": course["courseId"],
        },
    )
    if "教师未创建完成该测验" in resp.text:
        return {"error": "教师未创建完成该测验"}
    decoded = decode_questions_info(resp.text) or {}
    questions = decoded.get("questions") or []
    rows = []
    for q in questions:
        options = multi_cut(q.get("options") or "", resp.text) or []
        rows.append(
            {
                "title": q.get("title"),
                "type": q.get("type"),
                "options": [o.strip() for o in options if o and o.strip()],
                "id": q.get("id"),
            }
        )
    return {"questions": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("course_id")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--account", default="acc_01", help="账号 ID（le 工作区，默认 acc_01）")
    args = ap.parse_args()

    # 多账号：账号工作区决定 cookie 与 cache.json 的位置
    global ACCOUNT_DIR
    ACCOUNT_DIR = ROOT / "accounts" / args.account / "upstream" / "chaoxing"
    os.chdir(ACCOUNT_DIR)

    # 注意：脚本会 chdir 到账号工作区（上游从 CWD 读 cookie/cache），
    # 所以相对输出路径必须按 ROOT 解析 —— 否则会落到账号目录里（踩过两次）
    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = ROOT / out_path
    else:
        out_path = ROOT / "runs" / f"quiz_prefetch_{args.course_id}.json"
    client = Chaoxing(Account("", ""), tiku=None)
    ok, state = client.login(login_with_cookies=True)
    if not ok:
        print(f"登录失败：{state}")
        return

    courses = client.get_course_list() or []
    course = next((c for c in courses if str(c.get("courseId")) == str(args.course_id)), None)
    if course is None:
        print(f"课程不存在：{args.course_id}")
        return
    print(f"课程：{course.get('title')}（{course.get('courseId')}）")

    points = scan_course_points(client, course)
    total, quizzes = 0, 0
    report: list[dict] = []
    for point in points:
        jobs, job_info = client.get_job_list(course, point)
        if job_info.get("notOpen", False):
            continue
        for job in jobs or []:
            if str(job.get("type")) != "workid":
                continue
            quizzes += 1
            try:
                data = fetch_quiz_questions(client, course, job, job_info)
            except Exception as exc:  # noqa: BLE001
                data = {"error": f"{type(exc).__name__}: {exc}"}
            qs = data.get("questions") or []
            total += len(qs)
            report.append(
                {
                    "chapter_id": str(point.get("id")),
                    "chapter": point.get("title"),
                    "job_id": job.get("jobid"),
                    "job_title": job.get("name") or job.get("title"),
                    "question_count": len(qs),
                    "error": data.get("error"),
                    "questions": qs,
                }
            )
            print(f"  [{quizzes}] {point.get('title')} / {job.get('name') or ''} → {len(qs)} 题")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"course_id": str(args.course_id), "quizzes": report}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n共 {quizzes} 个章节检测 / {total} 道题 → {out_path}")


if __name__ == "__main__":
    main()

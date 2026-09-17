"""演示/测试用样例数据。

存在的两个理由：
1. **让 M0 可离线验收**。真实账号接入前，Router、状态机、日志、CLI 的
   全部行为都必须能跑出来给人看——否则"设计对不对"只能靠读代码。
2. **让 MockAdapter 的输出形态与真实上游一致**。如果 mock 返回的数据
   形状和 A1/C1 真实返回不同，M0 的测试就是自欺欺人。

因此这里的字段命名刻意对齐上游的术语（courseid/clazzid/cpi、enc 等）。
"""

from __future__ import annotations

from typing import Any

from .models import (
    Chapter,
    Course,
    HomeworkItem,
    TaskPoint,
    TaskPointStatus,
    TaskType,
)

COURSE_1 = "240100001"
COURSE_2 = "240100002"
COURSE_3 = "240100003"


def make_courses() -> list[Course]:
    return [
        Course(
            course_id=COURSE_1,
            clazz_id="58812345",
            cpi="240100001",
            name="数字电路",
            teacher="李明",
            fid="7213",
        ),
        Course(
            course_id=COURSE_2,
            clazz_id="58812346",
            cpi="240100002",
            name="大学英语（四）",
            teacher="王芳",
            fid="7213",
        ),
        Course(
            course_id=COURSE_3,
            clazz_id="58812347",
            cpi="240100003",
            name="马克思主义基本原理",
            teacher="张伟",
            fid="7213",
        ),
    ]


_CHAPTERS: dict[str, list[Chapter]] = {
    COURSE_1: [
        Chapter("ch_01", "第一章 逻辑代数基础", 1),
        Chapter("ch_02", "第二章 门电路", 2),
        Chapter("ch_03", "第三章 组合逻辑电路", 3),
        Chapter("ch_04", "第四章 触发器", 4),
    ],
    COURSE_2: [
        Chapter("ch_01", "Unit 1 Campus Life", 1),
        Chapter("ch_02", "Unit 2 Technology", 2),
    ],
    COURSE_3: [
        Chapter("ch_01", "绪论 什么是马克思主义", 1),
    ],
}


def make_chapters(course_id: str) -> list[Chapter]:
    return list(_CHAPTERS.get(course_id, []))


def _tp(
    task_point_id: str,
    task_type: TaskType,
    title: str,
    chapter_id: str,
    chapter_name: str,
    status: TaskPointStatus,
    needs_image: bool = False,
) -> TaskPoint:
    return TaskPoint(
        task_point_id=task_point_id,
        task_type=task_type,
        title=title,
        chapter_id=chapter_id,
        chapter_name=chapter_name,
        status=status,
        needs_image=needs_image,
    )


_TASK_POINTS: dict[str, list[TaskPoint]] = {
    COURSE_1: [
        _tp("tp_001", TaskType.VIDEO, "1.1 数制与码制", "ch_01", "第一章 逻辑代数基础", TaskPointStatus.DONE),
        _tp("tp_002", TaskType.VIDEO, "1.2 逻辑代数基本定律", "ch_01", "第一章 逻辑代数基础", TaskPointStatus.TODO),
        _tp("tp_003", TaskType.DOCUMENT, "1.3 章节讲义（PDF）", "ch_01", "第一章 逻辑代数基础", TaskPointStatus.TODO),
        _tp("tp_004", TaskType.VIDEO, "2.1 TTL 门电路", "ch_02", "第二章 门电路", TaskPointStatus.TODO),
        _tp("tp_005", TaskType.QUIZ, "2.2 章节测验", "ch_02", "第二章 门电路", TaskPointStatus.TODO),
        _tp("tp_006", TaskType.PPT, "2.3 课件（PPT）", "ch_02", "第二章 门电路", TaskPointStatus.TODO),
        _tp("tp_007", TaskType.VIDEO, "3.1 组合逻辑分析", "ch_03", "第三章 组合逻辑电路", TaskPointStatus.TODO),
        _tp("tp_008", TaskType.READING, "3.2 拓展阅读", "ch_03", "第三章 组合逻辑电路", TaskPointStatus.TODO),
        _tp("tp_009", TaskType.QUIZ, "3.3 章节测验（未开放）", "ch_03", "第三章 组合逻辑电路", TaskPointStatus.LOCKED),
        _tp(
            "tp_010",
            TaskType.QUIZ,
            "3.4 含图分析题",
            "ch_03",
            "第三章 组合逻辑电路",
            TaskPointStatus.TODO,
            needs_image=True,
        ),
        _tp("tp_011", TaskType.AUDIO, "4.1 音频讲解", "ch_04", "第四章 触发器", TaskPointStatus.TODO),
        _tp("tp_012", TaskType.LIVE, "4.2 直播回放", "ch_04", "第四章 触发器", TaskPointStatus.TODO),
        _tp("tp_013", TaskType.DISCUSSION, "4.3 专题讨论", "ch_04", "第四章 触发器", TaskPointStatus.TODO),
    ],
    COURSE_2: [
        _tp("tp_101", TaskType.VIDEO, "1.1 Listening", "ch_01", "Unit 1 Campus Life", TaskPointStatus.DONE),
        _tp("tp_102", TaskType.READING, "1.2 Reading", "ch_01", "Unit 1 Campus Life", TaskPointStatus.TODO),
        _tp("tp_103", TaskType.VIDEO, "2.1 Listening", "ch_02", "Unit 2 Technology", TaskPointStatus.TODO),
        _tp("tp_104", TaskType.QUIZ, "2.2 Unit Test", "ch_02", "Unit 2 Technology", TaskPointStatus.TODO),
    ],
    COURSE_3: [
        _tp("tp_201", TaskType.VIDEO, "0.1 绪论", "ch_01", "绪论 什么是马克思主义", TaskPointStatus.DONE),
        _tp("tp_202", TaskType.DOCUMENT, "0.2 参考资料", "ch_01", "绪论 什么是马克思主义", TaskPointStatus.TODO),
    ],
}


def make_task_points(course_id: str) -> list[TaskPoint]:
    return list(_TASK_POINTS.get(course_id, []))


def task_point_index() -> dict[str, list[TaskPoint]]:
    return {cid: list(items) for cid, items in _TASK_POINTS.items()}


_HOMEWORK: dict[str, list[HomeworkItem]] = {
    COURSE_1: [
        HomeworkItem(COURSE_1, 1, "作业1：逻辑函数化简", submitted=True, progress="已完成", due_at="2026-09-10 23:59", score="92"),
        HomeworkItem(COURSE_1, 2, "作业2：组合逻辑设计", submitted=False, progress="未提交", due_at="2026-09-20 23:59", score=None),
        HomeworkItem(COURSE_1, 3, "作业3：触发器时序分析", submitted=False, progress="未提交", due_at="2026-10-08 23:59", score=None),
    ],
    COURSE_2: [
        HomeworkItem(COURSE_2, 1, "Unit 1 Writing Task", submitted=True, progress="已完成", due_at="2026-09-05 23:59", score="88"),
    ],
    COURSE_3: [
        HomeworkItem(COURSE_3, 1, "读书报告", submitted=False, progress="未提交", due_at="2026-09-25 23:59", score=None),
    ],
}


def make_homework(course_id: str | None = None) -> list[HomeworkItem]:
    if course_id:
        return list(_HOMEWORK.get(course_id, []))
    items: list[HomeworkItem] = []
    for group in _HOMEWORK.values():
        items.extend(group)
    return items


def make_notices() -> list[dict[str, Any]]:
    return [
        {
            "title": "关于《数字电路》第 3 章测验开放的通知",
            "content": "第 3 章测验将于 9 月 20 日开放，请同学们按时完成。",
            "sender": "李明",
            "time": "2026-09-16 09:12",
            "unread": True,
        },
        {
            "title": "大学英语（四）期中考试安排",
            "content": "期中考试定于第 8 周周三下午，考试形式为闭卷。",
            "sender": "王芳",
            "time": "2026-09-15 16:40",
            "unread": True,
        },
        {
            "title": "国庆假期调课通知",
            "content": "10 月 1 日至 7 日放假，10 月 8 日（周四）补上周三课程。",
            "sender": "教务处",
            "time": "2026-09-14 11:05",
            "unread": False,
        },
    ]


def make_schedule(week: int = 3) -> dict[str, Any]:
    return {
        "week": week,
        "first_week_date": "2026-09-01",
        "lessons": [
            {"name": "数字电路", "weekday": 3, "date": "2026-09-16", "section": "3-4 节", "location": "工科楼 A302", "teacher": "李明"},
            {"name": "大学英语（四）", "weekday": 3, "date": "2026-09-16", "section": "5-6 节", "location": "外语楼 B105", "teacher": "王芳"},
            {"name": "马克思主义基本原理", "weekday": 5, "date": "2026-09-18", "section": "1-2 节", "location": "文科楼 C201", "teacher": "张伟"},
        ],
    }


#: 模拟上游 A1 发给 OpenAI 兼容代理的 prompt。
#: 真实场景里这段文本由 A1 的 `api/answer.py` 拼装，统一层不该假设其格式，
#: 所以只做"尽力结构化 + 保留原文"（docs/03 §11.3）。
def make_upstream_question_prompt() -> str:
    return (
        "1. 下列关于 TTL 与非门输入端的说法，正确的是（ ）\n"
        "A. 悬空相当于高电平\n"
        "B. 悬空相当于低电平\n"
        "C. 悬空时输出不确定\n"
        "D. 必须接地\n"
        "2. 组合逻辑电路的输出仅取决于当前输入。（ ）\n"
        "A. 正确\n"
        "B. 错误\n"
        "3. 逻辑函数 F = A·B + A·C 化简结果为（ ）\n"
        "A. A(B+C)\n"
        "B. A\n"
        "C. B+C\n"
        "D. AB+AC+B C\n"
    )


def make_sign_activity() -> dict[str, Any]:
    """模拟一个待签到活动。"""
    return {
        "course_id": COURSE_1,
        "course_name": "数字电路",
        "activity_id": "sign_20260917_01",
        "sign_type": "normal",
        "title": "第 3 周课堂签到",
        "open_at": "2026-09-17 10:00",
        "deadline": "2026-09-17 10:15",
        "status": "pending",
    }

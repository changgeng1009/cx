"""能力注册表的**原始定义**（单一事实来源）。

这里只定义"有哪些能力、每个能力是什么"。**谁提供了这个能力**由各
Adapter 的 `manifest.json` 声明，由 `registry.py` 汇总。

两件事分开是有意的：新增一个 Adapter 不应该需要改动本文件；
新增一个能力才需要改。这样"某个项目以后失效时单独替换 Adapter"
（用户原则 3）才不会牵动能力定义。
"""

from __future__ import annotations

from dataclasses import dataclass


class Group:
    AUTH = "认证与会话"
    STRUCT = "课程与结构"
    TASK = "任务执行"
    ANSWER = "答题"
    READ = "读取扩展"
    SYSTEM = "平台系统能力"
    AGENT = "Agent 答题链路"
    SIGN = "签到"


#: 组的展示顺序
GROUP_ORDER = (
    Group.AUTH,
    Group.STRUCT,
    Group.TASK,
    Group.ANSWER,
    Group.READ,
    Group.AGENT,
    Group.SYSTEM,
    Group.SIGN,
)


@dataclass(frozen=True)
class Capability:
    capability_id: str
    group: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {
            "capability_id": self.capability_id,
            "group": self.group,
            "name": self.name,
        }


_RAW: tuple[tuple[str, str, str], ...] = (
    # ---- A 组 认证与会话 ----
    ("C01", Group.AUTH, "账号密码登录"),
    ("C02", Group.AUTH, "Cookie/Session 持久化"),
    ("C03", Group.AUTH, "会话失效自动重登"),
    ("C04", Group.AUTH, "多账号管理"),
    ("C05", Group.AUTH, "验证码处理"),
    # ---- B 组 课程与结构 ----
    ("C06", Group.STRUCT, "课程列表"),
    ("C07", Group.STRUCT, "课程详情"),
    ("C08", Group.STRUCT, "章节树读取"),
    ("C09", Group.STRUCT, "任务点识别与状态"),
    ("C10", Group.STRUCT, "单课进度查询"),
    ("C11", Group.STRUCT, "全课程进度总览"),
    # ---- C 组 任务执行 ----
    ("C12", Group.TASK, "视频任务"),
    ("C13", Group.TASK, "音频任务"),
    ("C14", Group.TASK, "文档/阅读任务"),
    ("C15", Group.TASK, "PPT 任务"),
    ("C16", Group.TASK, "直播任务"),
    ("C17", Group.TASK, "讨论/互动任务"),
    ("C18", Group.TASK, "自动切换章节"),
    ("C19", Group.TASK, "未开放/已关闭任务点策略"),
    ("C20", Group.TASK, "播放倍速控制"),
    ("C21", Group.TASK, "刷学习次数"),
    # ---- D 组 答题 ----
    ("C22", Group.ANSWER, "题库查询"),
    ("C23", Group.ANSWER, "AI 解题"),
    ("C24", Group.ANSWER, "图片题识别"),
    ("C25", Group.ANSWER, "字体反爬解密"),
    ("C26", Group.ANSWER, "章节测验答题"),
    ("C27", Group.ANSWER, "作业自动提交"),
    ("C28", Group.ANSWER, "答案校验"),
    # ---- E 组 读取扩展 ----
    ("C29", Group.READ, "作业读取"),
    ("C30", Group.READ, "考试信息读取"),
    ("C31", Group.READ, "通知/消息读取"),
    ("C32", Group.READ, "资源/课件下载"),
    ("C33", Group.READ, "课表读取"),
    # ---- F 组 平台系统能力 ----
    ("C34", Group.SYSTEM, "CLI 接口"),
    ("C35", Group.SYSTEM, "HTTP API"),
    ("C36", Group.SYSTEM, "MCP 接口"),
    ("C37", Group.SYSTEM, "结构化日志"),
    ("C38", Group.SYSTEM, "浏览器自动化"),
    ("C39", Group.SYSTEM, "外部通知推送"),
    ("C40", Group.SYSTEM, "进程级暂停/恢复"),
    ("C41", Group.SYSTEM, "失败重试"),
    ("C42", Group.SYSTEM, "风控检测"),
    # ---- G 组 Agent 答题链路 ----
    ("C43", Group.AGENT, "题目导出"),
    ("C44", Group.AGENT, "答卷注入"),
    ("C45", Group.AGENT, "答题-回复循环编排"),
    ("C46", Group.AGENT, "OpenAI 兼容代理"),
    ("C47", Group.AGENT, "待答工单队列"),
    # ---- H 组 签到 ----
    ("C48", Group.SIGN, "签到任务执行"),
    ("C49", Group.SIGN, "签到类型覆盖"),
    ("C50", Group.SIGN, "签到监测与通知"),
    ("C51", Group.SIGN, "签退"),
)

CAPABILITIES: dict[str, Capability] = {
    cid: Capability(cid, group, name) for cid, group, name in _RAW
}

#: 统一层自建、不依赖任何第三方项目的能力。
#: 依据 docs/02 §三 的统计：51 项里 35 项由现成项目提供，16 项自建。
SELF_BUILT: frozenset[str] = frozenset(
    {
        "C03",  # 重登兜底
        "C04",  # 多账号
        "C09",  # 任务点细化
        "C37",  # 结构化日志
        "C40",  # 暂停/恢复
        "C41",  # 重试
        "C42",  # 风控检测
        "C43",
        "C44",
        "C45",
        "C46",
        "C47",
        "C48",
        "C49",
        "C50",
        "C51",
    }
)


def get(capability_id: str) -> Capability | None:
    return CAPABILITIES.get(capability_id)


def all_ids() -> list[str]:
    return sorted(CAPABILITIES)


def by_group() -> dict[str, list[Capability]]:
    grouped: dict[str, list[Capability]] = {g: [] for g in GROUP_ORDER}
    for cap in CAPABILITIES.values():
        grouped.setdefault(cap.group, []).append(cap)
    return grouped


class CapabilityLevel:
    """Adapter 在 manifest 里声明的支持程度。"""

    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"

    ALL = (FULL, PARTIAL, NONE)

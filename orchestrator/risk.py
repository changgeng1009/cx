"""风控检测。

为什么需要它（docs/02 §4、docs/03 §9）：
第三方项目在遇到风控时的表现通常是"报个错就退出"或"卡住不动"，
无法与"网络超时""解析失败"区分。而这两类错误的正确处置**完全相反**：

- 解析失败 → 换 Adapter 重试，可能就好了
- 风控     → 绝对不能重试，换 Adapter 也没用（同一个账号），必须停手冷却

因此统一层必须自己识别风控，并把它映射为 `ErrorCategory.RISK_CONTROL`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 命中即判为风控的特征。分三类，便于命中后给人看原因。
SIGNATURES: tuple[str, ...] = (
    # 显式限流/拦截文案
    "操作过于频繁",
    "操作太频繁",
    "请稍后再试",
    "请稍候重试",
    "访问受限",
    "访问频率",
    "请求过于频繁",
    "拒绝访问",
    "异常操作",
    "账号异常",
    "风险提示",
    # 验证/安全挑战
    "安全验证",
    "验证码",
    "滑动验证",
    "请完成验证",
    "人机验证",
    # 平台侧封禁语义
    "已被限制",
    "封禁",
    "禁止访问",
)

#: HTTP 状态码层面的风控信号
RISK_STATUS_CODES: frozenset[int] = frozenset({403, 429, 503})

#: 这些文案虽然出现在风控场景，但单独出现不足以判定风控。
#: 例如"无权限"更可能是课程未选（permission），不该触发全局熔断。
AMBIGUOUS_SIGNATURES: frozenset[str] = frozenset({"403"})


@dataclass
class RiskHit:
    signature: str
    source: str  # "text" | "status"
    excerpt: str = ""
    context: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {"signature": self.signature, "source": self.source}
        if self.excerpt:
            payload["excerpt"] = self.excerpt
        return payload


@dataclass
class RiskDetector:
    """从 Adapter 的原始输出里识别风控。

    设计上刻意保守：宁可漏判（退化为普通失败），也不要误判成风控
    导致整个账号被无谓冷却。所以：
    1. 只有明确的频控/验证文案才命中；
    2. `403` 这类模糊信号需要配合上下文词才判定；
    3. 命中后在日志里留下 `excerpt`，让人能复核判定是否合理。
    """

    extra_signatures: tuple[str, ...] = ()
    excerpt_radius: int = 40
    hits: list[RiskHit] = field(default_factory=list)

    def _all_signatures(self) -> tuple[str, ...]:
        return SIGNATURES + tuple(self.extra_signatures)

    def detect(self, raw: str, status_code: int | None = None) -> RiskHit | None:
        text = raw or ""

        for signature in self._all_signatures():
            index = text.find(signature)
            if index >= 0:
                hit = RiskHit(
                    signature=signature,
                    source="text",
                    excerpt=self._excerpt(text, index, len(signature)),
                )
                self.hits.append(hit)
                return hit

        if status_code is not None and status_code in RISK_STATUS_CODES:
            # 403 单独出现时更可能是权限问题；只有配合频控/验证语义才算风控
            if status_code != 403 or any(
                word in text for word in ("频繁", "稍后", "验证", "限流", "拦截")
            ):
                hit = RiskHit(
                    signature=f"HTTP {status_code}",
                    source="status",
                    excerpt=text[:120],
                )
                self.hits.append(hit)
                return hit

        return None

    def _excerpt(self, text: str, index: int, length: int) -> str:
        start = max(0, index - self.excerpt_radius)
        end = min(len(text), index + length + self.excerpt_radius)
        snippet = text[start:end].replace("\n", " ").strip()
        return f"...{snippet}..." if start > 0 else snippet

    @staticmethod
    def looks_like_login_page(raw: str) -> bool:
        """判定"被踢回登录页"（会话失效），这是 auth 而非风控。"""
        text = raw or ""
        markers = ("passport2.chaoxing.com", "fanyalogin", "请输入手机号", "登录超时")
        return any(m in text for m in markers)


#: 便于按字面量快速筛查（不含正则，纯子串匹配，零依赖且可预测）
def contains_risk_signature(raw: str) -> bool:
    return any(s in (raw or "") for s in SIGNATURES)


_ = re  # 保留 stdlib 引用，后续如需正则特征直接在此扩展

"""Cookie 的规范化存储与格式导出。

## 设计原则

**一种内部格式，多种导出格式。** 上游各项目对 cookie 的期望不一样：
`Samueli924/chaoxing` 支持 cookie 登录（PR #525），`Xuexitong-mcp` 用自己的
`session_cookies.json`，而有些工具要 Netscape 的 `cookies.txt`。

所以内部统一存成 `CookieJar`，再按需导出——**而不是让每个 Adapter 各存一份**。
将来发现某个上游要什么格式，加一个 exporter 即可，不需要迁移数据。

## 安全

Cookie 就是凭据。因此：
- 落盘位置在 `accounts/{id}/`（已 gitignore、权限收紧）
- 默认输出一律**掩码**，要看原值必须显式要求
- 永不进日志、永不进 Envelope（由 `redact.py` 统一兜底）

## 取 cookie 的方式

**只能通过 CDP 取**，理由见 `cdp.py`：新版浏览器启用了 App-Bound
Encryption，进程外直读 profile 数据库已经解不出明文。让浏览器自己交出来
是最短路径，也顺带避免了去做任何"破解加密"的事。
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import now_iso

#: 学习通相关域名后缀。默认只导出这些，避免把其他站点的 cookie 一起落盘。
CHAOXING_DOMAIN_SUFFIXES: tuple[str, ...] = (
    "chaoxing.com",
    "xuexitong.com",
    "chaoxing.net",
    # 资料下载走 cldisk 的 CDN，需要它的域参与 Referer 校验
    "cldisk.com",
)

#: 会话 cookie 在 CDP 里的 expires 标记
SESSION_EXPIRES = -1


@dataclass
class Cookie:
    name: str
    value: str
    domain: str = ""
    path: str = "/"
    expires: float = SESSION_EXPIRES
    secure: bool = False
    http_only: bool = False
    same_site: str = ""

    # ------------------------------------------------------------------
    @property
    def is_session(self) -> bool:
        return self.expires is None or self.expires < 0

    def is_expired(self, at: float | None = None) -> bool:
        if self.is_session:
            return False
        return self.expires <= (at if at is not None else time.time())

    def masked_value(self) -> str:
        """给终端展示用的掩码。短值整体打码，长值留前 4 位便于比对。"""
        if not self.value:
            return "(空)"
        if len(self.value) <= 8:
            return "*" * len(self.value)
        return f"{self.value[:4]}…({len(self.value)}字符)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "expires": self.expires,
            "secure": self.secure,
            "http_only": self.http_only,
            "same_site": self.same_site,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """不含原值的版本，可安全打印。"""
        payload = self.to_dict()
        payload["value"] = self.masked_value()
        payload["is_session"] = self.is_session
        payload["is_expired"] = self.is_expired()
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Cookie":
        expires = raw.get("expires", SESSION_EXPIRES)
        try:
            expires_value = float(expires) if expires is not None else SESSION_EXPIRES
        except (TypeError, ValueError):
            expires_value = SESSION_EXPIRES
        return cls(
            name=str(raw.get("name", "")),
            value=str(raw.get("value", "")),
            domain=str(raw.get("domain", "")),
            path=str(raw.get("path", "/")) or "/",
            expires=expires_value,
            secure=bool(raw.get("secure", False)),
            http_only=bool(raw.get("httpOnly", raw.get("http_only", False))),
            same_site=str(raw.get("sameSite", raw.get("same_site", "")) or ""),
        )

    def to_netscape_line(self) -> str:
        """Netscape / curl 的 `cookies.txt` 行格式。

        HttpOnly 没有独立字段，业界惯例是给 domain 加 `#HttpOnly_` 前缀
        （curl / wget / yt-dlp 都认这个约定）。
        """
        domain = self.domain or ""
        prefix = "#HttpOnly_" if self.http_only else ""
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if self.secure else "FALSE"
        expires = 0 if self.is_session else int(self.expires)
        return (
            f"{prefix}{domain}\t{include_subdomains}\t{self.path}\t{secure}\t"
            f"{expires}\t{self.name}\t{self.value}"
        )


def domain_matches(host: str, suffix: str) -> bool:
    host = host.lstrip(".").lower()
    suffix = suffix.lstrip(".").lower()
    return host == suffix or host.endswith("." + suffix)


@dataclass
class CookieJar:
    cookies: list[Cookie] = field(default_factory=list)
    account_id: str = ""
    source: str = ""
    captured_at: str = field(default_factory=now_iso)

    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.cookies)

    def __iter__(self):
        return iter(self.cookies)

    def add(self, cookie: Cookie) -> None:
        """加入并按 (domain, path, name) 去重，后写入的覆盖先前的。"""
        key = (cookie.domain.lstrip(".").lower(), cookie.path, cookie.name)
        for index, existing in enumerate(self.cookies):
            if (
                existing.domain.lstrip(".").lower(),
                existing.path,
                existing.name,
            ) == key:
                self.cookies[index] = cookie
                return
        self.cookies.append(cookie)

    def merge(self, other: "CookieJar") -> "CookieJar":
        for cookie in other.cookies:
            self.add(cookie)
        return self

    # ------------------------------------------------------------------
    # 过滤
    # ------------------------------------------------------------------
    def filter_domains(self, suffixes: Sequence[str] | None = None) -> "CookieJar":
        """按域名后缀过滤。

        **域名为空的 cookie 一律保留。** 手动从 DevTools 复制的 `Cookie:`
        请求头里根本没有域名信息，若按"域名不匹配"丢弃，就会把这条路
        （最省事、零依赖的那条）直接废掉。
        """
        patterns = list(suffixes) if suffixes else list(CHAOXING_DOMAIN_SUFFIXES)
        if not patterns:
            return CookieJar(list(self.cookies), self.account_id, self.source)
        kept = [
            c
            for c in self.cookies
            if not c.domain
            or any(domain_matches(c.domain, suffix) for suffix in patterns)
        ]
        return CookieJar(kept, self.account_id, self.source)

    def stamp_domain(self, domain: str) -> "CookieJar":
        """给没有域名的 cookie 补上域名，返回**新的** jar（不改动原对象）。

        需要它是因为 Netscape 格式必须有域名，而 header 串没有。
        默认补 `.chaoxing.com`（带前导点，表示包含子域）。
        """
        if not domain:
            return CookieJar(list(self.cookies), self.account_id, self.source)
        return CookieJar(
            [c if c.domain else replace(c, domain=domain) for c in self.cookies],
            self.account_id,
            self.source,
        )

    def without_expired(self, at: float | None = None) -> "CookieJar":
        return CookieJar(
            [c for c in self.cookies if not c.is_expired(at)],
            self.account_id,
            self.source,
        )

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def to_dict_list(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.cookies]

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "account_id": self.account_id,
                "source": self.source,
                "captured_at": self.captured_at,
                "count": len(self.cookies),
                "cookies": self.to_dict_list(),
            },
            ensure_ascii=False,
            indent=indent,
        )

    def to_netscape(self) -> str:
        lines = [
            "# Netscape HTTP Cookie File",
            "# 由学习通自动化能力平台生成。请勿提交到版本库。",
            f"# source: {self.source}   captured_at: {self.captured_at}",
            "",
        ]
        lines.extend(c.to_netscape_line() for c in self.cookies)
        return "\n".join(lines) + "\n"

    def to_header(self, domains: Sequence[str] | None = None) -> str:
        """导出 `Cookie:` 请求头的值。

        最可能被上游"cookie 登录"入口吃掉的形式——很多实现就是直接把这串
        塞进 `headers={"Cookie": ...}`。M2 接入时需按 A1 的实际读取方式确认。
        """
        jar = self.filter_domains(domains) if domains else self
        return "; ".join(f"{c.name}={c.value}" for c in jar.cookies if c.name)

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CookieJar":
        jar = cls(
            account_id=str(raw.get("account_id", "")),
            source=str(raw.get("source", "")),
            captured_at=str(raw.get("captured_at", now_iso())),
        )
        for item in raw.get("cookies") or []:
            if isinstance(item, dict):
                jar.add(Cookie.from_dict(item))
        return jar

    @classmethod
    def from_json(cls, text: str) -> "CookieJar":
        payload = json.loads(text)
        if isinstance(payload, dict):
            return cls.from_dict(payload)
        if isinstance(payload, list):
            jar = cls()
            for item in payload:
                if isinstance(item, dict):
                    jar.add(Cookie.from_dict(item))
            return jar
        raise ValueError("JSON cookie 需要是对象或数组")

    @classmethod
    def from_netscape(cls, text: str) -> "CookieJar":
        jar = cls(source="netscape-text")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            http_only = line.startswith("#HttpOnly_")
            if http_only:
                line = line[len("#HttpOnly_") :]
            parts = line.split("\t")
            if len(parts) < 7:
                # 有些文件用空格分隔
                parts = line.split()
                if len(parts) < 7:
                    continue
            domain, _flag, path, secure, expires, name = parts[:6]
            value = "\t".join(parts[6:])
            try:
                expires_value = float(expires)
            except ValueError:
                expires_value = 0.0
            jar.add(
                Cookie(
                    name=name,
                    value=value,
                    domain=domain,
                    path=path or "/",
                    expires=expires_value if expires_value > 0 else SESSION_EXPIRES,
                    secure=secure.upper() == "TRUE",
                    http_only=http_only,
                )
            )
        return jar

    @classmethod
    def from_header(cls, text: str, domain: str = "") -> "CookieJar":
        """解析 `name=value; name2=value2` 形式的字符串。

        domain 未知时留空——很多上游的 cookie 登录并不校验 domain，
        因为请求头里本来就不带 domain。
        """
        jar = cls(source="header-string")
        for chunk in text.replace("\n", ";").split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            name, _, value = chunk.partition("=")
            name = name.strip()
            if not name:
                continue
            jar.add(Cookie(name=name, value=value.strip(), domain=domain))
        return jar

    @classmethod
    def from_cdp(cls, payload: Iterable[dict[str, Any]]) -> "CookieJar":
        """把 `Network.getAllCookies` 的返回转成 CookieJar。"""
        jar = cls(source="cdp")
        for item in payload:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            jar.add(Cookie.from_dict(item))
        return jar

    # ------------------------------------------------------------------
    def diagnose(self) -> dict[str, Any]:
        """体检报告，用于回答"为什么登录不上"。"""
        by_domain: dict[str, int] = {}
        for cookie in self.cookies:
            by_domain[cookie.domain] = by_domain.get(cookie.domain, 0) + 1
        expired = [c for c in self.cookies if c.is_expired()]
        session = [c for c in self.cookies if c.is_session]
        chaoxing = self.filter_domains()
        return {
            "total": len(self.cookies),
            "chaoxing_related": len(chaoxing),
            "session_cookies": len(session),
            "expired": len(expired),
            "expired_names": sorted(c.name for c in expired)[:20],
            "domains": dict(sorted(by_domain.items(), key=lambda kv: -kv[1])),
            "source": self.source,
            "captured_at": self.captured_at,
        }


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------


@dataclass
class CookieStore:
    """`accounts/{id}/` 下的 cookie 持久化。

    同时写两份：
    - `cookies.json` —— 内部规范格式（唯一事实来源）
    - `cookies.txt`  —— Netscape 格式（给认这个格式的上游工具）
    另外 `cookies.header` 保存纯 `Cookie:` 头的值，方便直接粘贴使用。
    """

    root: Path
    domains: tuple[str, ...] = CHAOXING_DOMAIN_SUFFIXES

    def workdir(self, account_id: str) -> Path:
        return self.root / account_id

    def json_path(self, account_id: str) -> Path:
        return self.workdir(account_id) / "cookies.json"

    def netscape_path(self, account_id: str) -> Path:
        return self.workdir(account_id) / "cookies.txt"

    def header_path(self, account_id: str) -> Path:
        return self.workdir(account_id) / "cookies.header"

    def has(self, account_id: str) -> bool:
        return self.json_path(account_id).is_file()

    def save(
        self,
        account_id: str,
        jar: CookieJar,
        only_chaoxing: bool = True,
        default_domain: str = ".chaoxing.com",
        domain_filter: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """落盘。

        `default_domain`：给缺域名的 cookie 补的域名。
        `domain_filter`：按这些后缀过滤。**显式传入时以它为准** ——
        因为调用方说"这批 cookie 属于这个域名"，就不该再拿默认的
        学习通后缀把它们筛掉。
        """
        workdir = self.workdir(account_id)
        workdir.mkdir(parents=True, exist_ok=True)

        # 先补域名再过滤：手动粘贴的 header 串没有域名，
        # 补上之后 Netscape 导出才是合法的，filter_domains 也才有意义。
        stamped = jar.stamp_domain(default_domain)
        effective = list(domain_filter) if domain_filter is not None else list(self.domains)
        target = stamped.filter_domains(effective) if only_chaoxing else stamped
        target.account_id = account_id

        json_path = self.json_path(account_id)
        json_path.write_text(target.to_json(), encoding="utf-8")
        _restrict(json_path)

        netscape_path = self.netscape_path(account_id)
        netscape_path.write_text(target.to_netscape(), encoding="utf-8")
        _restrict(netscape_path)

        header_path = self.header_path(account_id)
        header_path.write_text(target.to_header(), encoding="utf-8")
        _restrict(header_path)

        return {
            "account_id": account_id,
            "kept": len(target),
            "dropped": len(jar) - len(target),
            "files": {
                "json": str(json_path),
                "netscape": str(netscape_path),
                "header": str(header_path),
            },
            "diagnose": target.diagnose(),
        }

    def load(self, account_id: str) -> CookieJar | None:
        path = self.json_path(account_id)
        if not path.is_file():
            return None
        return CookieJar.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def clear(self, account_id: str) -> list[str]:
        removed: list[str] = []
        for path in (
            self.json_path(account_id),
            self.netscape_path(account_id),
            self.header_path(account_id),
        ):
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        return removed

    def meta(self, account_id: str) -> dict[str, Any]:
        path = self.json_path(account_id)
        if not path.is_file():
            return {"present": False, "account_id": account_id}
        jar = CookieJar.from_dict(json.loads(path.read_text(encoding="utf-8")))
        report = jar.diagnose()
        report.update(
            {
                "present": True,
                "account_id": account_id,
                "json_path": str(path),
                "netscape_path": str(self.netscape_path(account_id)),
                "header_path": str(self.header_path(account_id)),
            }
        )
        return report


def _restrict(path: Path) -> None:
    """尽力收紧权限（Windows 上 chmod 语义有限，故为 best-effort）。"""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

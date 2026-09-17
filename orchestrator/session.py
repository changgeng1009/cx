"""账号、凭据与会话管理（多账号）。

多账号为什么在统一层实现（docs/02 §4）：
扫遍全部候选项目，**没有任何 CLI/库项目原生支持多账号**。所以这里不
是"包装"能力，而是"新增"能力。做法是每个账号一份独立工作目录 +
独立凭据 + 独立子进程——这反而比在单进程里切账号更安全，因为账号 A
的风控状态、cookie、断点天然不会污染账号 B。

凭据纪律（docs/03 §8）：凭据只在这一个模块里流转，**绝不进日志、
绝不进 Envelope、绝不出现在 repr 里**。
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import AccountContext, now_iso
from .redact import mask_phone

DEFAULT_ACCOUNT_ID = "acc_01"

ACCOUNTS_DIRNAME = "accounts"


@dataclass
class Account:
    account_id: str
    root: Path
    label: str = ""
    phone_masked: str = ""
    enabled: bool = True
    added_at: str = field(default_factory=now_iso)

    @property
    def workdir(self) -> Path:
        return self.root / self.account_id

    @property
    def meta_path(self) -> Path:
        return self.workdir / "meta.json"

    @property
    def credentials_path(self) -> Path:
        return self.workdir / "credentials.json"

    @property
    def cookies_path(self) -> Path:
        return self.workdir / "cookies.txt"

    @property
    def config_path(self) -> Path:
        return self.workdir / "config.ini"

    @property
    def state_dir(self) -> Path:
        return self.workdir / "state"

    def ensure_dirs(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def to_dict(self, credentials_present: bool = False, cookies_present: bool = False) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "label": self.label,
            "phone_masked": self.phone_masked,
            "enabled": self.enabled,
            "added_at": self.added_at,
            "workdir": str(self.workdir),
            "has_credentials": credentials_present,
            "has_cookies": cookies_present,
        }


class AccountManager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def ensure(self, account_id: str = DEFAULT_ACCOUNT_ID) -> Account:
        """取得账号；不存在则创建骨架（不写任何凭据）。"""
        workdir = self.root / account_id
        meta_path = workdir / "meta.json"
        if meta_path.is_file():
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            account = Account(
                account_id=account_id,
                root=self.root,
                label=str(raw.get("label", "")),
                phone_masked=str(raw.get("phone_masked", "")),
                enabled=bool(raw.get("enabled", True)),
                added_at=str(raw.get("added_at", now_iso())),
            )
        else:
            account = Account(account_id=account_id, root=self.root)
            account.ensure_dirs()
            self._write_meta(account)
        account.ensure_dirs()
        return account

    def _write_meta(self, account: Account) -> None:
        account.ensure_dirs()
        payload = {
            "account_id": account.account_id,
            "label": account.label,
            "phone_masked": account.phone_masked,
            "enabled": account.enabled,
            "added_at": account.added_at,
        }
        account.meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def create(
        self, account_id: str = DEFAULT_ACCOUNT_ID, label: str = "", phone: str = ""
    ) -> Account:
        account = self.ensure(account_id)
        if label:
            account.label = label
        if phone:
            account.phone_masked = mask_phone(phone)
        self._write_meta(account)
        return account

    def get(self, account_id: str) -> Account:
        return self.ensure(account_id)

    def list_accounts(self) -> list[Account]:
        found: list[Account] = []
        for child in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if child.is_dir() and (child / "meta.json").is_file():
                found.append(self.ensure(child.name))
        return found

    def default_account_id(self) -> str:
        accounts = self.list_accounts()
        if accounts:
            return accounts[0].account_id
        return DEFAULT_ACCOUNT_ID

    # ------------------------------------------------------------------
    # 凭据
    # ------------------------------------------------------------------
    def write_credentials(
        self, account_id: str, phone: str, password: str
    ) -> Account:
        """写入凭据。这是唯一会落盘明文密文的入口。"""
        account = self.ensure(account_id)
        payload = {"phone": phone, "password": password, "updated_at": now_iso()}
        path = account.credentials_path
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _restrict(path)
        if not account.phone_masked:
            account.phone_masked = mask_phone(phone)
        self._write_meta(account)
        return account

    def load_credentials(self, account_id: str) -> dict[str, str]:
        account = self.ensure(account_id)
        if not account.credentials_path.is_file():
            return {}
        raw = json.loads(account.credentials_path.read_text(encoding="utf-8"))
        return {
            "phone": str(raw.get("phone", "")),
            "password": str(raw.get("password", "")),
        }

    def has_credentials(self, account_id: str) -> bool:
        account = self.ensure(account_id)
        return account.credentials_path.is_file()

    def write_cookies(self, account_id: str, cookie_text: str) -> Path:
        account = self.ensure(account_id)
        account.cookies_path.write_text(cookie_text, encoding="utf-8")
        _restrict(account.cookies_path)
        return account.cookies_path

    def has_cookies(self, account_id: str) -> bool:
        return self.ensure(account_id).cookies_path.is_file()

    def write_adapter_config(self, account_id: str, content: str) -> Path:
        """为上游项目生成配置文件。

        这是"不修改第三方源码"（R2）的落点：配置写到**账号工作区**，
        再用 `-c` 参数把上游指过来，绝不往 `upstreams/` 里写文件。
        """
        account = self.ensure(account_id)
        path = account.config_path
        path.write_text(content, encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    def session_status(self, account_id: str) -> dict[str, Any]:
        account = self.ensure(account_id)
        return {
            "account_id": account.account_id,
            "label": account.label,
            "phone_masked": account.phone_masked,
            "enabled": account.enabled,
            "has_credentials": account.credentials_path.is_file(),
            "has_cookies": account.cookies_path.is_file(),
            "ready": account.credentials_path.is_file() or account.cookies_path.is_file(),
            "workdir": str(account.workdir),
        }

    def context(self, account_id: str) -> AccountContext:
        """构造 Adapter 用的上下文。凭据在此装载，且 `repr` 已屏蔽。"""
        account = self.ensure(account_id)
        return AccountContext(
            account_id=account.account_id,
            workdir=str(account.workdir),
            phone_masked=account.phone_masked,
            credentials=self.load_credentials(account.account_id),
        )

    def summary(self) -> list[dict[str, Any]]:
        return [
            self.session_status(a.account_id) for a in self.list_accounts()
        ]


def _restrict(path: Path) -> None:
    """尽力收紧文件权限。

    Windows 的 chmod 语义有限（只影响只读位），所以这是 best-effort；
    真正的保护靠 .gitignore + 不落日志。
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

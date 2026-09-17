"""跨进程控制通道（C40 的基础设施）。

`pause` / `resume` / `stop` 三个命令由**另一个进程**发起，而被暂停的
执行跑在原进程里。所以要有一个跨进程信号通道。这里同样用文件而不是
socket/信号量：简单、可靠、Windows 上行为一致、且状态可被人工检查。

协作式暂停（docs/02 §4、docs/03 §4.1）：
执行方在**任务点边界**检查本通道；一旦发现 pause 标志就优雅停下、
记录断点。恢复时重新进入课程即可 —— 因为学习通自己会跳过已完成的
任务点，所以不需要上游支持进程内暂停。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import now_iso


@dataclass
class ControlSignal:
    request_id: str
    pause: bool = False
    cancel: bool = False
    retry: bool = False
    note: str = ""
    updated_at: str = field(default_factory=now_iso)

    @property
    def any_set(self) -> bool:
        return self.pause or self.cancel or self.retry

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "pause": self.pause,
            "cancel": self.cancel,
            "retry": self.retry,
            "note": self.note,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ControlSignal":
        return cls(
            request_id=str(raw.get("request_id", "")),
            pause=bool(raw.get("pause", False)),
            cancel=bool(raw.get("cancel", False)),
            retry=bool(raw.get("retry", False)),
            note=str(raw.get("note", "")),
            updated_at=str(raw.get("updated_at", now_iso())),
        )


class ControlChannel:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, request_id: str) -> Path:
        return self.root / f"{request_id}.json"

    def read(self, request_id: str) -> ControlSignal | None:
        path = self._path(request_id)
        if not path.is_file():
            return None
        try:
            return ControlSignal.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return None

    def write(self, signal: ControlSignal) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        signal.updated_at = now_iso()
        target = self._path(signal.request_id)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(signal.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(target)
        return target

    def set_flag(self, request_id: str, flag: str, note: str = "") -> ControlSignal:
        signal = self.read(request_id) or ControlSignal(request_id=request_id)
        setattr(signal, flag, True)
        if note:
            signal.note = note
        self.write(signal)
        return signal

    def request_pause(self, request_id: str, note: str = "") -> ControlSignal:
        return self.set_flag(request_id, "pause", note)

    def request_cancel(self, request_id: str, note: str = "") -> ControlSignal:
        return self.set_flag(request_id, "cancel", note)

    def clear(self, request_id: str) -> None:
        path = self._path(request_id)
        if path.is_file():
            path.unlink()

    def clear_flag(self, request_id: str, flag: str) -> ControlSignal | None:
        signal = self.read(request_id)
        if signal is None:
            return None
        setattr(signal, flag, False)
        self.write(signal)
        return signal

    def list_active(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            signal = self.read(path.stem)
            if signal is not None and signal.any_set:
                items.append(signal.to_dict())
        return items

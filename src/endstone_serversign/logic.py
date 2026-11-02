"""Pure logic for ServerSign (no Endstone dependency): storage and commands."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

EXECUTOR_PLAYER = "player"
EXECUTOR_CONSOLE = "console"
VALID_EXECUTORS = (EXECUTOR_PLAYER, EXECUTOR_CONSOLE)


@dataclass
class SignEntry:
    commands: list[str]
    executor: str = EXECUTOR_PLAYER
    owner: str = ""


def make_key(dimension: str, x: int, y: int, z: int) -> str:
    return f"{dimension}:{x}:{y}:{z}"


def clean_commands(raw: Iterable[object], limit: int = 3) -> tuple[list[str], int]:
    """Cleans entered commands.

    - removes initial '/' and whitespace;
    - ignores empty lines;
    - rejects commands containing control characters (e.g. line breaks),
      which would allow chaining multiple commands in a single field.

    Returns (valid commands, number of rejected commands).
    """
    valid: list[str] = []
    rejected = 0
    for item in raw:
        cmd = str(item or "").strip()
        if cmd.startswith("/"):
            cmd = cmd.lstrip("/").strip()
        if not cmd:
            continue
        if any(ord(c) < 32 or ord(c) == 127 for c in cmd):
            rejected += 1
            continue
        valid.append(cmd)
    return valid[:limit], rejected


def render_command(template: str, player_name: str) -> str:
    """Replaces {player}; Bedrock usernames can contain spaces -> wrap them in quotes."""
    name = player_name.replace('"', "")
    if " " in name:
        name = f'"{name}"'
    return template.replace("{player}", name)


class SignStore:
    """Registry of ServerSign signs, persisted in JSON (atomic write)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._entries: dict[str, SignEntry] = {}

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str) -> SignEntry | None:
        return self._entries.get(key)

    def set(self, key: str, entry: SignEntry) -> None:
        self._entries[key] = entry

    def remove(self, key: str) -> bool:
        return self._entries.pop(key, None) is not None

    def items(self) -> list[tuple[str, SignEntry]]:
        return list(self._entries.items())

    def load(self) -> str | None:
        """Loads the file. Returns a warning message if the file was unreadable."""
        self._entries = {}
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("root must be a JSON object")
        except (OSError, ValueError) as exc:
            backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return f"{self.path.name} unreadable ({exc}); copy kept as {backup.name}, empty registry."

        for key, value in raw.items():
            if not isinstance(value, dict) or not isinstance(value.get("commands"), list):
                continue
            commands, _ = clean_commands([c for c in value["commands"] if isinstance(c, str)], limit=50)
            executor = value.get("executor")
            if executor not in VALID_EXECUTORS:
                executor = EXECUTOR_PLAYER  # never implicitly elevate to console
            if commands:
                self._entries[str(key)] = SignEntry(commands, executor, str(value.get("owner", "")))
        return None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {"commands": e.commands, "executor": e.executor, "owner": e.owner}
            for key, e in self._entries.items()
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".signs-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
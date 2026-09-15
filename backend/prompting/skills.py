from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from backend.core.errors import ValidationError
from backend.persistence.files import SafeStorage, StorageError
from backend.utils import sha256_bytes

MAX_SKILLS = 24
MAX_SKILL_BYTES = 16 * 1024
MAX_TOTAL_SKILL_BYTES = 48 * 1024
SKILL_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_SKILL_NAME = re.compile(SKILL_NAME_PATTERN)


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    name: str
    instructions: str
    size_bytes: int
    source: Literal["bundled", "local"]
    revision: str


class SkillRegistry:
    """Load instruction-only recipes; never register tools or execute skill content."""

    def __init__(self, defaults_root: Path, storage: SafeStorage | None = None) -> None:
        self._defaults_root = defaults_root
        self._storage = storage

    def snapshot(self) -> tuple[SkillDefinition, ...]:
        skills = self._read_directory(self._defaults_root, "bundled")
        if self._storage is not None:
            skills.update(self._read_directory(self.local_root, "local"))
        return self.validate_snapshot(skills)

    @property
    def local_root(self) -> Path:
        if self._storage is None:
            raise ValidationError("Local skill storage is not configured.")
        try:
            return self._storage.resolve_path(self._storage.settings.data_dir, "skills")
        except (OSError, StorageError) as exc:
            raise ValidationError(f"Cannot load local skills: {exc}") from exc

    @staticmethod
    def validate_snapshot(skills: dict[str, SkillDefinition]) -> tuple[SkillDefinition, ...]:
        if len(skills) > MAX_SKILLS:
            raise ValidationError(f"At most {MAX_SKILLS} skills can be loaded per turn.")
        if sum(skill.size_bytes for skill in skills.values()) > MAX_TOTAL_SKILL_BYTES:
            raise ValidationError(
                f"Combined skill instructions exceed {MAX_TOTAL_SKILL_BYTES} UTF-8 bytes."
            )
        return tuple(skills[name] for name in sorted(skills))

    @staticmethod
    def validate_name(name: str) -> None:
        if len(name) > 64 or not _SKILL_NAME.fullmatch(name):
            raise ValidationError(
                f"Invalid skill filename '{name}.md': use a lowercase kebab-case name "
                "of at most 64 characters followed by .md."
            )

    @classmethod
    def parse(
        cls, name: str, content: bytes, source: Literal["bundled", "local"],
    ) -> SkillDefinition:
        cls.validate_name(name)
        if len(content) > MAX_SKILL_BYTES:
            raise ValidationError(f"Skill '{name}.md' exceeds {MAX_SKILL_BYTES} UTF-8 bytes.")
        try:
            instructions = content.decode("utf-8-sig")
        except UnicodeError as exc:
            raise ValidationError(f"Cannot read skill instructions for '{name}.md': {exc}") from exc
        if not instructions.strip():
            raise ValidationError(f"Skill '{name}.md' must contain non-empty Markdown instructions.")
        return SkillDefinition(
            name, instructions, len(content), source,
            sha256_bytes(source.encode("ascii") + b"\0" + content),
        )

    def _read_directory(
        self, root: Path, source_kind: Literal["bundled", "local"],
    ) -> dict[str, SkillDefinition]:
        current_path = root
        try:
            if not root.exists():
                return {}
            paths = sorted(
                (path for path in root.iterdir() if path.suffix.lower() == ".md"),
                key=lambda path: path.name,
            )
            if len(paths) > MAX_SKILLS:
                raise ValidationError(f"Skill directory '{root}' exceeds {MAX_SKILLS} Markdown files.")
            skills: dict[str, SkillDefinition] = {}
            for path in paths:
                current_path = path
                name = path.stem
                if path.suffix != ".md":
                    raise ValidationError(
                        f"Invalid skill filename '{path.name}': use the lowercase .md extension."
                    )
                if self._storage is not None:
                    resolved = self._storage.resolve_path(root, path.name)
                else:
                    resolved = path.resolve()
                    if not resolved.is_relative_to(root.resolve()):
                        raise ValidationError(f"Skill '{path}' escapes its instruction directory.")
                with resolved.open("rb") as source:
                    content = source.read(MAX_SKILL_BYTES + 1)
                skills[name] = self.parse(name, content, source_kind)
            return skills
        except (OSError, UnicodeError, StorageError) as exc:
            raise ValidationError(f"Cannot read skill instructions at '{current_path}': {exc}") from exc

from __future__ import annotations

import threading

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.persistence.files import SafeStorage, StorageError
from backend.prompting.skills import SkillDefinition, SkillRegistry


class SkillService:
    def __init__(self, registry: SkillRegistry, storage: SafeStorage) -> None:
        self._registry = registry
        self._storage = storage
        self._lock = threading.Lock()

    def list(self) -> tuple[SkillDefinition, ...]:
        return self._registry.snapshot()

    def get(self, name: str) -> SkillDefinition:
        self._registry.validate_name(name)
        skill = next((skill for skill in self.list() if skill.name == name), None)
        if skill is None:
            raise NotFoundError(f"Skill '{name}' was not found.")
        return skill

    def save(self, name: str, content: str, expected_revision: str | None) -> SkillDefinition:
        try:
            data = content.encode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("Skill instructions must be valid UTF-8 text.") from exc
        candidate = self._registry.parse(name, data, "local")
        with self._lock:
            skills = {skill.name: skill for skill in self.list()}
            current = skills.get(name)
            if (current.revision if current else None) != expected_revision:
                raise ConflictError(
                    "This skill changed since it was opened. Your draft was not saved. "
                    "Reload the skill and reconcile your changes before saving."
                )
            skills[name] = candidate
            self._registry.validate_snapshot(skills)
            root = self._registry.local_root
            try:
                target = self._storage.resolve_path(root, f"{name}.md")
                if target != root / f"{name}.md":
                    raise ValidationError("Editing a symbolic-link skill file is not supported.")
                self._storage.write_bytes_atomic(root, f"{name}.md", data)
            except (OSError, StorageError) as exc:
                raise ValidationError(f"Could not save skill '{name}': {exc}") from exc
        return candidate

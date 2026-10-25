from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.agents.blueprint import AgentBlueprint
from backend.app import create_app
from backend.conversations.turns import deep_work_blueprint, research_blueprint
from backend.core.errors import ValidationError
from backend.persistence.files import SafeStorage
from backend.prompting.registry import PromptRegistry
from backend.prompting.skills import (
    MAX_SKILL_BYTES,
    MAX_SKILLS,
    MAX_TOTAL_SKILL_BYTES,
    SkillRegistry,
)
from backend.research.sources import WebSourceUnavailable
from backend.tests.test_api import configure_provider, wait_for_run

BUNDLED_SKILLS = [
    "experiment-planning", "literature-review", "problem-framing",
    "results-interpretation", "web-synthesis",
]


def _write_skill(settings, name: str, content: str | bytes) -> Path:
    data = content.encode("utf-8") if isinstance(content, str) else content
    return SafeStorage(settings).write_bytes(
        settings.data_dir, f"skills/{name}", data,
    ).absolute_path


def _registry(settings, *, empty_defaults: bool = False) -> SkillRegistry:
    defaults = (
        settings.data_dir / "empty-defaults"
        if empty_defaults else Path(__file__).parents[1] / "prompting" / "defaults" / "skills"
    )
    return SkillRegistry(defaults, SafeStorage(settings))


def test_local_skills_override_defaults_and_reload_in_stable_order(test_settings):
    registry = _registry(test_settings)
    assert [skill.name for skill in registry.snapshot()] == BUNDLED_SKILLS
    _write_skill(test_settings, "web-synthesis.md", "# My web recipe\n\nCompare the sources.")
    first_path = _write_skill(test_settings, "a-recipe.md", b"\xef\xbb\xbf# A recipe\n\nUse local notes.")
    _write_skill(test_settings, "ignored.py", "raise RuntimeError('never execute')")
    _write_skill(test_settings, "nested/not-loaded.md", "Not a top-level skill.")

    first = registry.snapshot()
    assert [skill.name for skill in first] == ["a-recipe", *BUNDLED_SKILLS]
    assert first[0].instructions.startswith("# A recipe")
    assert first[-1].instructions == "# My web recipe\n\nCompare the sources."
    _write_skill(test_settings, "web-synthesis.md", "# Changed\n\nReturn a comparison table.")
    first_path.unlink()
    second = registry.snapshot()
    assert [skill.name for skill in second] == BUNDLED_SKILLS
    assert second[-1].instructions.startswith("# Changed")
    assert first[-1].instructions.startswith("# My web recipe")


@pytest.mark.parametrize(
    ("name", "content", "error"),
    [
        ("empty.md", " \n\t", "non-empty"),
        ("bad-utf8.md", b"\xff", "Cannot read skill instructions"),
        ("Bad_Name.md", "# Invalid name", "Invalid skill filename"),
        ("bad--name.md", "# Invalid name", "Invalid skill filename"),
        ("x" * 65 + ".md", "# Too long", "Invalid skill filename"),
        ("upper.MD", "# Wrong extension", "Invalid skill filename"),
        ("large.md", b"x" * (MAX_SKILL_BYTES + 1), "exceeds"),
    ],
    ids=["empty", "utf8", "name", "hyphens", "long-name", "extension", "size"],
)
def test_invalid_skills_fail_explicitly(test_settings, name, content, error):
    _write_skill(test_settings, name, content)
    with pytest.raises(ValidationError, match=error):
        _registry(test_settings).snapshot()


def test_skill_size_limits_use_utf8_bytes_and_accept_exact_boundary(test_settings):
    registry = _registry(test_settings, empty_defaults=True)
    content = "\u00e9" * (MAX_SKILL_BYTES // 2)
    _write_skill(test_settings, "boundary.md", content)
    assert registry.snapshot()[0].size_bytes == MAX_SKILL_BYTES
    _write_skill(test_settings, "boundary.md", content + "x")
    with pytest.raises(ValidationError, match="exceeds"):
        registry.snapshot()


def test_total_skill_budget_and_count_are_enforced(test_settings):
    registry = _registry(test_settings, empty_defaults=True)
    for index in range(MAX_TOTAL_SKILL_BYTES // MAX_SKILL_BYTES):
        _write_skill(test_settings, f"large-{index}.md", "x" * MAX_SKILL_BYTES)
    assert sum(skill.size_bytes for skill in registry.snapshot()) == MAX_TOTAL_SKILL_BYTES
    _write_skill(test_settings, "extra.md", "x")
    with pytest.raises(ValidationError, match="Combined skill instructions"):
        registry.snapshot()
    for index in range(MAX_SKILLS + 1):
        _write_skill(test_settings, f"small-{index}.md", "x")
    with pytest.raises(ValidationError, match="Markdown files"):
        registry.snapshot()


def test_local_and_packaged_skills_share_one_budget(test_settings):
    for index in range(MAX_SKILLS):
        _write_skill(test_settings, f"local-{index}.md", "Use existing tools.")
    with pytest.raises(ValidationError, match="At most"):
        _registry(test_settings).snapshot()


def test_skill_directory_must_be_readable_directory(test_settings):
    SafeStorage(test_settings).write_bytes(test_settings.data_dir, "skills", b"not a directory")
    with pytest.raises(ValidationError, match="Cannot read skill instructions"):
        _registry(test_settings).snapshot()


def test_skills_cannot_follow_links_outside_their_directory(test_settings):
    target = SafeStorage(test_settings).write_bytes(
        test_settings.data_dir, "outside.md", b"Not installed as a skill.",
    ).absolute_path
    installed = _write_skill(test_settings, "linked.md", "Temporary.")
    installed.unlink()
    try:
        installed.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this system.")
    with pytest.raises(ValidationError, match="escapes"):
        _registry(test_settings).snapshot()


@pytest.mark.parametrize("effort", ["auto", "quick", "thorough"])
def test_skills_reuse_tools_without_expanding_capabilities(test_settings, effort):
    _write_skill(test_settings, "local-recipe.md", """# Local recipe

Use for note comparisons. Use create_work_plan for substantial tasks.

```json
{"tools": [{"name": "run_shell"}]}
```
""")
    registry = PromptRegistry(storage=SafeStorage(test_settings))
    blueprint = (
        deep_work_blueprint({}, prompts=registry, web_enabled=False)
        if effort == "thorough"
        else research_blueprint({}, prompts=registry, web_enabled=False, response_effort=effort)
    )
    baseline = (
        deep_work_blueprint({}, web_enabled=False)
        if effort == "thorough"
        else research_blueprint({}, web_enabled=False, response_effort=effort)
    )
    assert blueprint.tools == baseline.tools
    assert blueprint.agent_tools == baseline.agent_tools
    for agent in blueprint.agents:
        assert '<skill name="local-recipe">' in agent.instructions
        assert '<skill name="web-synthesis">' in agent.instructions
        assert "acquire-source" not in agent.tool_ids
        assert "run_shell" not in agent.tool_ids
    if effort == "thorough":
        assert "create-work-plan" not in blueprint.agents[1].tool_ids


def test_legacy_fast_answer_does_not_load_skills(test_settings):
    _write_skill(test_settings, "broken.md", "")
    registry = PromptRegistry(storage=SafeStorage(test_settings))
    blueprint = research_blueprint({}, prompts=registry, fast_answer=True)
    assert "<skill " not in blueprint.agents[0].instructions
    assert {tool.id for tool in blueprint.tools} == {
        "search-sources", "acquire-source", "read-web-page",
    }


def test_custom_skills_reload_per_message_and_persist_in_run_snapshots(
    test_settings, stub_provider,
):
    _write_skill(test_settings, "local-style.md", "# Local style\n\nFirst recipe version.")
    with TestClient(create_app(test_settings)) as client:
        configure_provider(client, stub_provider)
        services = client.app.state.services
        conversation = client.post("/api/agent/conversations", json={}).json()
        records = []
        for index, version in enumerate(["First", "Second"], start=1):
            _write_skill(test_settings, "local-style.md", f"# Local style\n\n{version} recipe version.")
            response = client.post(
                f"/api/agent/conversations/{conversation['id']}/messages",
                json={"content": "Discuss my idea without saving anything.", "web_enabled": False},
            )
            assert response.status_code == 202, response.text
            run = wait_for_run(client, response.json()["run"]["id"])
            assert run["status"] == "completed", run["error"]
            record = services.runs.get(run["id"])
            records.append(record)
            assert len(stub_provider.requests) == index
            assert f"{version} recipe version." in stub_provider.requests[-1]["messages"][0]["content"]
            assert not record.tool_attempts
            assert not record.runtime_metadata_json.get("work_plan")
        assert "First recipe version." in records[0].blueprint_json["agents"][0]["instructions"]
        assert "Second recipe version." in records[1].blueprint_json["agents"][0]["instructions"]
        _write_skill(test_settings, "local-style.md", "")
        recovered = services.compiler.compile(AgentBlueprint.model_validate(records[0].blueprint_json))
        assert "First recipe version." in recovered.entry_agent.instructions
        rejected = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={"content": "A new turn.", "web_enabled": False},
        )
        assert rejected.status_code == 400, rejected.text
        assert "local-style.md" in rejected.text
        assert len(stub_provider.requests) == 2
        assert services.workspace.list_files() == []


def test_unavailable_web_skill_sources_block_plan_without_saving(
    test_settings, stub_provider, monkeypatch,
):
    async def download(url):
        raise WebSourceUnavailable(url, "No readable page content.")

    needle = "# Web synthesis"
    stub_provider.tool_plans = [
        (needle, "create_work_plan", {"items": [{"id": "read", "title": "Read and synthesize"}]}),
        (needle, "acquire_research_source", {
            "kind": "web_page", "url": "https://example.com/empty", "title": None,
        }),
        (needle, "update_work_item", {
            "id": "read", "status": "blocked", "summary": "The page has no readable content.",
        }),
    ]
    stub_provider.reply = "The page is unavailable. No synthesis was saved."
    with TestClient(create_app(test_settings)) as client:
        configure_provider(client, stub_provider)
        services = client.app.state.services
        monkeypatch.setattr(services.source_downloads, "download_web_page", download)
        conversation = client.post("/api/agent/conversations", json={}).json()
        response = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={"content": "Use web-synthesis to read https://example.com/empty and save a guide."},
        )
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        record = services.runs.get(run["id"])
        assert record.tool_attempts[-1].result_json["items"][0]["status"] == "blocked"
        assert services.workspace.list_files() == []
        assert "No synthesis was saved" in run["final_output"]


def test_skill_api_edits_bundled_as_local_override_and_creates_custom_files(test_settings):
    with TestClient(create_app(test_settings)) as client:
        listed = client.get("/api/skills")
        assert listed.status_code == 200
        assert [item["name"] for item in listed.json()] == BUNDLED_SKILLS
        original = client.get("/api/skills/web-synthesis").json()
        assert original["source"] == "bundled"
        content = "# My web synthesis\n\nRetain source URLs.\n"
        response = client.put("/api/skills/web-synthesis", json={
            "content": content, "expected_revision": original["revision"],
        })
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["source"] == "local"
        assert saved["content"] == content
        assert saved["revision"] != original["revision"]
        assert client.get("/api/skills/web-synthesis").json() == saved
        assert SafeStorage(test_settings).read_text(
            test_settings.data_dir, "skills/web-synthesis.md",
        ) == content
        assert "# Web synthesis" in PromptRegistry().render_skills()
        assert "# My web synthesis" in client.app.state.services.prompts.render_skills()
        created = client.put("/api/skills/compare-notes", json={
            "content": "# Compare notes\n\nUse existing notes.", "expected_revision": None,
        })
        assert created.status_code == 200, created.text
        assert [item["name"] for item in client.get("/api/skills").json()] == [
            "compare-notes", *BUNDLED_SKILLS,
        ]
        assert client.app.state.services.workspace.list_files() == []
        assert client.get("/api/skills/missing").status_code == 404


def test_skill_api_prevents_stale_overwrites_and_rejects_invalid_edits(test_settings):
    with TestClient(create_app(test_settings)) as client:
        original = client.get("/api/skills/web-synthesis").json()
        _write_skill(test_settings, "web-synthesis.md", "# External edit")
        stale = client.put("/api/skills/web-synthesis", json={
            "content": "# Stale draft", "expected_revision": original["revision"],
        })
        assert stale.status_code == 409, stale.text
        assert client.get("/api/skills/web-synthesis").json()["content"] == "# External edit"
        assert client.put("/api/skills/web-synthesis", json={
            "content": "# Cannot create over existing skill", "expected_revision": None,
        }).status_code == 409
        latest = client.get("/api/skills/web-synthesis").json()
        for content in [" \n", "\u00e9" * (MAX_SKILL_BYTES // 2 + 1)]:
            response = client.put("/api/skills/web-synthesis", json={
                "content": content, "expected_revision": latest["revision"],
            })
            assert response.status_code == 400, response.text
        assert client.put("/api/skills/bad_name", json={
            "content": "# Invalid name", "expected_revision": None,
        }).status_code == 400
        assert client.put("/api/skills/new-skill", json={
            "content": "# Instructions", "tools": [{"name": "execute"}],
        }).status_code == 422
        assert client.get("/api/skills/web-synthesis").json() == latest


def test_skill_api_enforces_combined_budget_before_writing(test_settings):
    for index in range(2):
        _write_skill(test_settings, f"large-{index}.md", "x" * MAX_SKILL_BYTES)
    with TestClient(create_app(test_settings)) as client:
        response = client.put("/api/skills/large-third", json={
            "content": "x" * MAX_SKILL_BYTES, "expected_revision": None,
        })
        assert response.status_code == 400, response.text
        assert "Combined skill instructions" in response.text
        assert not (test_settings.data_dir / "skills" / "large-third.md").exists()


def test_failed_skill_replace_preserves_previous_file_and_cleans_temporary(test_settings, monkeypatch):
    _write_skill(test_settings, "custom.md", "# Original")
    with TestClient(create_app(test_settings)) as client:
        original = client.get("/api/skills/custom").json()
        replace = Path.replace

        def fail_replace(path, target):
            if Path(target).name == "custom.md":
                raise OSError("Disk replacement failed.")
            return replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_replace)
        response = client.put("/api/skills/custom", json={
            "content": "# Replacement", "expected_revision": original["revision"],
        })
        assert response.status_code == 400, response.text
        assert "Could not save skill" in response.text
        assert client.get("/api/skills/custom").json() == original
        assert [path.name for path in (test_settings.data_dir / "skills").iterdir()] == ["custom.md"]

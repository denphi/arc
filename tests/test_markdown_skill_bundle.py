from pathlib import Path

import pytest

from arc.contracts.agent import AgentContext
from arc.core.loader import MarkdownSkill


class CaptureProvider:
    def __init__(self):
        self.prompt = ""

    async def complete(self, prompt):
        self.prompt = prompt
        return "{}"


def test_markdown_skill_bundle_lists_and_reads_resources(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "scripts").mkdir()
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("---\nname: demo\n---\n# Demo\n", encoding="utf-8")
    (skill_dir / "references" / "schema.md").write_text("schema", encoding="utf-8")
    (skill_dir / "scripts" / "export.py").write_text("print('ok')", encoding="utf-8")

    skill = MarkdownSkill("demo", skill_path, skill_path.read_text(encoding="utf-8"))

    assert skill.bundle_root == str(skill_dir)
    assert skill._content is None
    assert skill.list_resources("references") == ["references/schema.md"]
    assert "scripts/export.py" in skill.metadata["resources"]
    assert skill.read_resource("references/schema.md") == "schema"


def test_markdown_skill_loads_instructions_only_on_activation(tmp_path):
    skill_dir = tmp_path / "lazy"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        "---\nname: lazy\ndescription: Lazy skill.\n---\n# Initial\n",
        encoding="utf-8",
    )
    skill = MarkdownSkill("lazy", skill_path)
    skill_path.write_text(
        "---\nname: lazy\ndescription: Lazy skill.\n---\n# Updated\n",
        encoding="utf-8",
    )

    assert skill._content is None
    assert "# Updated" in skill.content


def test_markdown_skill_bundle_rejects_resource_escape(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("# Demo\n", encoding="utf-8")
    skill = MarkdownSkill("demo", skill_path, "# Demo\n")

    with pytest.raises(ValueError, match="escapes bundle root"):
        skill.resolve_resource("../outside.md")


@pytest.mark.asyncio
async def test_markdown_skill_prompt_includes_bundle_and_file_tables(tmp_path):
    skill_dir = tmp_path / "skill"
    (skill_dir / "references").mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("# Demo\n", encoding="utf-8")
    (skill_dir / "references" / "schema.md").write_text("schema", encoding="utf-8")
    source = tmp_path / "paper.txt"
    source.write_text("paper", encoding="utf-8")

    from arc.assets import FileStore

    store = FileStore(tmp_path / "store")
    asset = store.import_file(source, role="paper", session_id="s1")
    provider = CaptureProvider()
    context = AgentContext(
        session_id="s1",
        memory={"provider": provider, "file_store": store},
    )
    skill = MarkdownSkill("demo", skill_path, "# Demo\n")

    await skill.execute({"paper": asset.id}, context)

    assert "Skill bundle resources:" in provider.prompt
    assert "references/schema.md" in provider.prompt
    assert "Available file inputs:" in provider.prompt
    assert asset.id in provider.prompt
    assert "paper.txt paper text/plain" in provider.prompt


# ── output_format: json (structured skill output) ────────────────────────


class _StaticProvider:
    """Provider that returns a fixed response regardless of prompt."""

    def __init__(self, response: str):
        self._response = response
        self.prompt = ""

    async def complete(self, prompt, system: str = "", **kwargs):
        self.prompt = prompt
        return self._response


def _skill_with_frontmatter(tmp_path, body_frontmatter: str) -> MarkdownSkill:
    path = tmp_path / "SKILL.md"
    content = f"---\n{body_frontmatter}\n---\n# Demo skill\n"
    path.write_text(content, encoding="utf-8")
    return MarkdownSkill("demo", path, content)


@pytest.mark.asyncio
async def test_markdown_skill_default_returns_raw_text(tmp_path):
    skill = _skill_with_frontmatter(tmp_path, "name: demo")
    provider = _StaticProvider('{"a": 1}')
    context = AgentContext(session_id="s1", memory={"provider": provider})

    out = await skill.execute({}, context)

    # Default (no output_format): unchanged — raw string, no `format` key.
    assert out == {"skill": "demo", "result": '{"a": 1}'}
    assert "Return a concise JSON-compatible result." in provider.prompt


@pytest.mark.asyncio
async def test_markdown_skill_output_format_json_parses_object(tmp_path):
    skill = _skill_with_frontmatter(tmp_path, "name: demo\noutput_format: json")
    provider = _StaticProvider('{"problem_id": "p1", "fields": ["u"]}')
    context = AgentContext(session_id="s1", memory={"provider": provider})

    out = await skill.execute({}, context)

    assert out["skill"] == "demo"
    assert out["format"] == "json"
    assert out["result"] == {"problem_id": "p1", "fields": ["u"]}
    # The prompt steers the model toward strict JSON.
    assert "ONLY a single valid JSON value" in provider.prompt


@pytest.mark.asyncio
async def test_markdown_skill_output_schema_triggers_json_and_strips_fences(tmp_path):
    # A truthy `output_schema` (not just output_format) also opts in.
    skill = _skill_with_frontmatter(tmp_path, "name: demo\noutput_schema: extraction")
    provider = _StaticProvider('Here you go:\n```json\n{"ok": true}\n```\n')
    context = AgentContext(session_id="s1", memory={"provider": provider})

    out = await skill.execute({}, context)

    assert out["format"] == "json"
    assert out["result"] == {"ok": True}


@pytest.mark.asyncio
async def test_markdown_skill_json_parse_failure_is_flagged(tmp_path):
    skill = _skill_with_frontmatter(tmp_path, "name: demo\noutput_format: json")
    provider = _StaticProvider("this is not json at all")
    context = AgentContext(session_id="s1", memory={"provider": provider})

    out = await skill.execute({}, context)

    # Falls back to raw text but flags the failure rather than crashing.
    assert out["result"] == "this is not json at all"
    assert out["format"] == "text"
    assert out["parse_error"] is True


def test_allowed_tools_accepts_commas_and_lists():
    """`allowed-tools: Read, Write, Edit` is the natural spelling.

    Splitting on whitespace alone produced ["Read,", "Write,", "Edit"] — names
    with trailing commas that match no tool, so a skill silently lost the
    permissions it declared.
    """
    from arc.core.skill_bundle import _parse_allowed_tools

    assert _parse_allowed_tools("Read, Write, Edit") == ["Read", "Write", "Edit"]
    assert _parse_allowed_tools("Read,Write") == ["Read", "Write"]
    assert _parse_allowed_tools("Read Write Edit") == ["Read", "Write", "Edit"]
    assert _parse_allowed_tools(["Read", " Write "]) == ["Read", "Write"]
    assert _parse_allowed_tools(None) == []
    assert _parse_allowed_tools(7) == []


def test_bundle_skill_content_excludes_frontmatter(tmp_path):
    """A bundle's instructions go to the model; its metadata should not.

    `content` feeds straight into the execution prompt, so leaving the YAML in
    handed the model `output_format: json` and the tool allow-list as though
    they were instructions.
    """
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\noutput_format: json\n---\n"
        "# Demo\n\nDo the thing.\n",
        encoding="utf-8",
    )
    skill = MarkdownSkill("demo", skill_dir / "SKILL.md")

    assert "output_format" not in skill.content
    assert "description: Demo skill." not in skill.content
    assert "Do the thing." in skill.content
    # Metadata is still parsed — it just doesn't reach the prompt.
    assert skill.description == "Demo skill."
    assert skill._wants_json_output() is True


def test_flat_legacy_skill_content_is_unchanged(tmp_path):
    """Non-bundle skills have no frontmatter to strip; keep them byte-identical."""
    path = tmp_path / "legacy.md"
    body = "# legacy\n\n## Description\nA flat skill.\n"
    path.write_text(body, encoding="utf-8")

    assert MarkdownSkill("legacy", path).content == body


def test_create_sim2l_ships_as_a_canonical_bundle():
    """arc-sim2l's flagship skill is a real bundle, not a flat .md file."""
    from arc.core.skill_bundle import validate_skill_bundle

    root = Path(__file__).resolve().parent.parent
    bundle_dir = root / "arc/packages/arc-sim2l/skills/create-sim2l"

    assert validate_skill_bundle(bundle_dir) == []

    skill = MarkdownSkill("create-sim2l", bundle_dir / "SKILL.md")
    assert skill.bundle is not None
    assert skill.frontmatter["name"] == "create-sim2l"
    # Declares a structured contract, so callers get a dict rather than prose.
    assert skill._wants_json_output() is True
    assert skill.metadata["allowed_tools"] == ["Read", "Write", "Edit"]
    assert sorted(skill.list_resources()) == [
        "references/artifact-contract.md",
        "references/worked-example.md",
    ]
    # The instructions must state the constraints the executor actually
    # enforces — a skill that omits them produces artifacts that fail the
    # static safety check after a full model round-trip.
    body = skill.content
    for required in ("simulate(**inputs)", "sim2l.yaml", "snake_case", "allow-list"):
        assert required in body, f"SKILL.md no longer documents {required!r}"

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
    assert skill.list_resources("references") == ["references/schema.md"]
    assert "scripts/export.py" in skill.metadata["resources"]
    assert skill.read_resource("references/schema.md") == "schema"


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

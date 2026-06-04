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

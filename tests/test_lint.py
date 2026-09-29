from __future__ import annotations

from aft import lint
from aft.registry import Registry

PAGE = """\
---
type: Project
title: widget
sources:
  - id: s1
    resource: aft://session/claude/abc#entry-1
    quote: "we will use SQLite"
aft:
  project: {project_id}
  anchors:
    s1: {{path: src/index.py, all_of: [sqlite3], commit: 4f2a91c}}
---

## Decisions

- The index is SQLite. [stated] 2026-09-28 [^s1]
"""


def register(cfg, slug="widget"):
    registry = Registry.load(cfg.registry_path)
    project = registry.add(slug)
    registry.save()
    return project


def write(cfg, rel, text):
    path = cfg.vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def messages(cfg):
    return [str(f) for f in lint.lint_vault(cfg.vault)]


def test_new_vault_is_clean(initialized):
    assert messages(initialized) == []


def test_valid_project_page(initialized):
    project = register(initialized)
    write(initialized, "projects/widget/overview.md", PAGE.format(project_id=project.id))
    assert messages(initialized) == []


def test_page_errors_carry_their_line(initialized):
    project = register(initialized)
    text = PAGE.format(project_id=project.id).replace("[stated] 2026", "[heard] 2026")
    write(initialized, "projects/widget/overview.md", text)
    assert messages(initialized) == ["projects/widget/overview.md:16: unknown source tag [heard]"]


def check(cfg, replace_from, replace_to, expected):
    project = register(cfg)
    text = PAGE.format(project_id=project.id).replace(replace_from, replace_to)
    write(cfg, "projects/widget/overview.md", text)
    found = messages(cfg)
    assert any(expected in m for m in found), found


def test_missing_type(initialized):
    check(initialized, "type: Project\n", "", "non-empty type")


def test_undefined_footnote(initialized):
    check(initialized, "[^s1]\n", "[^s9]\n", "footnote [^s9] is not defined")


def test_source_without_quote(initialized):
    check(initialized, '    quote: "we will use SQLite"\n', "", "source s1 has no quote")


def test_source_without_resource(initialized):
    check(initialized, "    resource: aft://session/claude/abc#entry-1\n", "", "needs a resource")


def test_wrong_project_id(initialized):
    check(initialized, "  project: p_", "  project: x_", "aft.project must be")


def test_bad_anchor(initialized):
    check(initialized, ", commit: 4f2a91c", "", "anchor s1 needs")


def test_bad_status(initialized):
    check(initialized, "title: widget\n", "title: widget\nstatus: final\n", "status must be one of")


def test_unregistered_project_directory(initialized):
    write(initialized, "projects/ghost/overview.md", PAGE.format(project_id="p_x"))
    assert any("not a registered project" in m for m in messages(initialized))


def test_missing_frontmatter(initialized):
    write(initialized, "digests/widget/note.md", "# just text\n")
    assert messages(initialized) == ["digests/widget/note.md:1: missing frontmatter"]


def test_root_index_needs_okf_version(initialized):
    write(initialized, "index.md", "# no frontmatter\n")
    assert any("okf_version" in m for m in messages(initialized))


def test_nested_index_may_not_have_frontmatter(initialized):
    write(initialized, "projects/widget/index.md", "---\ntype: Index\n---\n")
    register(initialized)
    assert any("only the root index.md" in m for m in messages(initialized))


def test_global_page_cannot_name_a_project(initialized):
    text = (initialized.vault / "global/preferences.md").read_text()
    write(
        initialized,
        "global/preferences.md",
        text.replace("sources: []", "sources: []\naft:\n  project: p_x"),
    )
    assert any("global pages cannot" in m for m in messages(initialized))


def test_library_and_reports_are_not_fact_pages(initialized):
    write(initialized, "library/rvc/notes.md", "no frontmatter at all\n")
    write(initialized, "reports/2026/09/2026-09-28.md", "---\ntype: Dream Report\n---\nfree text\n")
    assert messages(initialized) == []


def test_broken_registry_is_reported(initialized):
    initialized.registry_path.write_text("version: 7\n")
    assert any(m.startswith("registry.yaml:1:") for m in messages(initialized))


def test_note_footnotes_must_resolve(initialized):
    project = register(initialized)
    note = "\n> [!conflict] 2026-10-01\n> A session says otherwise ([^s1]), see also [^s9].\n"
    text = PAGE.format(project_id=project.id) + note
    write(initialized, "projects/widget/overview.md", text)
    assert messages(initialized) == [
        "projects/widget/overview.md:19: footnote [^s9] is not defined in sources"
    ]


def test_plain_brackets_in_a_note_are_not_footnotes(initialized):
    project = register(initialized)
    note = "\n> [!question] 2026-10-01\n> Is [this] still true? See [s9] and [^ not a ref].\n"
    write(initialized, "projects/widget/overview.md", PAGE.format(project_id=project.id) + note)
    assert messages(initialized) == []


def test_struck_fact_without_a_state_marker_is_a_finding(initialized):
    project = register(initialized)
    text = PAGE.format(project_id=project.id).replace(
        "- The index is SQLite. [stated] 2026-09-28 [^s1]",
        "- ~~The index is SQLite.~~ [stated] 2026-09-28 [^s1] no longer true",
    )
    write(initialized, "projects/widget/overview.md", text)
    assert messages(initialized) == [
        "projects/widget/overview.md:16: a struck fact must end "
        "'superseded <date> by [^id]' or 'withdrawn <date>'"
    ]


def test_a_refused_lines_footnotes_are_still_checked(initialized):
    project = register(initialized)
    text = (
        PAGE.format(project_id=project.id).replace(
            "- The index is SQLite. [stated] 2026-09-28 [^s1]",
            "- ~~The index is SQLite.~~ [stated] 2026-09-28 [^s9] no longer true",
        )
        + "\n> [!conflict] 2026-09-30\n> Neither citation resolves: [^s9] and [^s8].\n"
    )
    write(initialized, "projects/widget/overview.md", text)
    found = messages(initialized)
    assert any("a struck fact must end" in m for m in found), found
    assert "projects/widget/overview.md:16: footnote [^s9] is not defined in sources" in found
    assert "projects/widget/overview.md:19: footnote [^s9] is not defined in sources" in found
    assert "projects/widget/overview.md:19: footnote [^s8] is not defined in sources" in found


def test_typed_answers_under_a_refused_note_are_not_findings(initialized):
    project = register(initialized)
    text = PAGE.format(project_id=project.id) + (
        "\n> [!conflict] 2026-09-31\n> That day does not exist, but this answer is mine.\n"
        "> It is not a broken callout header.\n"
    )
    write(initialized, "projects/widget/overview.md", text)
    assert messages(initialized) == ["projects/widget/overview.md:18: invalid date 2026-09-31"]

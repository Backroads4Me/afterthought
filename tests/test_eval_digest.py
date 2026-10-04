"""The F54 harness: the digest pipeline runs unmodified over the test set, every raw
answer is saved, the backends agree and differ by quote overlap, and a run scores against
the answer key (BUILD-PLAN F54).

The gemini and claude backends are exercised only through a fake command, never the real
CLI. The local backend is exercised through the fake endpoint, never a real one.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from aft import config as configmod
from aft import model
from aft.config import Config
from fakes.model_server import MODEL_NAME, FakeEndpoint

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "eval_digest.py"
NOW = "2026-10-04T09:00:00Z"


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location("eval_digest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fixtures of the test set


def row(entry_id: str, role: str, text: str, is_error: bool = False) -> dict:
    """One row of an exported session's evidence, as the lead's export holds it."""
    return {"entry_id": entry_id, "time": NOW, "role": role, "text": text, "is_error": is_error}


def write_data(tmp_path: Path, sessions: dict[str, list[dict]], cwd: str = "/tmp/x") -> Path:
    """A data directory with one exported session per entry of ``sessions``."""
    data = tmp_path / "data"
    index = []
    for number, (session_id, rows) in enumerate(sessions.items(), start=1):
        directory = data / "sessions" / session_id
        directory.mkdir(parents=True)
        (directory / "evidence.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        index.append({"session_id": session_id, "agent": "claude", "cwd": cwd, "project_id": None})
        assert number
    (data / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return data


def empty_synthetic(tmp_path: Path) -> Path:
    """A synthetic directory with no sessions in it, for a run over real sessions alone."""
    directory = tmp_path / "synthetic"
    directory.mkdir()
    return directory


def claude_record(kind: str, entry_id: str, text: str, seconds: int = 0) -> dict:
    """One conversation record of a Claude Code transcript, as the reader reads it."""
    return {
        "type": kind,
        "uuid": entry_id,
        "timestamp": f"2026-10-04T09:00:{seconds:02d}Z",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def serve(cfg: Config, *answers: object) -> FakeEndpoint:
    """Point the config at the fake endpoint, which answers with these objects in order."""
    fake = FakeEndpoint(list(answers))
    fake.start()
    path = cfg.home / configmod.CONFIG_NAME
    path.write_text(
        path.read_text(encoding="utf-8")
        + f'\n[model]\nendpoint = "{fake.endpoint}"\nname = "{MODEL_NAME}"\n',
        encoding="utf-8",
    )
    return fake


def run_local(ev, data: Path, fake: FakeEndpoint, tmp_path: Path, *extra: str) -> int:
    """One harness run of the local backend, aimed at the fake endpoint ``serve`` set up.

    The model settings are given by flags, so every run test names the fake endpoint here
    and none of them can reach a real one by forgetting.
    """
    return ev.main(
        [
            "run",
            "--data",
            str(data),
            "--endpoint",
            fake.endpoint,
            "--model",
            MODEL_NAME,
            "--synthetic",
            str(empty_synthetic(tmp_path)),
            *extra,
        ]
    )


def digest_answer(*facts: dict, summary: str = "The user stated the plan for the day.") -> dict:
    """One answer of the shape the schema describes."""
    return {
        "summary": summary,
        "handoff": {"in_flight": {"text": "", "entry": ""}, "failed": [], "open": []},
        "facts": list(facts),
    }


def plan_fact(entry: str = "#1", quote: str = "hello there, this is the plan") -> dict:
    """The fact a correct digest of the fixture session keeps, citing a real label."""
    return {
        "statement": "The session's plan was stated by the user.",
        "kind": "decision",
        "source": "stated",
        "term": "long",
        "quote": quote,
        "entry": entry,
        "scope": "project",
    }


def raw(data: Path, backend: str, session_id: str, variant: str = "base") -> dict:
    """What the run wrote for one session, read back."""
    path = ev_run_dir(data, backend, session_id, variant) / "summary.json"
    return json.loads(path.read_text(encoding="utf-8"))


def ev_run_dir(data: Path, backend: str, session_id: str, variant: str = "base") -> Path:
    root = data / "answers" / backend
    if variant != "base":
        root = root / variant
    return root / session_id


def write_summary(
    data: Path,
    backend: str,
    session_id: str,
    facts: list[dict],
    variant: str = "base",
    **extra: object,
) -> Path:
    """A hand-made session summary, as a run of the harness would have written it."""
    path = ev_run_dir(data, backend, session_id, variant)
    path.mkdir(parents=True, exist_ok=True)
    summary = {
        "session": session_id,
        "agent": "claude",
        "source": "export",
        "case": None,
        "backend": backend,
        "variant": variant,
        "model": "fake-model",
        "endpoint": "http://127.0.0.1:1/v1",
        "command": None,
        "entries": 3,
        "parts": 1,
        "calls": 1,
        "facts": facts,
        "dropped": [],
        "refused_parts": [],
        "summaries": ["The user stated the plan for the day."],
        "handoff": {"in_flight": "", "failed": [], "open": []},
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        "seconds": 1.5,
        "nothing_to_digest": False,
        "fully_refused": False,
        "refused_reason": None,
    }
    summary.update(extra)
    (path / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path


def key_fact(
    quote: str,
    source: str = "stated",
    kind: str = "decision",
    term: str = "long",
    scope: str = "project",
    statement: str = "The session settled it.",
) -> dict:
    """One fact of an answer key, as the lead settles it."""
    return {
        "statement": statement,
        "quote": quote,
        "source": source,
        "kind": kind,
        "term": term,
        "scope": scope,
    }


def write_key(data: Path, session_id: str, facts: list[dict]) -> Path:
    directory = data / "key"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.json"
    path.write_text(json.dumps({"session": session_id, "facts": facts}, indent=2) + "\n")
    return path


# ---------------------------------------------------------------------------
# The home the harness is allowed to run against


def test_run_refuses_to_guess_a_home(ev):
    with pytest.raises(ev.EvalError, match="AFT_HOME"):
        ev.load_config({})


def test_run_refuses_the_real_home(ev, home, monkeypatch):
    monkeypatch.setenv(configmod.HOME_ENV, str(Path.home() / ".afterthought"))
    with pytest.raises(ev.EvalError, match="refusing"):
        ev.load_config()


def test_run_refuses_a_vault_in_the_real_tree(ev, home):
    (home / configmod.CONFIG_NAME).write_text(
        'vault = "~/afterthought/vault"\nhost = "testhost"\n', encoding="utf-8"
    )
    with pytest.raises(ev.EvalError, match="refusing"):
        ev.load_config()


def test_main_reports_a_refusal_and_exits_two(ev, tmp_path, capsys):
    code = ev.main(["run", "--data", str(tmp_path / "nothere")])
    assert code == 2
    assert "eval-digest:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Loading the test set


def test_real_sessions_read_the_export(ev, tmp_path):
    data = write_data(
        tmp_path,
        {
            "sess-1": [
                row("e1", "user", "hello there, this is the plan for today"),
                row("e2", "assistant", "I read the plan and agree with it."),
                row("e3", "tool", "FAILED tests/test_x.py - assert False", is_error=True),
            ]
        },
    )
    sessions = ev.real_sessions(data)
    assert [session.session_id for session in sessions] == ["sess-1"]
    session = sessions[0]
    assert session.source == "export"
    assert [entry.role for entry in session.entries] == ["user", "assistant", "tool"]
    assert [entry.id for entry in session.entries] == ["e1", "e2", "e3"]
    assert session.entries[2].is_error is True


def test_export_rows_are_read_in_conversation_order(ev, tmp_path):
    data = write_data(
        tmp_path,
        {
            "sess-1": [
                row("later", "user", "second prompt"),
                row("earlier", "assistant", "first answer"),
            ]
        },
    )
    directory = data / "sessions" / "sess-1" / "evidence.jsonl"
    rows = [json.loads(line) for line in directory.read_text().splitlines()]
    rows[0]["time"] = "2026-10-04T10:00:00Z"
    rows[1]["time"] = "2026-10-04T09:00:00Z"
    directory.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    sessions = ev.real_sessions(data)
    assert [entry.id for entry in sessions[0].entries] == ["earlier", "later"]


def test_a_broken_export_is_refused(ev, tmp_path):
    data = write_data(tmp_path, {"sess-1": [row("e1", "user", "hello")]})
    path = data / "sessions" / "sess-1" / "evidence.jsonl"
    path.write_text('{"entry_id": "e1", "role": "ghost", "text": "x"}\n', encoding="utf-8")
    with pytest.raises(ev.EvalError, match="unknown role"):
        ev.real_sessions(data)


def test_synthetic_sessions_run_through_the_filter(ev, tmp_path, cfg):
    directory = tmp_path / "synthetic" / "synthetic-x"
    directory.mkdir(parents=True)
    (directory / "expected.json").write_text(
        json.dumps({"session_id": "synthetic-x", "agent": "claude", "case": "c", "facts": []}),
        encoding="utf-8",
    )
    (directory / "transcript.jsonl").write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in [
                claude_record("user", "u1", "let us plan the day", 0),
                claude_record("assistant", "a1", "Here is the plan.", 1),
                claude_record("user", "u2", "aft off", 2),
                claude_record("assistant", "a2", "This part is off the record.", 3),
            ]
        ),
        encoding="utf-8",
    )
    sessions = ev.synthetic_sessions(tmp_path / "synthetic", cfg)
    assert [session.session_id for session in sessions] == ["synthetic-x"]
    assert sessions[0].source == "synthetic"
    assert sessions[0].case == "c"
    # The off-the-record span is dropped by the shipped filter, so the model never sees it.
    assert [entry.id for entry in sessions[0].entries] == ["u1", "a1"]


def test_select_sessions_names_the_set_when_asked_for_an_unknown_one(ev, tmp_path):
    data = write_data(tmp_path, {"sess-1": [row("e1", "user", "hello")]})
    sessions = ev.real_sessions(data)
    with pytest.raises(ev.EvalError, match="sess-1"):
        ev.select_sessions(sessions, ["nope"])
    assert [s.session_id for s in ev.select_sessions(sessions, ["sess-1"])] == ["sess-1"]


# ---------------------------------------------------------------------------
# Running the pipeline


def test_run_records_every_answer_and_what_the_pipeline_kept(ev, tmp_path, cfg):
    data = write_data(
        tmp_path,
        {
            "sess-1": [
                row("e1", "user", "hello there, this is the plan for today"),
                row("e2", "assistant", "I read the plan and agree with it."),
                row("e3", "tool", "FAILED tests/test_x.py - assert False", is_error=True),
            ]
        },
    )
    answer = digest_answer(plan_fact())
    fake = serve(cfg, answer)
    original = model.complete_json
    code = run_local(ev, data, fake, tmp_path)
    assert code == 0
    summary = raw(data, "local", "sess-1")
    assert summary["backend"] == "local"
    assert summary["model"] == MODEL_NAME
    assert summary["endpoint"] == fake.endpoint
    assert summary["command"] is None
    assert summary["parts"] == 1
    assert summary["calls"] == 1
    assert summary["nothing_to_digest"] is False
    assert summary["fully_refused"] is False
    # The label #1 is mapped back to the entry's own id, and the quote verified against
    # the archive, so the fact is one the pipeline kept.
    assert summary["facts"] == [
        {
            "statement": plan_fact()["statement"],
            "kind": "decision",
            "source": "stated",
            "quote": plan_fact()["quote"],
            "entry": "e1",
            "scope": "project",
            "date": "2026-10-04",
            "role": "primary",
            "term": "long",
        }
    ]
    assert summary["dropped"] == []
    assert summary["usage"] == {"prompt_tokens": 200, "completion_tokens": 50}
    call = json.loads(
        (ev_run_dir(data, "local", "sess-1") / "call-001.json").read_text(encoding="utf-8")
    )
    assert call["answer"] == answer
    assert call["error"] is None
    assert call["usage"] == {"prompt_tokens": 200, "completion_tokens": 50}
    assert call["entries"] == {"first": "#1", "last": "#3", "count": 3}
    manifest = json.loads((data / "answers" / "local" / "run.json").read_text(encoding="utf-8"))
    assert manifest["sessions"] == ["sess-1"]
    assert manifest["model"] == MODEL_NAME
    # The pipeline's own answer source is back in place after the run.
    assert model.complete_json is original


def test_run_again_replaces_the_stale_answers(ev, tmp_path, cfg):
    data = write_data(
        tmp_path, {"sess-1": [row("e1", "user", "hello"), row("e2", "assistant", "hi")]}
    )
    fake = serve(cfg, digest_answer())
    out = ev_run_dir(data, "local", "sess-1")
    out.mkdir(parents=True)
    (out / "call-099.json").write_text("{}\n", encoding="utf-8")
    code = run_local(ev, data, fake, tmp_path)
    assert code == 0
    assert sorted(path.name for path in out.iterdir()) == ["call-001.json", "summary.json"]


def test_a_part_the_model_cannot_read_is_refused_and_recorded(ev, tmp_path, cfg):
    data = write_data(
        tmp_path, {"sess-1": [row("e1", "user", "hello"), row("e2", "assistant", "hi")]}
    )
    fake = serve(cfg, digest_answer(plan_fact(entry="#9")), digest_answer(plan_fact(entry="#9")))
    code = run_local(ev, data, fake, tmp_path)
    assert code == 0
    summary = raw(data, "local", "sess-1")
    assert summary["fully_refused"] is True
    assert summary["facts"] == []
    assert summary["calls"] == 2
    assert "not in the chunk" in summary["refused_reason"]
    for call in (1, 2):
        record = json.loads(
            (ev_run_dir(data, "local", "sess-1") / f"call-{call:03d}.json").read_text()
        )
        assert record["answer"] is not None and record["error"] is None


def test_an_answer_that_fails_its_schema_is_recorded_as_a_refusal(ev, tmp_path, cfg):
    data = write_data(
        tmp_path, {"sess-1": [row("e1", "user", "hello"), row("e2", "assistant", "hi")]}
    )
    fake = serve(cfg, "not json at all")
    code = run_local(ev, data, fake, tmp_path)
    assert code == 0
    summary = raw(data, "local", "sess-1")
    assert summary["fully_refused"] is True
    assert summary["calls"] == 1
    record = json.loads(
        (ev_run_dir(data, "local", "sess-1") / "call-001.json").read_text(encoding="utf-8")
    )
    assert record["answer"] is None
    assert "the answer failed its schema" in record["error"]


def test_a_session_with_no_filtered_entries_costs_no_call(ev, tmp_path, cfg):
    data = write_data(tmp_path, {"sess-1": []})
    fake = serve(cfg)
    code = run_local(ev, data, fake, tmp_path)
    assert code == 0
    summary = raw(data, "local", "sess-1")
    assert summary["nothing_to_digest"] is True
    assert summary["calls"] == 0
    assert fake.requests == []


def test_a_later_variant_runs_in_its_own_directory(ev, tmp_path, cfg):
    data = write_data(
        tmp_path, {"sess-1": [row("e1", "user", "hello"), row("e2", "assistant", "hi")]}
    )
    fake = serve(cfg, digest_answer())
    code = run_local(ev, data, fake, tmp_path, "--variant", "narrow")
    assert code == 0
    assert (ev_run_dir(data, "local", "sess-1", "narrow") / "summary.json").is_file()
    assert not (ev_run_dir(data, "local", "sess-1") / "summary.json").exists()
    manifest = json.loads(
        (data / "answers" / "local" / "narrow" / "run.json").read_text(encoding="utf-8")
    )
    assert manifest["variant"] == "narrow"


# ---------------------------------------------------------------------------
# The gemini and claude backends, through a fake command


def _fake_command(tmp_path: Path, lines: str) -> tuple[str, ...]:
    stub = tmp_path / "fake_cli.py"
    stub.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"Path({str(tmp_path / 'prompt.txt')!r}).write_text(sys.argv[-1])\n" + lines,
        encoding="utf-8",
    )
    return (sys.executable, str(stub))


def test_gemini_backend_answers_through_a_fake_command(ev, tmp_path, cfg, monkeypatch):
    data = write_data(
        tmp_path,
        {
            "sess-1": [
                row("e1", "user", "hello there, this is the plan for today"),
                row("e2", "assistant", "I read the plan and agree with it."),
            ]
        },
    )
    answer = digest_answer(plan_fact())
    monkeypatch.setattr(
        ev, "GEMINI_COMMAND", _fake_command(tmp_path, f"print(json.dumps({answer!r}))\n")
    )
    code = ev.main(
        [
            "run",
            "--backend",
            "gemini",
            "--data",
            str(data),
            "--synthetic",
            str(empty_synthetic(tmp_path)),
        ]
    )
    assert code == 0
    summary = raw(data, "gemini", "sess-1")
    assert summary["backend"] == "gemini"
    assert summary["model"] is None
    assert summary["endpoint"] is None
    assert summary["command"] == list(ev.GEMINI_COMMAND)
    assert summary["facts"][0]["entry"] == "e1"
    call = json.loads(
        (ev_run_dir(data, "gemini", "sess-1") / "call-001.json").read_text(encoding="utf-8")
    )
    assert call["answer"] == answer
    assert call["usage"] is None
    assert call["command"] == list(ev.GEMINI_COMMAND)
    # The prompt the CLI was handed is the shipped prompt followed by the rendered chunk.
    prompt = (tmp_path / "prompt.txt").read_text(encoding="utf-8")
    assert prompt.startswith(ev.digestmod.prompt_text())
    assert "[#1] USER" in prompt


def test_claude_backend_refuses_an_answer_with_no_json(ev, tmp_path, cfg, monkeypatch):
    data = write_data(
        tmp_path, {"sess-1": [row("e1", "user", "hello"), row("e2", "assistant", "hi")]}
    )
    monkeypatch.setattr(ev, "CLAUDE_COMMAND", _fake_command(tmp_path, "print('I cannot answer')\n"))
    code = ev.main(
        [
            "run",
            "--backend",
            "claude",
            "--data",
            str(data),
            "--synthetic",
            str(empty_synthetic(tmp_path)),
        ]
    )
    assert code == 0
    summary = raw(data, "claude", "sess-1")
    assert summary["fully_refused"] is True
    assert summary["calls"] == 1
    assert "printed no JSON object" in summary["refused_reason"]
    record = json.loads(
        (ev_run_dir(data, "claude", "sess-1") / "call-001.json").read_text(encoding="utf-8")
    )
    assert record["answer"] is None
    assert "the answer failed its schema" in record["error"]
    assert record["raw_text"] == "I cannot answer\n"


# ---------------------------------------------------------------------------
# Matching facts by quote overlap


def named(quote: str) -> dict:
    return {"statement": "s", "quote": quote}


def test_quotes_of_one_sentence_match_across_backends(ev):
    left = named("run the full suite before pushing")
    right = named("Run the full suite before pushing!")
    groups = ev.match_facts({"local": [left], "gemini": [right]})
    assert len(groups) == 1
    assert groups[0].facts == {"local": left, "gemini": right}
    assert groups[0].overlap >= ev.QUOTE_MATCH_RATIO


def test_quotes_of_different_sentences_do_not_match(ev):
    left = named("run the full suite before pushing")
    right = named("the endpoint moved to port 8008 this morning")
    groups = ev.match_facts({"local": [left], "gemini": [right]})
    assert [(group.facts, group.overlap) for group in groups] == [
        ({"local": left}, None),
        ({"gemini": right}, None),
    ]


def test_a_fact_never_joins_two_groups(ev):
    mine = named("the deploy window is noon to one")
    first = named("the deploy window is noon to one")
    second = named("the deploy window is noon to one today")
    groups = ev.match_facts({"local": [mine], "gemini": [first, second]})
    joined = [group for group in groups if "local" in group.facts]
    assert len(joined) == 1
    assert joined[0].facts["gemini"] in (first, second)
    loser = second if joined[0].facts["gemini"] is first else first
    left_alone = [group for group in groups if group.facts.get("gemini") is loser]
    assert len(left_alone) == 1 and left_alone[0].overlap is None


# ---------------------------------------------------------------------------
# Comparing the backends


def test_compare_lists_where_backends_agree_and_differ(ev, tmp_path):
    data = tmp_path / "data"
    shared = key_fact("run the full suite before pushing")
    write_summary(data, "local", "sess-1", [dict(shared), named("deploy at noon")])
    write_summary(
        data,
        "gemini",
        "sess-1",
        [dict(shared), key_fact("the endpoint moved to port 8008", source="observed")],
    )
    write_summary(data, "local", "sess-2", [])
    paths = ev.compare_runs(data, "base", ["local", "gemini"])
    assert len(paths) == 2
    report = json.loads((data / "compare" / "base" / "sess-1.json").read_text())
    assert report["backends"] == ["local", "gemini"]
    agreed = [group for group in report["groups"] if len(group["facts"]) == 2]
    assert len(agreed) == 1 and agreed[0]["overlap"] >= ev.QUOTE_MATCH_RATIO
    only_local = [
        group
        for group in report["groups"]
        if group["facts"].get("local") and len(group["facts"]) == 1
    ]
    only_gemini = [
        group
        for group in report["groups"]
        if group["facts"].get("gemini") and len(group["facts"]) == 1
    ]
    assert len(only_local) == 1 and len(only_gemini) == 1
    missing = json.loads((data / "compare" / "base" / "sess-2.json").read_text())
    assert missing["not_run"] == {"gemini": ["sess-2"]}


def test_compare_without_answers_is_refused(ev, tmp_path):
    with pytest.raises(ev.EvalError, match="no answers"):
        ev.compare_runs(tmp_path / "data", "base", ["local"])


# ---------------------------------------------------------------------------
# Scoring a run against the key


def test_score_counts_wrong_missed_tags_and_questions(ev, tmp_path):
    data = tmp_path / "data"
    write_summary(
        data,
        "local",
        "sess-1",
        [
            # Matches the key's fact, but the tag disagrees: it was observed, not stated.
            key_fact("run the full suite before pushing", source="observed"),
            # A question the run recorded as a decision, which the key does not hold.
            key_fact("should the report go out at noon?", kind="decision"),
        ],
    )
    write_key(
        data,
        "sess-1",
        [
            key_fact("run the full suite before pushing"),
            key_fact("the endpoint moved to port 8008", source="observed"),
        ],
    )
    report = ev.score_run(data, "base", "local", empty_synthetic(tmp_path))
    assert report["totals"]["wrong_facts"] == 1
    assert report["totals"]["missed_facts"] == 1
    assert report["totals"]["wrong_tags"] == 1
    assert report["totals"]["question_decisions"] == 1
    assert report["totals"]["prompt_tokens"] == 100
    assert report["totals"]["completion_tokens"] == 20
    session = report["sessions"][0]
    assert session["wrong"][0]["quote"] == "should the report go out at noon?"
    assert session["missed"][0]["quote"] == "the endpoint moved to port 8008"
    assert session["tags"][0] == {
        "statement": "The session settled it.",
        "tag": "source",
        "run": "observed",
        "key": "stated",
    }
    assert session["questions"][0]["quote"] == "should the report go out at noon?"


def test_score_reads_tokens_from_the_calls_of_the_run(ev, tmp_path):
    data = tmp_path / "data"
    path = write_summary(data, "local", "sess-1", [])
    call = {
        "session": "sess-1",
        "answer": {},
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }
    (path / "call-001.json").write_text(json.dumps(call) + "\n", encoding="utf-8")
    write_key(data, "sess-1", [])
    report = ev.score_run(data, "base", "local", empty_synthetic(tmp_path))
    # Tokens are the run's own record, so the scorer reads them from there and adds nothing.
    assert report["totals"]["prompt_tokens"] == 100
    assert report["totals"]["completion_tokens"] == 20


def test_score_refusals_of_a_session_count_in_the_report(ev, tmp_path):
    data = tmp_path / "data"
    write_summary(
        data,
        "local",
        "sess-1",
        [],
        dropped=[
            {
                "statement": "s",
                "kind": "fact",
                "source": "stated",
                "entry": "e1",
                "reason": "the quote is not found",
            }
        ],
        refused_parts=[
            {"first": "e1", "last": "e1", "reason": "the answer does not match its chunk"}
        ],
    )
    write_key(data, "sess-1", [key_fact("the endpoint moved to port 8008")])
    report = ev.score_run(data, "base", "local", empty_synthetic(tmp_path))
    assert report["totals"]["refused_facts"] == 1
    assert report["totals"]["refused_parts"] == 1
    assert report["totals"]["missed_facts"] == 1


def test_a_synthetic_session_scores_against_its_expected_answer(ev, tmp_path):
    data = tmp_path / "data"
    synthetic = tmp_path / "synthetic" / "synthetic-nothing"
    synthetic.mkdir(parents=True)
    (synthetic / "expected.json").write_text(
        json.dumps(
            {
                "session_id": "synthetic-nothing",
                "agent": "claude",
                "case": "a session with nothing worth keeping",
                "facts": [key_fact("run the full suite before pushing")],
            }
        ),
        encoding="utf-8",
    )
    write_summary(data, "local", "synthetic-nothing", [])
    report = ev.score_run(data, "base", "local", tmp_path / "synthetic")
    assert report["sessions"][0]["key_from"] == "expected"
    assert report["totals"]["missed_facts"] == 1


def test_an_unkeyed_session_is_listed_and_not_scored(ev, tmp_path):
    data = tmp_path / "data"
    write_summary(data, "local", "sess-1", [key_fact("run the full suite before pushing")])
    report = ev.score_run(data, "base", "local", empty_synthetic(tmp_path))
    assert report["unkeyed"] == ["sess-1"]
    assert report["totals"]["sessions"] == 0
    assert report["totals"]["facts_kept"] == 0


def test_score_writes_a_dated_report(ev, tmp_path):
    data = tmp_path / "data"
    write_summary(data, "local", "sess-1", [])
    write_key(data, "sess-1", [])
    ev.score_run(data, "base", "local", empty_synthetic(tmp_path))
    reports = list((data / "reports").iterdir())
    assert len(reports) == 1
    assert reports[0].name.endswith("-base-local.json")
    payload = json.loads(reports[0].read_text())
    assert payload["variant"] == "base" and payload["backend"] == "local"


def test_score_scores_a_variant_in_its_own_directory(ev, tmp_path):
    data = tmp_path / "data"
    write_summary(data, "local", "sess-1", [], variant="narrow")
    write_key(data, "sess-1", [key_fact("run the full suite before pushing")])
    report = ev.score_run(data, "narrow", "local", empty_synthetic(tmp_path))
    assert report["variant"] == "narrow"
    assert report["totals"]["missed_facts"] == 1


def test_a_broken_key_is_refused(ev, tmp_path):
    data = tmp_path / "data"
    write_summary(data, "local", "sess-1", [])
    write_summary(data, "local", "sess-1", [])
    synthetic = empty_synthetic(tmp_path)
    write_key(data, "sess-1", [{"quote": " "}])
    with pytest.raises(ev.EvalError, match="quote"):
        ev.load_key(data, "sess-1", synthetic)
    write_key(data, "sess-1", [key_fact("quote held", kind="guesswork")])
    with pytest.raises(ev.EvalError, match="kind"):
        ev.load_key(data, "sess-1", synthetic)


def test_score_refuses_an_unknown_backend(ev, tmp_path):
    with pytest.raises(ev.EvalError, match="unknown backend"):
        ev.score_run(tmp_path / "data", "base", "openai", tmp_path)

"""The eval harness: the digest pipeline runs unmodified over the test set, every raw
answer is saved, the backends agree, and a run scores against a key one pair at a time,
with a held-out split and a checked judge (BUILD-PLAN F54, M16).

The gemini and claude backends are exercised only through a fake command, never the real
CLI. The local backend is exercised through the fake endpoint, never a real one. The
judge is exercised through the same fake endpoint with scripted verdicts.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from aft import config as configmod
from aft import model
from aft.config import Config
from fakes.model_server import MODEL_NAME, FakeEndpoint, Reply

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


def serve_fake(cfg: Config, fake: FakeEndpoint) -> FakeEndpoint:
    """Point the config at this fake endpoint."""
    fake.start()
    path = cfg.home / configmod.CONFIG_NAME
    path.write_text(
        path.read_text(encoding="utf-8")
        + f'\n[model]\nendpoint = "{fake.endpoint}"\nname = "{MODEL_NAME}"\n',
        encoding="utf-8",
    )
    return fake


def serve(cfg: Config, *answers: object) -> FakeEndpoint:
    """Point the config at the fake endpoint, which answers with these objects in order."""
    return serve_fake(cfg, FakeEndpoint(list(answers)))


def verdict(name: str, *differs: str) -> dict:
    """A judge answer in the shape judge.schema.json describes."""
    return {"differs": list(differs), "verdict": name}


def judged(ev, cfg: Config, data: Path, *replies: dict) -> tuple[FakeEndpoint, object]:
    """A judge over the fake endpoint scripted with these verdicts, and the fake.

    Verdicts are cached in the data directory, so a second judge over the same key text
    and run statements makes no calls.
    """
    fake = serve(cfg, *replies)
    judge = ev.Judge(ev.with_model(cfg, fake.endpoint, MODEL_NAME, "{}"), data / ev.JUDGE_CACHE)
    return fake, judge


def judge_requests(fake: FakeEndpoint) -> list[dict]:
    """The calls the fake endpoint saw that were judge calls."""
    return [
        request
        for request in fake.requests
        if "Run statement:" in request["messages"][-1]["content"]
    ]


def run_fact(statement: str, quote: str, source: str = "stated", kind: str = "decision") -> dict:
    """A run fact with an exact statement, for pair-level tests."""
    return {
        "statement": statement,
        "kind": kind,
        "source": source,
        "term": "long",
        "quote": quote,
        "entry": "#1",
        "scope": "project",
    }


def key_turns_file(data: Path, turns: dict) -> Path:
    """Write the per-turn key these turns describe and return its path."""
    data.mkdir(parents=True, exist_ok=True)
    path = data / "key-turns.json"
    path.write_text(json.dumps(turns), encoding="utf-8")
    return path


class RoutedEndpoint(FakeEndpoint):
    """A fake endpoint with one script for digest and dream calls, one for judge calls.

    The F60 scenarios drive the whole pipeline over an ordered digest script, so an
    ordered script alone would hand the judge a dream action. Routing on the user
    message keeps the two apart: the judge is the only caller whose last message
    starts a run statement.
    """

    def __init__(self, digest_replies: list, judge_replies: list) -> None:
        super().__init__([])
        self.digest_replies = [
            reply if isinstance(reply, Reply) else Reply(content=reply) for reply in digest_replies
        ]
        self.judge_replies = [
            reply if isinstance(reply, Reply) else Reply(content=reply) for reply in judge_replies
        ]
        self.digest_calls = 0
        self.judge_calls = 0

    def reply_for(self, call: int, body: dict) -> Reply:
        judge_call = body["messages"][-1]["content"].startswith("Key statement:")
        script = self.judge_replies if judge_call else self.digest_replies
        count = self.judge_calls if judge_call else self.digest_calls
        if judge_call:
            self.judge_calls += 1
        else:
            self.digest_calls += 1
        return script[min(count, len(script) - 1)] if script else Reply()


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
    statement: str | None = None,
) -> dict:
    """One fact of an answer key, as the lead settles it; its statement says what its quote
    says, as a real fact's does."""
    return {
        "statement": statement if statement is not None else f"The session said: {quote}",
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


def test_the_repository_synthetic_sessions_load(ev, cfg):
    sessions = ev.synthetic_sessions(REPO_ROOT / "eval" / "digest" / "synthetic", cfg)
    assert [session.session_id for session in sessions] == [
        "synthetic-made-false",
        "synthetic-nothing",
        "synthetic-pasted-document",
        "synthetic-profile-mixed",
        "synthetic-profile-nonfacts",
        "synthetic-profile-personal",
        "synthetic-question",
        "synthetic-reversed-rule",
        "synthetic-tentative",
    ]
    assert all(session.source == "synthetic" and session.entries for session in sessions)


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
            "proposal": None,
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
    # A CLI cannot be handed a response_format, so the prompt carries the schema itself.
    assert "Answer with exactly one JSON object that satisfies this JSON schema" in prompt
    assert json.dumps(ev.digestmod.digest_schema()) in prompt


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


def test_score_counts_wrong_missed_tags_and_questions(ev, tmp_path, cfg):
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
    fake, judge = judged(ev, cfg, data, verdict("same"), verdict("same"))
    report = ev.score_run(data, "base", "local", empty_synthetic(tmp_path), judge)
    assert report["totals"]["wrong_facts"] == 1
    assert report["totals"]["missed_facts"] == 1
    assert report["totals"]["wrong_tags"] == 1
    assert report["totals"]["question_decisions"] == 1
    assert report["totals"]["narrower"] == 0
    assert report["totals"]["observed"] == 1
    assert report["totals"]["observed_supported"] == 1
    assert report["totals"]["observed_unsupported"] == 0
    assert report["totals"]["prompt_tokens"] == 100
    assert report["totals"]["completion_tokens"] == 20
    assert report["judge_calls"] == 2
    session = report["sessions"][0]
    assert session["wrong"][0]["quote"] == "should the report go out at noon?"
    assert session["missed"][0]["quote"] == "the endpoint moved to port 8008"
    assert session["pairs"][0] == {
        "key": "The session said: run the full suite before pushing",
        "run": "The session said: run the full suite before pushing",
        "verdict": "same",
        "differs": [],
        "unheld": [],
        "split": None,
    }
    assert session["tags"][0] == {
        "statement": "The session said: run the full suite before pushing",
        "tag": "source",
        "run": "observed",
        "key": "stated",
    }
    assert session["questions"][0]["quote"] == "should the report go out at noon?"
    assert (
        report["judge_prompt"]
        == (hashlib.sha256(ev.JUDGE_PROMPT_PATH.read_bytes()).hexdigest()[:16])
    )


def test_score_reads_tokens_from_the_calls_of_the_run(ev, tmp_path, cfg):
    data = tmp_path / "data"
    path = write_summary(data, "local", "sess-1", [])
    call = {
        "session": "sess-1",
        "answer": {},
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }
    (path / "call-001.json").write_text(json.dumps(call) + "\n", encoding="utf-8")
    write_key(data, "sess-1", [])
    _, judge = judged(ev, cfg, data)
    report = ev.score_run(data, "base", "local", empty_synthetic(tmp_path), judge)
    # Tokens are the run's own record, so the scorer reads them from there and adds nothing.
    assert report["totals"]["prompt_tokens"] == 100
    assert report["totals"]["completion_tokens"] == 20


def test_score_refusals_of_a_session_count_in_the_report(ev, tmp_path, cfg):
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
    _, judge = judged(ev, cfg, data)
    report = ev.score_run(data, "base", "local", empty_synthetic(tmp_path), judge)
    assert report["totals"]["refused_facts"] == 1
    assert report["totals"]["refused_parts"] == 1
    assert report["totals"]["missed_facts"] == 1


def test_a_synthetic_session_scores_against_its_expected_answer(ev, tmp_path, cfg):
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
    _, judge = judged(ev, cfg, data)
    report = ev.score_run(data, "base", "local", tmp_path / "synthetic", judge)
    assert report["sessions"][0]["key_from"] == "expected"
    assert report["totals"]["missed_facts"] == 1


def test_an_unkeyed_session_is_listed_and_not_scored(ev, tmp_path, cfg):
    data = tmp_path / "data"
    write_summary(data, "local", "sess-1", [key_fact("run the full suite before pushing")])
    _, judge = judged(ev, cfg, data)
    report = ev.score_run(data, "base", "local", empty_synthetic(tmp_path), judge)
    assert report["unkeyed"] == ["sess-1"]
    assert report["totals"]["sessions"] == 0
    assert report["totals"]["facts_kept"] == 0


def test_score_writes_a_dated_report(ev, tmp_path, cfg):
    data = tmp_path / "data"
    write_summary(data, "local", "sess-1", [])
    write_key(data, "sess-1", [])
    _, judge = judged(ev, cfg, data)
    ev.score_run(data, "base", "local", empty_synthetic(tmp_path), judge)
    reports = list((data / "reports").iterdir())
    assert len(reports) == 1
    assert reports[0].name.endswith("-base-local.json")
    payload = json.loads(reports[0].read_text())
    assert payload["variant"] == "base" and payload["backend"] == "local"


def test_score_scores_a_variant_in_its_own_directory(ev, tmp_path, cfg):
    data = tmp_path / "data"
    write_summary(data, "local", "sess-1", [], variant="narrow")
    write_key(data, "sess-1", [key_fact("run the full suite before pushing")])
    _, judge = judged(ev, cfg, data)
    report = ev.score_run(data, "narrow", "local", empty_synthetic(tmp_path), judge)
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


def test_score_refuses_an_unknown_backend(ev, tmp_path, cfg):
    _, judge = judged(ev, cfg, tmp_path / "data")
    with pytest.raises(ev.EvalError, match="unknown backend"):
        ev.score_run(tmp_path / "data", "base", "openai", tmp_path, judge)


def test_a_run_fact_holding_two_key_items_quotes_answers_both(ev, tmp_path, cfg):
    """One run fact holding two key items' quotes pairs with and answers both."""
    data = tmp_path / "data"
    key_turns_file(
        data,
        {
            "sess-1#1": {"user": "", "durable": [key_fact("deploy happens on port 8008")]},
            "sess-1#2": {
                "user": "",
                "durable": [key_fact("blue runner handles staging")],
            },
        },
    )
    write_summary(
        data,
        "local",
        "sess-1",
        [
            run_fact(
                "deploy happens on port 8008 and blue runner handles staging",
                "deploy happens on port 8008. blue runner handles staging.",
            )
        ],
    )
    fake, judge = judged(ev, cfg, data, verdict("same"), verdict("same"))
    report = ev.score_turns(data, "base", "local", judge)
    assert report["totals"]["key_items"] == 2
    assert report["totals"]["found"] == 2
    assert report["totals"]["missed"] == 0
    assert report["totals"]["stated_wrong"] == 0
    assert len(judge_requests(fake)) == 2
    pairs = report["sessions"][0]["pairs"]
    assert len(pairs) == 2
    assert {p["key"] for p in pairs} == {
        "The session said: deploy happens on port 8008",
        "The session said: blue runner handles staging",
    }


def test_a_key_item_whose_quote_stands_inside_a_longer_run_quote_pairs_under_thresholds(
    ev, tmp_path, cfg
):
    """A key item whose quote stands inside a longer run quote pairs although both ratios
    are under their thresholds."""
    data = tmp_path / "data"
    key_quote = "I'm a FL resident, so no state taxes."
    run_quote = (
        "my primary concern is cost reduction and simplicity. I'm a FL resident, so no"
        " state taxes. We also need to keep the infrastructure minimal."
    )
    key_turns_file(
        data,
        {
            "sess-1#1": {
                "user": "",
                "durable": [
                    key_fact(
                        key_quote,
                        statement="FL resident pays no state taxes",
                    )
                ],
            }
        },
    )
    write_summary(
        data,
        "local",
        "sess-1",
        [
            run_fact(
                "User prioritizes cost reduction, simplicity, minimal infrastructure, and has no"
                " state taxes due to FL residency.",
                run_quote,
            )
        ],
    )
    # Verify that fact_overlap alone is None
    assert (
        ev.fact_overlap(
            {"quote": key_quote, "statement": "FL resident pays no state taxes"},
            {
                "quote": run_quote,
                "statement": (
                    "User prioritizes cost reduction, simplicity, minimal infrastructure, and has"
                    " no state taxes due to FL residency."
                ),
            },
        )
        is None
    )
    fake, judge = judged(ev, cfg, data, verdict("same"))
    report = ev.score_turns(data, "base", "local", judge)
    assert report["totals"]["key_items"] == 1
    assert report["totals"]["found"] == 1
    assert report["totals"]["missed"] == 0
    assert len(judge_requests(fake)) == 1


def test_a_key_item_takes_only_its_strongest_run_fact(ev, tmp_path, cfg):
    """A key item matching several run facts takes only its strongest, leaving weaker ones."""
    data = tmp_path / "data"
    key_turns_file(
        data,
        {
            "sess-1#1": {
                "user": "",
                "durable": [key_fact("deploy happens on port 8008")],
            }
        },
    )
    write_summary(
        data,
        "local",
        "sess-1",
        [
            # Weaker match: containment-only or lower ratio
            run_fact(
                "deploy happens on port 8008 with the blue runner in the secondary cluster",
                "notice: deploy happens on port 8008 with the blue runner in the secondary cluster",
            ),
            # Stronger match: exact fact_overlap
            run_fact(
                "deploy happens on port 8008",
                "deploy happens on port 8008",
            ),
        ],
    )
    fake, judge = judged(ev, cfg, data, verdict("same"))
    report = ev.score_turns(data, "base", "local", judge)
    assert report["totals"]["key_items"] == 1
    assert report["totals"]["found"] == 1
    assert len(judge_requests(fake)) == 1
    pair = report["sessions"][0]["pairs"][0]
    assert pair["run"] == "deploy happens on port 8008"


def test_a_stated_run_fact_paired_only_with_different_verdicts_counts_once_as_wrong(
    ev, tmp_path, cfg
):
    """A [stated] run fact paired with several key items counts as wrong once when all its
    verdicts are different."""
    data = tmp_path / "data"
    key_turns_file(
        data,
        {
            "sess-1#1": {"user": "", "durable": [key_fact("deploy happens on port 8008")]},
            "sess-1#2": {
                "user": "",
                "durable": [key_fact("blue runner handles staging")],
            },
        },
    )
    write_summary(
        data,
        "local",
        "sess-1",
        [
            run_fact(
                "deploy happens on port 9999 and green runner handles staging",
                "deploy happens on port 8008. blue runner handles staging.",
                source="stated",
            )
        ],
    )
    fake, judge = judged(
        ev,
        cfg,
        data,
        verdict("different", "port 9999"),
        verdict("different", "green runner"),
    )
    report = ev.score_turns(data, "base", "local", judge)
    assert report["totals"]["key_items"] == 2
    assert report["totals"]["found"] == 0
    assert report["totals"]["missed"] == 2
    assert report["totals"]["stated"] == 1
    assert report["totals"]["stated_wrong"] == 1
    wrong = report["sessions"][0]["wrong"]
    assert wrong == ["deploy happens on port 9999 and green runner handles staging"]


def test_match_facts_still_pairs_one_to_one(ev):
    """match_facts retains one-to-one pairing across lists."""
    key = [key_fact("deploy happens on port 8008"), key_fact("deploy on port 8008")]
    run = [run_fact("deploy happens on port 8008", "deploy happens on port 8008")]
    groups = ev.match_facts({"key": key, "run": run})
    paired = [group for group in groups if len(group.facts) == 2]
    assert len(paired) == 1
    assert len(groups) == 2


def test_a_stated_run_fact_with_one_same_verdict_is_not_wrong(ev, tmp_path, cfg):
    """A stated run fact paired with several key items is not wrong if at least one pair is
    judged same."""
    data = tmp_path / "data"
    key_turns_file(
        data,
        {
            "sess-1#1": {"user": "", "durable": [key_fact("deploy happens on port 8008")]},
            "sess-1#2": {
                "user": "",
                "durable": [key_fact("blue runner handles staging")],
            },
        },
    )
    write_summary(
        data,
        "local",
        "sess-1",
        [
            run_fact(
                "deploy happens on port 8008 and green runner handles staging",
                "deploy happens on port 8008. blue runner handles staging.",
                source="stated",
            )
        ],
    )
    fake, judge = judged(
        ev,
        cfg,
        data,
        verdict("same"),
        verdict("different", "green runner"),
    )
    report = ev.score_turns(data, "base", "local", judge)
    assert report["totals"]["key_items"] == 2
    assert report["totals"]["found"] == 1
    assert report["totals"]["missed"] == 1
    assert report["totals"]["stated"] == 1
    assert report["totals"]["stated_wrong"] == 0
    assert report["sessions"][0]["wrong"] == []


def test_a_right_quote_under_a_wrong_statement_is_wrong(ev, tmp_path, cfg):
    """A run fact citing the key's quote but naming another number is wrong: the judge
    decides, and the number neither text holds is recorded as unheld."""
    data = tmp_path / "data"
    key_turns_file(
        data, {"sess-1#1": {"user": "", "durable": [key_fact("the api listens on 8008")]}}
    )
    write_summary(
        data, "local", "sess-1", [run_fact("the api listens on 9999", "the api listens on 8008")]
    )
    fake, judge = judged(ev, cfg, data, verdict("different", "9999"))
    report = ev.score_turns(data, "base", "local", judge)
    assert report["totals"]["found"] == 0
    assert report["totals"]["missed"] == 1
    assert report["totals"]["stated_wrong"] == 1
    assert len(judge_requests(fake)) == 1
    pair = report["sessions"][0]["pairs"][0]
    assert pair["verdict"] == "different" and pair["unheld"] == ["9999"]


def test_a_broader_statement_counts_found_and_its_own_quote_grounds_added_names(ev, tmp_path, cfg):
    """A run statement holding the key's claim and a true detail beside it is found and
    counted broader; a name its own verified quote holds is not recorded as unheld."""
    data = tmp_path / "data"
    key_turns_file(
        data,
        {"sess-1#1": {"user": "", "durable": [key_fact("only the replica moved to db-03")]}},
    )
    write_summary(
        data,
        "local",
        "sess-1",
        [
            run_fact(
                "Staging is still on db-02; only the replica moved to db-03.",
                "staging is still on db-02. only the replica moved to db-03",
            )
        ],
    )
    fake, judge = judged(ev, cfg, data, verdict("broader", "staging is still on db-02"))
    report = ev.score_turns(data, "base", "local", judge)
    assert report["totals"]["found"] == 1
    assert report["totals"]["broader"] == 1
    assert report["totals"]["stated_wrong"] == 0
    assert len(judge_requests(fake)) == 1
    assert report["sessions"][0]["pairs"][0]["unheld"] == []


def test_an_approval_s_proposal_words_ground_its_statement(ev, tmp_path, cfg):
    """A name an approval took from the proposal it answers is held by the proposal's
    words, so it is not recorded as unheld."""
    data = tmp_path / "data"
    key_turns_file(data, {"sess-1#1": {"user": "", "durable": [key_fact("and the guardrail")]}})
    fact = run_fact("Add the guardrail line to captain.md.", "and the guardrail")
    fact["proposal"] = ["entry-1", "Add a line to captain.md saying not to push."]
    write_summary(data, "local", "sess-1", [fact])
    fake, judge = judged(ev, cfg, data, verdict("broader", "captain.md"))
    report = ev.score_turns(data, "base", "local", judge)
    assert report["totals"]["found"] == 1
    assert report["sessions"][0]["pairs"][0]["unheld"] == []


def test_a_statement_the_judge_rejects_scores_wrong(ev, tmp_path, cfg):
    """A run fact the name check cannot fault is judged, and its verdict decides."""
    data = tmp_path / "data"
    key_turns_file(data, {"sess-1#1": {"user": "", "durable": [key_fact("never push to main")]}})
    write_summary(
        data,
        "local",
        "sess-1",
        [run_fact("The session said: push to main when the tests pass", "never push to main")],
    )
    fake, judge = judged(ev, cfg, data, verdict("different", "the session says push freely"))
    report = ev.score_turns(data, "base", "local", judge)
    assert report["totals"]["found"] == 0
    assert report["totals"]["missed"] == 1
    assert report["totals"]["stated_wrong"] == 1
    assert len(judge_requests(fake)) == 1
    pair = report["sessions"][0]["pairs"][0]
    assert pair["verdict"] == "different"


def test_a_narrower_verdict_counts_without_a_tag_check(ev, tmp_path, cfg):
    """A pair judged narrower counts as found and skips the tag comparison."""
    data = tmp_path / "data"
    key_turns_file(data, {"sess-1#1": {"user": "", "durable": [key_fact("run the full suite")]}})
    write_summary(
        data,
        "local",
        "sess-1",
        [key_fact("run the full suite", source="observed")],
    )
    fake, judge = judged(ev, cfg, data, verdict("narrower", "the run only names the suite"))
    report = ev.score_turns(data, "base", "local", judge)
    assert report["totals"]["found"] == 1
    assert report["totals"]["narrower"] == 1
    assert report["totals"]["stated_wrong"] == 0


def test_a_cached_verdict_makes_no_call(ev, tmp_path, cfg):
    """A second scoring pass over the same statements reads the verdicts from the cache."""
    data = tmp_path / "data"
    key_turns_file(data, {"sess-1#1": {"user": "", "durable": [key_fact("use port 8008")]}})
    write_summary(data, "local", "sess-1", [key_fact("use port 8008")])
    fake, judge = judged(ev, cfg, data, verdict("same"))
    first = ev.score_turns(data, "base", "local", judge)
    assert judge.calls == 1
    again = ev.Judge(ev.with_model(cfg, fake.endpoint, MODEL_NAME, "{}"), data / ev.JUDGE_CACHE)
    second = ev.score_turns(data, "base", "local", again)
    assert again.calls == 0
    assert second["totals"]["found"] == first["totals"]["found"]


def test_an_observed_fact_is_judged_against_its_own_quote(ev, tmp_path, cfg):
    """Recall the run itself witnessed is judged against the quote it cites, not the key."""
    data = tmp_path / "data"
    key_turns_file(data, {"sess-1#1": {"user": "", "durable": [key_fact("use port 8008")]}})
    write_summary(
        data,
        "local",
        "sess-1",
        [
            run_fact(
                "The test run failed with three errors",
                "FAILED tests/test_api.py::test_upload - assert 3 == 0",
                source="observed",
                kind="fact",
            ),
            run_fact(
                "The test run failed in tests/test_api.py",
                "FAILED tests/test_api.py::test_upload - assert 3 == 0",
                source="observed",
                kind="fact",
            ),
        ],
    )
    fake, judge = judged(ev, cfg, data, verdict("different", "three errors"), verdict("same"))
    report = ev.score_turns(data, "base", "local", judge)
    assert report["totals"]["observed"] == 2
    assert report["totals"]["observed_supported"] == 1
    assert report["totals"]["observed_unsupported"] == 1
    request = judge_requests(fake)[0]
    assert "Key statement: (none)" in request["messages"][-1]["content"]
    assert (
        "FAILED tests/test_api.py::test_upload - assert 3 == 0"
        in request["messages"][-1]["content"]
    )


def test_the_turns_report_holds_the_hashes_model_and_temperature(ev, tmp_path, cfg):
    """The report records what was judged with, so a rerun can be compared line by line."""
    data = tmp_path / "data"
    key_turns_file(data, {"sess-1#1": {"user": "", "durable": [key_fact("use port 8008")]}})
    write_summary(data, "local", "sess-1", [key_fact("use port 8008")])
    _, judge = judged(ev, cfg, data, verdict("same"))
    report = ev.score_turns(data, "base", "local", judge)
    digest = hashlib.sha256(ev.JUDGE_PROMPT_PATH.read_bytes()).hexdigest()[:16]
    assert report["judge_prompt"] == digest
    assert re.fullmatch(r"[0-9a-f]{16}", report["digest_prompts"])
    assert report["model"] == MODEL_NAME
    assert report["temperature"] == 0.0
    assert report["prompt_shapes"] == {"local": "endpoint-schema"}


def test_both_splits_are_reported_for_the_per_turn_key(ev, tmp_path, cfg):
    """A frozen split reports the key-side numbers for dev and held-out separately."""
    data = tmp_path / "data"
    key_turns_file(
        data,
        {
            "sess-1#1": {"user": "", "durable": [key_fact("use port 8008")]},
            "sess-1#2": {"user": "", "durable": [key_fact("never push to main")]},
        },
    )
    write_summary(
        data,
        "local",
        "sess-1",
        [key_fact("use port 8008"), key_fact("never push to main")],
    )
    split_path = ev.build_split(data)
    assert split_path == data / "split.json"
    fake, judge = judged(ev, cfg, data, verdict("same"), verdict("same"))
    report = ev.score_turns(data, "base", "local", judge)
    turns = report["split"]["turns"]
    assert turns["dev"] + turns["held-out"] == 2
    assert set(report["splits"]) == {"dev", "held-out", "unsplit"}
    buckets = report["splits"]
    assert buckets["dev"]["key_facts"] + buckets["held-out"]["key_facts"] == 2
    assert buckets["dev"]["found"] + buckets["held-out"]["found"] == 2
    assert buckets["unsplit"]["key_facts"] == 0


def test_score_reports_the_split_of_its_key_facts(ev, tmp_path, cfg):
    """The per-session key reports its facts' splits too, through the per-turn key."""
    data = tmp_path / "data"
    write_summary(data, "local", "sess-1", [key_fact("use port 8008")])
    write_key(data, "sess-1", [key_fact("use port 8008")])
    key_turns_file(data, {"sess-1#1": {"user": "", "durable": [key_fact("use port 8008")]}})
    ev.build_split(data)
    _, judge = judged(ev, cfg, data, verdict("same"))
    report = ev.score_run(data, "base", "local", empty_synthetic(tmp_path), judge)
    buckets = report["splits"]
    assert sum(bucket["key_facts"] for bucket in buckets.values()) == 1
    assert sum(bucket["found"] for bucket in buckets.values()) == 1
    assert buckets["unsplit"]["key_facts"] == 0


def test_split_refuses_to_overwrite_and_rebuilds_identically(ev, tmp_path):
    """A frozen split is only rewritten with --force, and the rule rebuilds the same."""
    data = tmp_path / "data"
    key_turns_file(
        data,
        {
            "sess-1#1": {"user": "", "durable": [key_fact("one")]},
            "sess-1#2": {"user": "", "durable": [key_fact("two")]},
            "sess-1#3": {"user": "", "durable": [key_fact("three")]},
        },
    )
    path = ev.build_split(data)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["rule"] == ev.SPLIT_RULE
    with pytest.raises(ev.EvalError, match="frozen"):
        ev.build_split(data)
    again = ev.build_split(data, force=True)
    assert json.loads(again.read_text(encoding="utf-8"))["turns"] == payload["turns"]
    assert set(payload["turns"].values()) <= {"dev", "held-out"}


def test_a_split_naming_an_unknown_side_is_refused(ev, tmp_path):
    """A split file may only name dev and held-out."""
    data = tmp_path / "data"
    key_turns_file(data, {"sess-1#1": {"user": "", "durable": [key_fact("one")]}})
    (data / "split.json").write_text(
        json.dumps({"built": NOW, "rule": "odd", "turns": {"sess-1#1": "training"}}),
        encoding="utf-8",
    )
    with pytest.raises(ev.EvalError, match="dev"):
        ev.load_split(data)


def test_judge_check_passes_and_fails_on_the_bars(ev, tmp_path, cfg):
    """The check passes when the planted wrong pairs are caught and the same and broader
    pairs hold."""
    data = tmp_path / "data"
    data.mkdir(parents=True)
    check = {
        "wrong": [
            {
                "statement": "The session said: retry twice",
                "quote": "retry twice",
                "planted": "twice",
            }
        ],
        "same": [
            {
                "statement": "The session said: retry twice",
                "quote": "retry twice",
                "planted": "the session says to try the task two times",
            }
        ],
        "broader": [
            {
                "statement": "The session said: retry twice",
                "quote": "retry twice",
                "planted": "the session says to retry twice, with a pause between tries",
            }
        ],
    }
    (data / ev.JUDGE_CHECK).write_text(json.dumps(check), encoding="utf-8")

    caught = run_judge_check(
        ev, cfg, data, verdict("different"), verdict("same"), verdict("broader")
    )
    assert caught == 0
    # The verdict cache would answer the second run from the first, so drop it.
    (data / ev.JUDGE_CACHE).unlink()
    missed = run_judge_check(ev, cfg, data, verdict("same"), verdict("same"), verdict("broader"))
    assert missed == 1
    (data / ev.JUDGE_CACHE).unlink()
    # A superset the judge calls different fails the broader bar.
    strict = run_judge_check(
        ev, cfg, data, verdict("different"), verdict("same"), verdict("different")
    )
    assert strict == 1


def run_judge_check(ev, cfg: Config, data: Path, *replies: dict) -> int:
    """Run the judge-check command against a fake endpoint scripted with these verdicts.

    The endpoint is given on the command line, so the check can run twice in one test
    without rewriting the config.
    """
    fake = FakeEndpoint(list(replies))
    fake.start()
    return ev.main(
        [
            "judge-check",
            "--data",
            str(data),
            "--endpoint",
            fake.endpoint,
            "--model",
            MODEL_NAME,
        ]
    )


def test_judge_check_refuses_bad_planted_pairs(ev, tmp_path):
    """The check needs at least one planted wrong pair and one planted same pair."""
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / ev.JUDGE_CHECK).write_text(json.dumps({"wrong": [], "same": []}), encoding="utf-8")
    with pytest.raises(ev.EvalError, match="wrong"):
        ev.load_judge_check(data)
    (data / ev.JUDGE_CHECK).write_text(
        json.dumps({"wrong": [{"statement": "s", "quote": "q", "planted": "p"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ev.EvalError, match="same"):
        ev.load_judge_check(data)


def test_two_quotes_of_one_fact_are_matched_by_what_the_statements_say(ev):
    """Models quote different lines for one fact; the statements still pair them."""
    local = {
        "statement": "Greptile is the automated reviewer whose findings gate every firstmate PR.",
        "quote": "greptile flagged two P1 issues on the PR",
    }
    claude = {
        "statement": "Firstmate PRs are gated by Greptile's automated reviewer findings.",
        "quote": "wait for the greptile check before merging",
    }
    other = {"statement": "The user prefers small commits.", "quote": "keep commits small"}
    groups = ev.match_facts({"local": [local, other], "claude": [claude]})
    paired = [group for group in groups if len(group.facts) == 2]
    assert len(paired) == 1 and paired[0].facts == {"local": local, "claude": claude}


def test_a_variant_named_after_a_pipeline_runs_that_pipeline(ev, cfg):
    assert ev.with_pipeline(cfg, "turns").settings.digest.pipeline == "turns"
    assert ev.with_pipeline(cfg, "base").settings.digest.pipeline == "base"
    assert ev.with_pipeline(cfg, "trial-3") is cfg


def test_score_turns_counts_key_items_found_and_stated_facts_the_key_lacks(ev, tmp_path, cfg):
    data = tmp_path / "data"
    key_turns_file(
        data,
        {
            "sess-1#1": {"user": "", "durable": [key_fact("use port 8008")]},
            "sess-1#2": {"user": "", "durable": [key_fact("never push to main")]},
        },
    )
    write_summary(
        data,
        "local",
        "sess-1",
        [key_fact("use port 8008"), key_fact("should the report go out at noon?")],
    )
    fake, judge = judged(ev, cfg, data, verdict("same"))
    report = ev.score_turns(data, "base", "local", judge)
    assert report["totals"] == {
        "sessions": 1,
        "key_items": 2,
        "found": 1,
        "narrower": 0,
        "broader": 0,
        "missed": 1,
        "stated": 2,
        "stated_wrong": 1,
        "observed": 0,
        "observed_supported": 0,
        "observed_narrower": 0,
        "observed_broader": 0,
        "observed_unsupported": 0,
    }
    assert len(judge_requests(fake)) == 1
    pair = report["sessions"][0]["pairs"][0]
    assert pair["verdict"] == "same" and pair["unheld"] == []


# ---------------------------------------------------------------------------
# F60: current truth evaluation (BUILD-PLAN F60)


def test_score_block_on_handmade_blocks(ev, tmp_path, cfg):
    """A value is present when a line holds its tokens and the judge agrees with it."""
    current = [ev.ScenarioValue("Reply 'settle' to go through notes now.", ("settle",))]
    superseded = [ev.ScenarioValue("Reply 'yes' to go through notes.", ("yes",))]
    block = (
        "# afterthought\n\n"
        "## Decisions\n"
        "- Reply 'settle' to go through notes now. [stated] 2026-10-02\n"
    )

    # The line is not a substring of the value: tokens do the finding and the judge
    # decides, so wording differences between value and block line cannot fail it.
    fake, judge = judged(ev, cfg, tmp_path / "data", verdict("same"))
    score = ev.score_block(block, current, superseded, judge)
    assert score["current_present"] is True
    assert score["superseded_absent"] is True
    assert score["contradiction"] is False
    assert score["passed"] is True
    assert score["current"][0]["line"] == (
        "- Reply 'settle' to go through notes now. [stated] 2026-10-02"
    )
    (request,) = judge_requests(fake)
    # The line is the key and the value is judged against it, so a line that holds the
    # value and more reads as narrower, not as a difference.
    shown = request["messages"][-1]["content"]
    assert "Key statement: - Reply 'settle' to go through notes now. [stated]" in shown
    assert "Run statement: Reply 'settle' to go through notes now." in shown

    # A line holding the superseded value's tokens that the judge confirms is a
    # contradiction, no matter how the words are arranged.
    contradicted = block + "- Reply 'yes' to go through notes when asked again\n"
    fake_bad, judge_bad = judged(
        ev, cfg, tmp_path / "data-contradicted", verdict("same"), verdict("same")
    )
    score_bad = ev.score_block(contradicted, current, superseded, judge_bad)
    assert score_bad["current_present"] is True
    assert score_bad["superseded_absent"] is False
    assert score_bad["contradiction"] is True
    assert score_bad["passed"] is False
    assert score_bad["superseded"][0]["line"] == (
        "- Reply 'yes' to go through notes when asked again"
    )
    assert len(judge_requests(fake_bad)) == 2

    # A missing current value fails the scenario.
    _, judge_missing = judged(ev, cfg, tmp_path / "data-missing", verdict("same"))
    score_missing = ev.score_block("## Decisions\n", current, superseded, judge_missing)
    assert score_missing["current_present"] is False
    assert score_missing["superseded_absent"] is True
    assert score_missing["contradiction"] is False
    assert score_missing["passed"] is False

    # When there is no superseded value to check, absence is always satisfied.
    score_no_sup = ev.score_block(block, current, [], judge_missing)
    assert score_no_sup["current_present"] is True
    assert score_no_sup["superseded_absent"] is True
    assert score_no_sup["passed"] is True


def test_load_scenarios_from_repo(ev):
    """All 5 shipped scenarios in eval/current load with valid sessions and expectations."""
    scenarios = ev.load_scenarios(ev.DEFAULT_CURRENT_SCENARIOS)
    assert len(scenarios) == 5
    names = {s.name for s in scenarios}
    assert names == {
        "reversed-preference",
        "paused-then-resumed",
        "expired-temporary-setting",
        "replaced-rule",
        "restated-fact",
    }
    by_name = {s.name: s for s in scenarios}
    assert by_name["replaced-rule"].current[0].tokens == ("settle",)
    assert by_name["replaced-rule"].superseded[0].tokens == ("yes",)
    assert by_name["restated-fact"].current[0].tokens == ("retention", "90")
    assert by_name["restated-fact"].superseded == ()
    assert by_name["reversed-preference"].current[0].tokens == ("capitalized",)
    assert by_name["reversed-preference"].superseded[0].tokens == ("lowercase",)
    selected = ev.load_scenarios(ev.DEFAULT_CURRENT_SCENARIOS, wanted=["reversed-preference"])
    assert len(selected) == 1
    assert selected[0].name == "reversed-preference"

    with pytest.raises(ev.EvalError, match="unknown scenario"):
        ev.load_scenarios(ev.DEFAULT_CURRENT_SCENARIOS, wanted=["nonexistent"])


def test_a_scenario_value_without_tokens_is_refused(ev, tmp_path):
    """A current value with no distinguishing tokens cannot be scored, so it is refused."""
    directory = tmp_path / "scenarios" / "thin"
    directory.mkdir(parents=True)
    (directory / "scenario.json").write_text(
        json.dumps(
            {
                "name": "thin",
                "description": "a value with no tokens",
                "current": ["just a phrase"],
                "superseded": [],
                "sessions": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ev.EvalError, match="tokens"):
        ev.load_scenarios(tmp_path / "scenarios")


def test_current_refuses_real_home_and_vault(ev, monkeypatch):
    """The current command enforces the sandbox checks and refuses real paths."""
    real = str(Path("~/.afterthought").expanduser().resolve())
    monkeypatch.setenv(configmod.HOME_ENV, real)
    with pytest.raises(ev.EvalError, match="refusing"):
        ev.load_config()


def test_current_runs_scenario_with_fake_endpoint(ev, tmp_path, cfg):
    """Running current over one scenario drives capture, digest, dream, and inject."""

    def said_item(statement: str, quote: str, entry: str) -> dict:
        return {
            "statement": statement,
            "kind": "preference",
            "source": "stated",
            "term": "long",
            "quote": quote,
            "entry": entry,
            "scope": "global",
        }

    def digest_answer(*facts: dict) -> dict:
        return {
            "summary": "Updated commit style.",
            "handoff": {"in_flight": {"text": "", "entry": ""}, "failed": [], "open": []},
            "facts": list(facts),
        }

    script = [
        digest_answer(
            said_item(
                "Commit messages must be lowercase imperative.",
                "always write them in lowercase imperative style",
                "rp1-u1",
            )
        ),
        {"action": "add", "line": None, "reason": "stated preference"},
        digest_answer(
            said_item(
                "Commit messages must start with a capitalized imperative verb.",
                "commit messages must start with a capitalized imperative verb",
                "rp2-u1",
            )
        ),
        {"action": "supersede", "line": "L1", "reason": "reversed preference"},
    ]
    with RoutedEndpoint(script, [verdict("same")]) as fake:
        scenarios = ev.load_scenarios(ev.DEFAULT_CURRENT_SCENARIOS, wanted=["reversed-preference"])
        assert len(scenarios) == 1
        backend = ev.Backend("local")
        config = ev.with_model(cfg, fake.endpoint, "fake-model", "{}")
        judge = ev.Judge(config, tmp_path / "judge-cache.json")
        report = ev.run_scenarios(scenarios, config, backend, judge, variant="base")

        assert report["totals"]["scenarios"] == 1
        assert report["totals"]["current_present"] == 1
        assert report["totals"]["superseded_absent"] == 1
        assert report["totals"]["contradictions"] == 0
        assert report["totals"]["passed"] == 1
        res = report["scenarios"][0]
        assert res["current_present"] is True
        assert res["superseded_absent"] is True
        assert res["contradiction"] is False
        assert res["passed"] is True


def test_current_cli_command_with_report_file(ev, tmp_path, cfg):
    """The current CLI command runs and writes the JSON report file when asked."""

    def said_item(statement: str, quote: str, entry: str) -> dict:
        return {
            "statement": statement,
            "kind": "preference",
            "source": "stated",
            "term": "long",
            "quote": quote,
            "entry": entry,
            "scope": "global",
        }

    def digest_answer(*facts: dict) -> dict:
        return {
            "summary": "Updated commit style.",
            "handoff": {"in_flight": {"text": "", "entry": ""}, "failed": [], "open": []},
            "facts": list(facts),
        }

    script = [
        digest_answer(
            said_item(
                "Commit messages must be lowercase imperative.",
                "always write them in lowercase imperative style",
                "rp1-u1",
            )
        ),
        {"action": "add", "line": None, "reason": "stated preference"},
        digest_answer(
            said_item(
                "Commit messages must start with a capitalized imperative verb.",
                "commit messages must start with a capitalized imperative verb",
                "rp2-u1",
            )
        ),
        {"action": "supersede", "line": "L1", "reason": "reversed preference"},
    ]
    report_file = tmp_path / "reports" / "current-test.json"
    with RoutedEndpoint(script, [verdict("same")]) as fake:
        code = ev.main(
            [
                "current",
                "--backend",
                "local",
                "--endpoint",
                fake.endpoint,
                "--variant",
                "base",
                "--scenario",
                "reversed-preference",
                "--report",
                str(report_file),
            ]
        )
        assert code == 0
        assert report_file.is_file()
        data = json.loads(report_file.read_text(encoding="utf-8"))
        assert data["command"] == "current"
        assert data["totals"]["scenarios"] == 1
        assert data["totals"]["passed"] == 1
        assert data["totals"]["contradiction_rate"] == 0.0
        assert data["totals"]["current_rate"] == 1.0


def test_current_print_report(ev, capsys):
    """The human-readable report shows per scenario and overall rates."""
    report = {
        "command": "current",
        "backend": "local",
        "model": "fake-model",
        "totals": {
            "scenarios": 2,
            "current_present": 2,
            "current_rate": 1.0,
            "superseded_absent": 2,
            "contradictions": 0,
            "contradiction_rate": 0.0,
            "passed": 2,
        },
        "scenarios": [
            {
                "scenario": "reversed-preference",
                "current_present": True,
                "superseded_absent": True,
                "contradiction": False,
                "passed": True,
            },
            {
                "scenario": "paused-then-resumed",
                "current_present": True,
                "superseded_absent": True,
                "contradiction": False,
                "passed": True,
            },
        ],
    }
    ev.print_current_report(report)
    out = capsys.readouterr().out
    assert "local (fake-model) - Current truth evaluation:" in out
    assert "reversed-preference: current=yes, superseded=absent -> PASS" in out
    assert "paused-then-resumed: current=yes, superseded=absent -> PASS" in out
    assert "2/2 current present (100.0%)" in out
    assert "0/2 contradictions (0.0% contradiction rate)" in out

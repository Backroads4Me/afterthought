"""The outcome eval's parsing and scoring (BUILD-PLAN T59)."""

import json

from eval import outcome


def _events(*messages):
    return [json.dumps({"type": "agent_end", "messages": list(messages)})]


def _assistant(*content, tokens=(10, 5, 2)):
    usage = dict(zip(("input", "cacheRead", "output"), tokens, strict=True))
    return {"role": "assistant", "content": list(content), "usage": usage}


def _call(name, **arguments):
    return {"type": "toolCall", "name": name, "arguments": arguments}


def test_tokens_sum_turns_and_the_last_text_is_the_answer():
    run = outcome.parse_run(
        _events(
            _assistant({"type": "text", "text": "looking"}, _call("read", path="a.py")),
            _assistant({"type": "text", "text": " done "}, tokens=(1, 20, 3)),
        )
    )
    assert run["answer"] == "done"
    assert (run["tokens"], run["uncached"], run["turns"]) == (41, 11, 2)
    assert run["tools"] == ["read"]
    assert run["void"] == []


def test_a_run_that_uses_another_tool_is_void():
    run = outcome.parse_run(_events(_assistant(_call("subagent_run", task="go"))))
    assert run["void"] == ["tool subagent_run"]


def test_an_agents_own_memory_search_is_counted_and_not_void():
    run = outcome.parse_run(_events(_assistant(_call("bash", command='aft search "review"'))))
    assert run["memory_calls"] == 1
    assert run["void"] == []


def test_a_stream_with_no_end_is_void():
    assert outcome.parse_run(["not json"])["void"] == ["no agent_end"]


def _record(qid, kind, arm, answer, void=(), tokens=100):
    return {
        "id": qid,
        "kind": kind,
        "arm": arm,
        "answer": answer,
        "void": list(void),
        "tokens": tokens,
        "uncached": 1,
        "turns": 1,
        "seconds": 1,
    }


def test_partial_counts_half_and_an_empty_answer_counts_nothing():
    q = {"id": "a", "kind": "memory", "reference": "r"}
    verdicts = {outcome._judge_key(q, "x"): {"verdict": "partial"}}
    records = [_record("a", "memory", "with", "x"), _record("a", "memory", "without", "")]
    kinds = outcome.score(records, verdicts, [q])["kinds"]
    assert kinds["memory"]["with"]["rate"] == 0.5
    assert kinds["memory"]["without"]["rate"] == 0.0


def test_a_question_over_the_void_share_leaves_both_arms():
    q = {"id": "a", "kind": "repo", "reference": "r"}
    verdicts = {outcome._judge_key(q, "x"): {"verdict": "correct"}}
    records = [
        _record("a", "repo", "with", "x", void=["timed out"]),
        _record("a", "repo", "without", "x"),
    ]
    result = outcome.score(records, verdicts, [q])
    assert result["dropped"] == ["a"]
    assert result["kinds"]["repo"]["without"]["runs"] == 0


def _kinds(**rates):
    return {
        kind: {
            "with": {"rate": rate[0], "tokens": rate[2]},
            "without": {"rate": rate[1], "tokens": rate[3]},
        }
        for kind, rate in rates.items()
    }


def test_the_rule_reads_each_kind_against_its_own_limit():
    held = _kinds(
        memory=(0.9, 0.2, 100, 100),
        repo=(0.9, 0.9, 90, 100),
        control=(0.9, 0.9, 125, 100),
        stale=(0.5, 0.6, 100, 100),
    )
    assert all(part[1] for part in outcome.rule(held))
    missed = _kinds(
        memory=(0.5, 0.2, 100, 100),
        repo=(0.9, 0.9, 100, 100),
        control=(0.9, 0.9, 126, 100),
        stale=(0.4, 0.6, 100, 100),
    )
    assert not any(part[1] for part in outcome.rule(missed))


def test_only_the_with_arm_keeps_the_extension_and_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(outcome.Path, "home", lambda: tmp_path / "home")
    agent = tmp_path / "home" / ".pi" / "agent"
    for path in ("extensions/afterthought/index.ts", "skills/aft/SKILL.md", "sessions/a.jsonl"):
        (agent / path).parent.mkdir(parents=True)
        (agent / path).write_text("x")
    (agent / "auth.json").write_text("{}")
    (agent / "settings.json").write_text("{}")
    outcome.copy_pi(tmp_path)
    assert (tmp_path / "pi-with" / "extensions" / "afterthought" / "index.ts").exists()
    assert not (tmp_path / "pi-without" / "extensions" / "afterthought").exists()
    assert not (tmp_path / "pi-without" / "skills" / "aft").exists()
    for arm in ("with", "without"):
        assert (tmp_path / f"pi-{arm}" / "settings.json").exists()
        assert not (tmp_path / f"pi-{arm}" / "sessions").exists()
        assert not (tmp_path / f"pi-{arm}" / "auth.json").exists()

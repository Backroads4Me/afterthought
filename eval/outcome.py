#!/usr/bin/env python3
"""Does memory change what a cold agent does (BUILD-PLAN T59).

The same questions go to a cold pi agent with memory and without. ``setup`` builds a
workdir: a clone of each project, and for the ``with`` arm a sandbox home and a copy of the
vault at one commit with each clone's path registered. It proves each question before any
run: a ``check`` command holds in the clone, ``repo_absent`` matches nothing the clone
tracks, and ``vault_absent`` matches nothing in the copied vault outside its library.

``run`` starts one ``pi -p`` per question, arm and repeat, each in its own linked worktree
of the clone. Every run is inside ``bwrap`` with the home directory emptied, since an agent
that cannot answer goes looking on the machine, and what it finds there is neither arm. The
home holds only what pi and aft need to start. The ``with`` arm has the afterthought
extension and skill, so the real hooks fire against the sandbox vault, and no dream runs
there, so the vault stays as ``setup`` left it. The ``without`` arm has neither, which is an
agent with no memory installed. ``judge`` puts each answer to the model with no sign of its
arm, in shuffled order, and ``report`` writes ``report.md`` and applies the rule set in
BUILD-PLAN T59.

The questions file holds real project names and stays outside the repository. It is YAML:
``repos`` maps a project slug to its local path, and ``questions`` lists ``id``,
``project``, ``kind`` (one of ``KINDS``), ``q``, ``reference``, and the optional proofs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from aft import config as configmod
from aft import model
from aft.registry import Registry, path_key

KINDS = ("repo", "memory", "stale", "control")
ARMS = ("with", "without")
# read and the search tools cover what a question asks; a subagent or an ask-the-user tool
# would leave the sandbox or wait on a closed stdin.
TOOLS = ("read", "bash", "ffgrep", "fffind", "grep")
RUN_SECONDS = 300
ASK = "{q}\n\nAnswer in one or two sentences. Do not change anything."
VOID_SHARE = 0.2
# Under the emptied home: what pi, node and aft need to start, read-only.
HOME_BINDS = (".local/bin", ".local/lib", ".local/share/uv", ".nvm", "src/afterthought/src")
# pi-lens leaves this in the working directory on every start.
PROBE_DIR = ".pi-lens-probe-home"
PI_LEFT_BEHIND = (
    "sessions",
    "npm",
    "fff",
    "pi-cache-optimizer-stats.d",
    "auth.json",
    "mcp-auth.json",
    "crashes.json",
    "*.bak",
)
CREDIT = {"correct": 1.0, "partial": 0.5, "wrong": 0.0, "no_answer": 0.0}

JUDGE_SYSTEM = """You grade an answer to a question about a software project.

You receive the question, a reference answer known to be true, and the answer given. You
see nothing about who gave it or how.

Give one verdict:
- "correct": the answer says what the reference says, in the same words or other ones.
  Extra detail is fine unless it contradicts the reference.
- "partial": the answer gets part of the reference right and contradicts none of it.
- "wrong": the answer contradicts the reference or says something else in its place.
- "no_answer": the answer says it could not find out, or gives no answer.

Give a one-sentence "reason"."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(CREDIT)},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


def load_questions(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    ids = [q["id"] for q in data["questions"]]
    if len(set(ids)) != len(ids):
        raise SystemExit("question ids repeat")
    for q in data["questions"]:
        if q["kind"] not in KINDS:
            raise SystemExit(f"{q['id']}: unknown kind {q['kind']}")
        if q["project"] not in data["repos"]:
            raise SystemExit(f"{q['id']}: no repo for {q['project']}")
    return data


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _config_text(source: Path, vault: Path) -> str:
    """``source``'s config, pointed at ``vault``: both arms run the live settings."""
    text = source.read_text(encoding="utf-8")
    return re.sub(r"^vault = .*$", f'vault = "{vault}"', text, count=1, flags=re.M)


def _copy_vault(vault: Path, rev: str, target: Path) -> None:
    """Write ``vault`` as it stood at ``rev`` into ``target``, a git repository of its own."""
    target.mkdir(parents=True)
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "archive", rev], cwd=vault, stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as tar:
            tar.extractall(target, filter="data")
    _git("init", "-q", cwd=target)
    _git("add", "-A", cwd=target)
    _git(
        "-c",
        "user.name=eval",
        "-c",
        "user.email=eval@localhost",
        "commit",
        "-qm",
        "copy",
        cwd=target,
    )


def setup(workdir: Path, questions: Path, vault: Path, rev: str, home: Path) -> int:
    data = load_questions(questions)
    if workdir.exists():
        raise SystemExit(f"{workdir} exists")
    (workdir / "clones").mkdir(parents=True)
    for slug, path in data["repos"].items():
        _git(
            "clone", "-q", str(Path(path).expanduser()), str(workdir / "clones" / slug), cwd=workdir
        )
    host = configmod.load({"AFT_HOME": str(home)}).host
    copy_pi(workdir)
    (workdir / "home-with").mkdir()
    (workdir / "home-with" / "config.toml").write_text(
        _config_text(home / "config.toml", workdir / "vault-with"), encoding="utf-8"
    )
    _copy_vault(vault, rev, workdir / "vault-with")
    registry = Registry.load(workdir / "vault-with" / "registry.yaml")
    for slug in data["repos"]:
        registry.add_path(registry.by_slug(slug), path_key(host, workdir / "clones" / slug))
    registry.save()
    env = dict(os.environ, AFT_HOME=str(workdir / "home-with"))
    subprocess.run(["aft", "reindex"], env=env, check=True, capture_output=True)
    return prove(workdir, data)


def prove(workdir: Path, data: dict) -> int:
    """Check every question's proofs; returns how many failed."""
    failed = 0
    for q in data["questions"]:
        clone = workdir / "clones" / q["project"]
        problems = []
        if "check" in q and subprocess.run(q["check"], shell=True, cwd=clone).returncode:
            problems.append("check failed")
        if "repo_absent" in q:
            hits = _git("grep", "-I", "-l", "-i", "-E", q["repo_absent"], cwd=clone).stdout
            if hits:
                problems.append(f"in the clone: {hits.split()[:3]}")
        if "vault_absent" in q:
            hits = subprocess.run(
                [
                    "grep",
                    "-rIl",
                    "-E",
                    q["vault_absent"],
                    "--exclude-dir=library",
                    "--exclude-dir=.git",
                    str(workdir / "vault-with"),
                ],
                capture_output=True,
                text=True,
            ).stdout
            if hits:
                problems.append(f"in the vault: {hits.split()[:3]}")
        if problems:
            failed += 1
            print(f"{q['id']}: {'; '.join(problems)}")
    print(f"{len(data['questions']) - failed} of {len(data['questions'])} questions proved")
    return failed


def parse_run(lines: Iterable[str]) -> dict:
    """What one run's event stream says: answer, tokens, tools, and why it is void."""
    end = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "agent_end":
            end = event
    result = {
        "answer": "",
        "tokens": 0,
        "uncached": 0,
        "turns": 0,
        "tools": [],
        "memory_calls": 0,
        "block_chars": 0,
        "recall_chars": 0,
        "void": [],
    }
    if end is None:
        result["void"].append("no agent_end")
        return result
    for message in end["messages"]:
        if message.get("role") == "system":
            sections = message.get("sections") or {}
            result["block_chars"] = len(sections.get("afterthought") or "")
            result["recall_chars"] = len(sections.get("afterthought-recall") or "")
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage") or {}
        result["turns"] += 1
        result["tokens"] += sum(usage.get(k, 0) for k in ("input", "cacheRead", "output"))
        result["uncached"] += usage.get("input", 0)
        texts = []
        for part in message.get("content") or []:
            if part.get("type") == "text":
                texts.append(part["text"])
            if part.get("type") == "toolCall":
                name, args = part.get("name", ""), json.dumps(part.get("arguments"))
                result["tools"].append(name)
                if name not in TOOLS:
                    result["void"].append(f"tool {name}")
                if re.search(r"\baft\s+(search|read|trace)\b", args):
                    result["memory_calls"] += 1
        if texts:
            result["answer"] = "\n".join(texts).strip()
    return result


def sandbox(workdir: Path, arm: str) -> list[str]:
    """The ``bwrap`` prefix of a run: the machine read-only, the home directory emptied."""
    home = Path.home()
    agent = home / ".pi" / "agent"
    args = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev"]
    args += ["--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", str(home)]
    for path in HOME_BINDS:
        args += ["--ro-bind", str(home / path), str(home / path)]
    args += ["--bind", str(workdir), str(workdir)]
    args += ["--bind", str(workdir / f"pi-{arm}"), str(agent)]
    args += ["--ro-bind", str(agent / "npm"), str(agent / "npm")]
    return [*args, "--"]


def copy_pi(workdir: Path) -> None:
    """Give each arm pi's settings to write to, and the extension and skill only to ``with``.

    pi locks its settings file, so it needs its own copy. Session history and credentials
    stay behind; the model endpoint takes no key.
    """
    agent = Path.home() / ".pi" / "agent"
    skip = shutil.ignore_patterns(*PI_LEFT_BEHIND)
    for arm in ARMS:
        shutil.copytree(agent, workdir / f"pi-{arm}", ignore=skip)
    shutil.rmtree(workdir / "pi-without" / "extensions" / "afterthought")
    shutil.rmtree(workdir / "pi-without" / "skills" / "aft")


def one_run(workdir: Path, q: dict, arm: str, repeat: int, write: threading.Lock) -> None:
    run_id = f"{q['id']}.{arm}.{repeat}"
    clone = workdir / "clones" / q["project"]
    tree = workdir / "trees" / run_id
    with write:
        _git("worktree", "add", "-q", "--detach", str(tree), cwd=clone)
    log = workdir / "runs" / f"{run_id}.jsonl"
    command = [
        *sandbox(workdir, arm),
        "pi",
        "-p",
        "--no-session",
        "--mode",
        "json",
        "--no-mcp",
        "--tools",
        ",".join(TOOLS),
        ASK.format(q=q["q"]),
    ]
    env = dict(os.environ, AFT_HOME=str(workdir / "home-with"))
    started = time.monotonic()
    timed_out = False
    with log.open("w", encoding="utf-8") as out:
        process = subprocess.Popen(
            command,
            cwd=tree,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            process.wait(RUN_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, 9)
            process.wait()
    seconds = round(time.monotonic() - started, 1)
    record = parse_run(log.read_text(encoding="utf-8").splitlines())
    if timed_out:
        record["void"].append("timed out")
    if _git("status", "--porcelain", "--", ".", f":!{PROBE_DIR}", cwd=tree).stdout.strip():
        record["void"].append("changed the clone")
    record.update(id=q["id"], kind=q["kind"], arm=arm, repeat=repeat, seconds=seconds)
    with write:
        _git("worktree", "remove", "--force", str(tree), cwd=clone)
        with (workdir / "records.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(record) + "\n")
    print(f"{run_id} {seconds}s {record['tokens']} tokens {record['void'] or ''}", flush=True)


def read_records(workdir: Path) -> list[dict]:
    path = workdir / "records.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run(workdir: Path, questions: Path, repeats: int, jobs: int, seed: int) -> int:
    data = load_questions(questions)
    if prove(workdir, data):
        return 1
    (workdir / "runs").mkdir(exist_ok=True)
    (workdir / "trees").mkdir(exist_ok=True)
    done = {(r["id"], r["arm"], r["repeat"]) for r in read_records(workdir)}
    todo = [
        (q, arm, repeat)
        for q in data["questions"]
        for arm in ARMS
        for repeat in range(repeats)
        if (q["id"], arm, repeat) not in done
    ]
    # Shuffled, so neither arm always warms the endpoint's shared cache for the other.
    random.Random(seed).shuffle(todo)
    write = threading.Lock()
    with ThreadPoolExecutor(jobs) as pool:
        for future in [pool.submit(one_run, workdir, *job, write) for job in todo]:
            future.result()
    return 0


def _judge_key(q: dict, answer: str) -> str:
    return hashlib.sha256(f"{q['id']}\0{answer}".encode()).hexdigest()


def judge(workdir: Path, questions: Path, seed: int) -> int:
    data = load_questions(questions)
    by_id = {q["id"]: q for q in data["questions"]}
    path = workdir / "judged.json"
    judged = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = configmod.load({"AFT_HOME": str(workdir / "home-with")})
    records = [r for r in read_records(workdir) if r["answer"]]
    random.Random(seed).shuffle(records)
    for record in records:
        q = by_id[record["id"]]
        key = _judge_key(q, record["answer"])
        if key in judged:
            continue
        user = (
            f"Question: {q['q']}\n\nReference answer: {q['reference']}\n\n"
            f"Answer given: {record['answer']}"
        )
        judged[key] = model.complete_json(cfg, JUDGE_SYSTEM, user, JUDGE_SCHEMA, 300)
        path.write_text(json.dumps(judged, indent=1), encoding="utf-8")
    print(f"{len(judged)} distinct answers judged")
    return 0


def _rate(rows: list[dict]) -> float | None:
    return statistics.mean(r["credit"] for r in rows) if rows else None


def _median(rows: list[dict], field: str) -> float | None:
    return statistics.median(r[field] for r in rows) if rows else None


def score(records: list[dict], verdicts: dict, questions: list[dict]) -> dict:
    """Credit every run, drop void runs and questions over the void share, sum each kind."""
    by_id = {q["id"]: q for q in questions}
    rows = []
    for record in records:
        q = by_id[record["id"]]
        verdict = "no_answer"
        if record["answer"]:
            verdict = verdicts[_judge_key(q, record["answer"])]["verdict"]
        rows.append({**record, "verdict": verdict, "credit": CREDIT[verdict]})
    dropped = []
    for q in questions:
        mine = [r for r in rows if r["id"] == q["id"]]
        if mine and sum(bool(r["void"]) for r in mine) / len(mine) > VOID_SHARE:
            dropped.append(q["id"])
    kept = [r for r in rows if not r["void"] and r["id"] not in dropped]
    kinds: dict = {}
    for kind in KINDS:
        kinds[kind] = {}
        for arm in ARMS:
            mine = [r for r in kept if r["kind"] == kind and r["arm"] == arm]
            kinds[kind][arm] = {
                "runs": len(mine),
                "rate": _rate(mine),
                "tokens": _median(mine, "tokens"),
                "uncached": _median(mine, "uncached"),
                "turns": _median(mine, "turns"),
                "seconds": _median(mine, "seconds"),
            }
    return {"rows": rows, "kept": kept, "dropped": dropped, "kinds": kinds}


def rule(kinds: dict) -> list[tuple[str, bool, str]]:
    """The four parts of the rule in BUILD-PLAN T59, each as (kind, held, how it read)."""

    def gap(kind: str) -> float:
        return (kinds[kind]["with"]["rate"] - kinds[kind]["without"]["rate"]) * 100

    def cost(kind: str) -> float:
        return kinds[kind]["with"]["tokens"] / kinds[kind]["without"]["tokens"]

    return [
        ("memory", gap("memory") >= 50, f"{gap('memory'):+.0f} points, needs +50"),
        (
            "repo",
            gap("repo") >= -5 and cost("repo") < 1,
            f"{gap('repo'):+.0f} points, needs -5 or better; tokens {cost('repo'):.2f}x, "
            "needs under 1",
        ),
        (
            "control",
            gap("control") >= -5 and cost("control") <= 1.25,
            f"{gap('control'):+.0f} points, needs -5 or better; tokens "
            f"{cost('control'):.2f}x, needs 1.25 or less",
        ),
        ("stale", gap("stale") >= -10, f"{gap('stale'):+.0f} points, needs -10 or better"),
    ]


def report(workdir: Path, questions: Path) -> int:
    data = load_questions(questions)
    judged = json.loads((workdir / "judged.json").read_text(encoding="utf-8"))
    result = score(read_records(workdir), judged, data["questions"])
    lines = ["# Outcome eval", ""]
    lines += ["| kind | arm | runs | correct | tokens | uncached | turns | seconds |"]
    lines += ["|---|---|---:|---:|---:|---:|---:|---:|"]
    for kind in KINDS:
        for arm in ARMS:
            k = result["kinds"][kind][arm]
            rate = "-" if k["rate"] is None else f"{k['rate'] * 100:.0f}%"
            lines.append(
                f"| {kind} | {arm} | {k['runs']} | {rate} | {k['tokens']} | {k['uncached']} "
                f"| {k['turns']} | {k['seconds']} |"
            )
    lines += ["", "## Rule", ""]
    for kind, held, how in rule(result["kinds"]):
        lines.append(f"- {kind}: {'held' if held else 'MISSED'} ({how})")
    voids = [r for r in result["rows"] if r["void"]]
    lines += ["", f"## Void: {len(voids)} runs, dropped questions {result['dropped']}", ""]
    lines += [f"- {r['id']} {r['arm']} {r['repeat']}: {r['void'][:2]}" for r in voids]
    lines += ["", "## Questions", ""]
    lines += ["| id | with | without | tokens with | tokens without | recall | aft calls |"]
    lines += ["|---|---:|---:|---:|---:|---:|---:|"]
    for q in data["questions"]:
        mine = [r for r in result["kept"] if r["id"] == q["id"]]
        arm = {a: [r for r in mine if r["arm"] == a] for a in ARMS}
        rates = ["-" if not arm[a] else f"{_rate(arm[a]):.2f}" for a in ARMS]
        recall = _median(arm["with"], "recall_chars")
        calls = sum(r["memory_calls"] for r in arm["with"])
        lines.append(
            f"| {q['id']} | {rates[0]} | {rates[1]} | {_median(arm['with'], 'tokens')} "
            f"| {_median(arm['without'], 'tokens')} | {recall} | {calls} |"
        )
    (workdir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("setup", "run", "judge", "report"))
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--vault", type=Path, default=Path.home() / "afterthought")
    parser.add_argument("--rev", default="HEAD", help="the vault commit the with arm reads")
    parser.add_argument("--home", type=Path, default=Path.home() / ".afterthought")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--seed", type=int, default=59)
    args = parser.parse_args(argv)
    workdir = args.workdir.resolve()
    if shutil.which("pi") is None:
        raise SystemExit("pi is not on PATH")
    if args.command == "setup":
        return 1 if setup(workdir, args.questions, args.vault, args.rev, args.home) else 0
    if args.command == "run":
        return run(workdir, args.questions, args.repeats, args.jobs, args.seed)
    if args.command == "judge":
        return judge(workdir, args.questions, args.seed)
    return report(workdir, args.questions)


if __name__ == "__main__":
    sys.exit(main())

"""The model client: one OpenAI-compatible endpoint, called over HTTP (DESIGN 13).

Every call asks for structured output with ``response_format: json_schema``, so
decoding is constrained by the schema the caller passed. vLLM and hosted endpoints
both take that field as written. The answer is still checked with ``aft.schema``,
because constrained decoding is a strong expectation rather than a guarantee:
output that fails validation, and a 5xx, are each retried once before the call
raises (DESIGN 14).

Each call records the tokens it used in ``usage.jsonl`` in the home directory.
Budgets are measured from that log: the daily cap spans separate processes, so the
count cannot live in memory (DESIGN 13).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aft import schema as schemamod
from aft.config import Config

USAGE_LOG = "usage.jsonl"
# A digest chunk is large and the endpoint is shared, so a call is allowed to take
# a while before it counts as lost.
TIMEOUT_SECONDS = 300.0
ATTEMPTS = 2
# OpenAI requires the name of a json_schema response format to match [a-zA-Z0-9_-]{1,64}.
SCHEMA_NAME = "aft"


class ModelError(Exception):
    pass


class _Retryable(Exception):
    """The endpoint answered badly in a way one more call can plausibly fix."""


def complete_json(
    config: Config,
    system: str,
    user: str,
    schema: dict,
    max_tokens: int,
) -> dict:
    """Ask the model for one JSON object that satisfies ``schema``.

    Raises ``ModelError`` when the endpoint cannot be reached, answers with a
    non-2xx or an unusable body, or returns output that fails ``schema`` twice.
    """
    if not config.settings.model.name:
        raise ModelError("model.name is empty; set it in config.toml")

    body = {
        "model": config.settings.model.name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": schema},
        },
    }

    problems: list[str] = []
    for _ in range(ATTEMPTS):
        try:
            content = _chat(config, body)
        except _Retryable as exc:
            problems.append(str(exc))
            continue
        try:
            answer = _checked(content, schema)
        except schemamod.SchemaError as exc:
            problems.append(str(exc))
            continue
        return answer

    raise ModelError("; ".join(dict.fromkeys(problems)) if problems else "the model made no call")


def tokens_used(config: Config, since: datetime | None = None) -> int:
    """Tokens charged to this home since ``since``, or ever when it is None."""
    path = usage_path(config)
    if not path.exists():
        return 0
    total = 0
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # A torn write must not spoil the ledger for good, so an unreadable line
            # is left out of the total.
            continue
        if not isinstance(record, dict):
            continue
        if since is not None:
            at = _time(record.get("at"))
            if at is None or at < since:
                continue
        total += _tokens(record, "prompt_tokens") + _tokens(record, "completion_tokens")
    return total


def tokens_used_today(config: Config, now: datetime | None = None) -> int:
    """Tokens used since midnight UTC, the day the daily cap is counted over."""
    moment = now or datetime.now(UTC)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return tokens_used(config, since=midnight)


def usage_path(config: Config) -> Path:
    return config.home / USAGE_LOG


def _chat(config: Config, body: dict) -> str:
    """One HTTP call. Returns the assistant message content."""
    url = config.settings.model.endpoint.rstrip("/") + "/chat/completions"
    started = datetime.now(UTC)
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        message = f"{url} returned {exc.code}: {detail}"
        if exc.code >= 500:
            raise _Retryable(message) from exc
        raise ModelError(message) from exc
    except OSError as exc:
        # Covers a refused connection, a DNS failure and a timeout alike: the
        # endpoint is treated as down and the work stays queued (DESIGN 14).
        raise ModelError(f"cannot reach {url}: {exc}") from exc

    try:
        reply = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelError(f"{url} did not answer with JSON: {raw[:200]}") from exc
    if not isinstance(reply, dict):
        raise ModelError(f"{url} answered with JSON that is not an object: {raw[:200]}")

    _record_usage(config, started, reply.get("usage"))
    choices = reply.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelError(f"{url} returned no choices: {raw[:200]}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ModelError(f"{url} returned no message content: {raw[:200]}")
    return content


def _checked(content: str, schema: dict) -> dict:
    """Parse and validate the model's own answer, the output that can be retried."""
    try:
        answer = json.loads(content)
    except json.JSONDecodeError as exc:
        raise schemamod.SchemaError(f"the model did not answer with JSON: {exc}") from exc
    if not isinstance(answer, dict):
        raise schemamod.SchemaError("the model answered with a value that is not a JSON object")
    schemamod.validate(answer, schema)
    return answer


def _record_usage(config: Config, started: datetime, usage: Any) -> None:
    prompt = completion = 0
    if isinstance(usage, dict):
        prompt = _tokens(usage, "prompt_tokens")
        completion = _tokens(usage, "completion_tokens")
    record = {
        "at": started.isoformat(),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
    }
    path = usage_path(config)
    try:
        config.home.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError as exc:
        # An answer that cannot be billed is not returned: budgets are read from this
        # log, so the caller leaves the work queued and reports the failure (DESIGN 14).
        raise ModelError(f"cannot record token usage in {path}: {exc}") from exc


def _tokens(record: dict, key: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

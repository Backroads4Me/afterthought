"""The model client: one OpenAI-compatible endpoint, called over HTTP (DESIGN 13).

Every call asks for structured output with ``response_format: json_schema``, so
decoding is constrained by the schema the caller passed. vLLM and hosted endpoints
both take that field as written. The answer is still checked with ``aft.schema``,
because constrained decoding is a strong expectation rather than a guarantee:
output that fails validation, and a 5xx, are each retried once before the call
raises (DESIGN 14).

An endpoint that needs a key gets it from the environment variable named by
``model.api_key_env``, sent as a bearer token and kept out of every report (DESIGN 13).

Each call records the tokens it used in ``usage.jsonl`` in the home directory; a
response that does not report a usable token count raises rather than being written
as free. Budgets are measured from that log: the daily cap spans separate processes,
so the count cannot live in memory (DESIGN 13).
"""

from __future__ import annotations

import http.client
import json
import os
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
# What a report carries of the endpoint's own text.
REPORT_CHARS = 500
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
    non-2xx, an unusable body or no usable token count, or returns output that fails
    ``schema`` twice.
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
            "json_schema": {"name": SCHEMA_NAME, "schema": schema},
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

    raise ModelError(_reported(config, "; ".join(problems)))


def tokens_used(config: Config, since: datetime | None = None) -> int:
    """Tokens charged to this home since ``since``, or ever when it is None.

    A stamp in the ledger or an argument with no offset is read as UTC, so every
    comparison is between like instants.
    """
    path = usage_path(config)
    if not path.exists():
        return 0
    start = _utc(since) if since is not None else None
    total = 0
    # A torn write must not spoil the ledger for good, so bytes that do not decode
    # are read as replacement characters and any line that is not a billable record
    # of this home's own shape is left out of the total.
    for line in path.read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if start is not None:
            at = _time(record.get("at"))
            if at is None or at < start:
                continue
        prompt = _token_count(record, "prompt_tokens")
        completion = _token_count(record, "completion_tokens")
        if prompt is None or completion is None:
            continue
        total += prompt + completion
    return total


def tokens_used_today(config: Config, now: datetime | None = None) -> int:
    """Tokens used since midnight UTC, the day the daily cap is counted over."""
    moment = _utc(now) if now is not None else datetime.now(UTC)
    midnight = moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return tokens_used(config, since=midnight)


def usage_path(config: Config) -> Path:
    return config.home / USAGE_LOG


def _chat(config: Config, body: dict) -> str:
    """One HTTP call. Returns the assistant message content."""
    url = config.settings.model.endpoint.rstrip("/") + "/chat/completions"
    headers = _headers(config)
    started = datetime.now(UTC)
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        # A report carries REPORT_CHARS characters of the endpoint's own text at most, and
        # reading that body is a request in itself: it can stop short or never arrive.
        try:
            detail = exc.read(REPORT_CHARS)
        except (OSError, http.client.HTTPException, ValueError):
            detail = b""
        excerpt = detail.decode(errors="replace")[:REPORT_CHARS]
        message = _reported(config, f"{url} returned {exc.code}: {excerpt}")
        if exc.code >= 500:
            raise _Retryable(message) from exc
        raise ModelError(message) from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # Covers a refused connection, a DNS failure, a timeout, a reply that is not
        # HTTP, an answer cut off mid-body and a URL urllib cannot use alike: the
        # endpoint is treated as down and the work stays queued (DESIGN 14).
        raise ModelError(_reported(config, f"cannot reach {url}: {exc}")) from exc

    try:
        reply = json.loads(raw)
    except json.JSONDecodeError as exc:
        problem = f"{url} did not answer with JSON: {raw[:200]}"
        raise ModelError(_reported(config, problem)) from exc
    if not isinstance(reply, dict):
        problem = f"{url} answered with JSON that is not an object: {raw[:200]}"
        raise ModelError(_reported(config, problem))

    _record_usage(config, started, reply.get("usage"))
    choices = reply.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelError(_reported(config, f"{url} returned no choices: {raw[:200]}"))
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        problem = f"{url} returned no message content: {raw[:200]}"
        raise ModelError(_reported(config, problem))
    return content


def _headers(config: Config) -> dict[str, str]:
    """The headers of one call.

    An endpoint that needs a key takes it from the environment variable
    ``model.api_key_env`` names, so config.toml holds a name and never the key (DESIGN
    13). A name with nothing behind it is the setup fault it sounds like: the call fails
    there, naming the variable and never a value.
    """
    name = config.settings.model.api_key_env
    if not name:
        return {"Content-Type": "application/json"}
    key = os.environ.get(name, "")
    if not key:
        raise ModelError(f"{name} is unset or empty; model.api_key_env names it as the key")
    return {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}


def _reported(config: Config, problem: str) -> str:
    """The failure text aft may put in a report.

    DESIGN 13 keeps the API key out of error messages, and an endpoint or a proxy in
    front of one can echo the request back in its own error body, so the key this call
    sent is taken out of the text however the rest of the message reads.
    """
    name = config.settings.model.api_key_env
    key = os.environ.get(name, "") if name else ""
    return problem.replace(key, "[redacted]") if key else problem


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
    prompt = _token_count(usage, "prompt_tokens")
    completion = _token_count(usage, "completion_tokens")
    if prompt is None or completion is None or (prompt == 0 and completion == 0):
        # A call whose cost is unknown, or reported as nothing at all, is not returned
        # either: an aft call always has a prompt to charge, so a server billing zero
        # both ways has usage accounting off and a cap over that ledger never trips.
        problem = f"the endpoint reported no usable token usage: {usage!r}"
        raise ModelError(_reported(config, problem))
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
        problem = f"cannot record token usage in {path}: {exc}"
        raise ModelError(_reported(config, problem)) from exc


def _token_count(source: Any, key: str) -> int | None:
    """The token count under ``key``, or None when it is not a whole number of tokens."""
    value = source.get(key) if isinstance(source, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _utc(moment: datetime) -> datetime:
    """A stamp with no offset is read as UTC, so every comparison is aware."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)

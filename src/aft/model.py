"""The model client: one OpenAI-compatible endpoint, called over HTTP (DESIGN 13).

Every call asks for structured output with ``response_format: json_schema``, so
decoding is constrained by the schema the caller passed. vLLM and hosted endpoints
both take that field as written. The answer is still checked with ``aft.schema``,
because constrained decoding is a strong expectation rather than a guarantee:
output that fails validation, and a 5xx, are each retried once before the call
raises (DESIGN 14).

Each call records the tokens it used in ``usage.jsonl`` in the home directory; a
response that does not report a usable token count raises rather than being written
as free. Budgets are measured from that log: the daily cap spans separate processes,
so the count cannot live in memory (DESIGN 13).
"""

from __future__ import annotations

import http.client
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

    raise ModelError("; ".join(problems))


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
    origin = _address(url)
    started = datetime.now(UTC)
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        # An error body is itself a response that can stop short or never arrive, and it
        # may be enormous: aft reads only as much of it as a report can carry.
        try:
            detail = exc.read(500)
        except (OSError, http.client.HTTPException, ValueError):
            detail = b""
        excerpt = detail.decode(errors="replace")[:500]
        message = _reported(config, f"{origin} returned {exc.code}: {excerpt}")
        if exc.code >= 500:
            raise _Retryable(message) from exc
        raise ModelError(message) from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # Covers a refused connection, a DNS failure, a timeout, a reply that is not
        # HTTP, an answer cut off mid-body and a URL urllib cannot use alike: the
        # endpoint is treated as down and the work stays queued (DESIGN 14).
        raise ModelError(_reported(config, f"cannot reach {origin}: {exc}")) from exc

    try:
        reply = json.loads(raw)
    except json.JSONDecodeError as exc:
        problem = f"{origin} did not answer with JSON: {raw[:200]}"
        raise ModelError(_reported(config, problem)) from exc
    if not isinstance(reply, dict):
        problem = f"{origin} answered with JSON that is not an object: {raw[:200]}"
        raise ModelError(_reported(config, problem))

    _record_usage(config, started, reply.get("usage"))
    choices = reply.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelError(_reported(config, f"{origin} returned no choices: {raw[:200]}"))
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        problem = f"{origin} returned no message content: {raw[:200]}"
        raise ModelError(_reported(config, problem))
    return content


def _address(url: str) -> str:
    """The endpoint as aft names it in a failure: scheme, host and path.

    The userinfo and parameters a URL may carry are dropped as noise; ``_reported`` is
    what keeps a credential out of the text, however it reached the message.
    """
    address = url.partition("?")[0].partition("#")[0]
    scheme, sep, rest = address.partition("://")
    if not sep:
        return address
    authority, slash, path = rest.partition("/")
    return f"{scheme}://{authority.rpartition('@')[2]}{slash}{path}"


def _reported(config: Config, problem: str) -> str:
    """The failure text aft reports, with any credential the endpoint setting carries
    taken out. DESIGN 14 writes this text into a report, and the setting is the only
    place a token can sit today, so nothing it holds may be printed: urllib echoes the
    whole configured URL in its own wording, and a proxy can echo it in an error body.
    """
    for secret in _secrets(config.settings.model.endpoint):
        problem = problem.replace(secret, "[redacted]")
    return problem


def _secrets(endpoint: str) -> list[str]:
    """The parts of the endpoint setting that could be a credential: its userinfo and
    each parameter written after a '?' or a '#'. Longest first, so a whole parameter is
    taken out before a part of one.
    """
    head, _, parameters = endpoint.replace("#", "?").partition("?")
    authority = head.split("://")[-1].split("/")[0]
    pieces = {authority.rpartition("@")[0], parameters, *parameters.split("&")}
    return sorted((piece for piece in pieces if piece), key=len, reverse=True)


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
        raise ModelError(f"the endpoint reported no usable token usage: {usage!r}")
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

"""The model client: one OpenAI-compatible endpoint, called over HTTP (DESIGN 13).

Every call asks for structured output with ``response_format: json_schema``, so
decoding is constrained by the schema the caller passed. vLLM and hosted endpoints
both take that field as written. The answer is still checked with ``aft.schema``,
because constrained decoding is a strong expectation rather than a guarantee:
output that fails validation, and a 5xx, are each retried once before the call
raises (DESIGN 14). A retry is not the same ask again: a schema failure sends the
model its own invalid answer beside the validator's reason as a repair turn, and
each attempt after the first is asked at a temperature raised off the first ask's,
so a model that repeats itself at temperature 0 is given room to answer differently.

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
from datetime import UTC, datetime, timedelta
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
# A real API key runs 20 to 50 characters, so at this length a cut cannot mangle prose.
MIN_KEY_CHARS = 16
# What an endpoint's own words become when a key too short to cut cleanly is among them.
WITHHELD = "[details withheld]"
# OpenAI requires the name of a json_schema response format to match [a-zA-Z0-9_-]{1,64}.
SCHEMA_NAME = "aft"

# A retry asks at a temperature raised by this step per attempt, capped here, because a
# model asked twice for the same thing at temperature 0 tends to answer twice the same
# way. A base above the cap is a retry's floor as well: a retry never asks colder than
# the caller chose.
RETRY_TEMPERATURE_STEP = 0.2
RETRY_TEMPERATURE_CAP = 0.7

# The repair turn a schema failure appends to the conversation: the model sees its own
# invalid answer and the reason it failed before it is asked once more.
REPAIR_TURN = (
    "Your previous response failed JSON schema validation: {error}. "
    "Please output valid JSON strictly conforming to the required schema."
)

# An endpoint that answered with a server error, a rate limit, or nothing at all is left
# to cool down in this file: no call is made until the deadline passes, so a down
# endpoint costs one line per run instead of the same error job after job (F66).
COOLDOWN_FILE = "model-cooldown.json"
COOLDOWN_SECONDS = 900.0


class ModelError(Exception):
    pass


class _Retryable(Exception):
    """The endpoint answered badly in a way one more call can plausibly fix."""


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx back as an HTTPError rather than following it.

    aft posts to the endpoint the operator configured and nowhere else, so a bearer key
    never travels to a host nobody set up.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """None is urllib's signal to leave the redirect response to the caller."""
        return None


def complete_json(
    config: Config,
    system: str,
    user: str,
    schema: dict,
    max_tokens: int,
) -> dict:
    """Ask the model for one JSON object that satisfies ``schema``.

    Raises ``ModelError`` when ``model.name`` is empty or ``schema`` misuses a keyword,
    when the endpoint is cooling down from an earlier failure, when the endpoint cannot
    be reached, answers with a non-2xx, an unusable body or no usable token count, or
    returns output that fails ``schema`` twice.

    A retry is not the same ask again: a schema failure sends the model its own invalid
    answer beside the validator's reason as a repair turn, and every attempt after the
    first is asked at ``retry_temperature``.
    """

    # A cooldown an earlier failure left is answered before anything is sent: the point
    # of the record is that a down endpoint costs no calls at all while it lasts (F66).
    cooling, reason = is_in_cooldown(config)
    if cooling:
        record = get_cooldown(config) or {}
        raise ModelError(f"Model endpoint is in cooldown until {record.get('until')}: {reason}")
    if not config.settings.model.name:
        raise ModelError("model.name is empty; set it in config.toml")
    try:
        schemamod.check(schema)
    except schemamod.SchemaError as exc:
        raise ModelError(f"the schema aft was given is faulty: {exc}") from exc

    body = {
        **config.settings.model.extra_body,
        "model": config.settings.model.name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": config.settings.model.temperature,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": SCHEMA_NAME, "schema": schema},
        },
    }

    problems: list[str] = []
    for attempt in range(ATTEMPTS):
        # A retry is a different ask: the temperature rises off the first attempt's, so
        # a model that answered twice alike at one temperature is given room not to.
        body["temperature"] = retry_temperature(config.settings.model.temperature, attempt)
        try:
            content = _chat(config, body)
        except _Retryable as exc:
            problems.append(str(exc))
            continue
        try:
            answer = _checked(content, schema)
        except schemamod.SchemaError as exc:
            error = _quoted(config, str(exc))
            problems.append(f"the answer failed its schema: {error}")
            # The retry carries the failed answer and the reason it failed, so the model
            # is told what was wrong rather than asked the same question again cold.
            body["messages"] = [
                *body["messages"],
                {"role": "assistant", "content": content},
                {"role": "user", "content": REPAIR_TURN.format(error=error)},
            ]
            continue
        return answer

    raise ModelError("; ".join(problems))


def retry_temperature(base: float, attempt: int) -> float:
    """The temperature of the call numbered ``attempt``, where 0 is the first ask.

    Each attempt after the first rises by RETRY_TEMPERATURE_STEP and stops at
    RETRY_TEMPERATURE_CAP. A base above the cap never falls: a retry asks at least as
    hot as the caller chose.
    """
    return min(base + RETRY_TEMPERATURE_STEP * attempt, max(base, RETRY_TEMPERATURE_CAP))


def cooldown_path(config: Config) -> Path:
    """Where the endpoint's cooldown state lives."""
    return config.home / COOLDOWN_FILE


def get_cooldown(config: Config) -> dict[str, Any] | None:
    """The endpoint's cooldown record, or None when it is not cooling down.

    A record left with ``active`` false, or one that cannot be read, is as good as
    absent.
    """
    try:
        record = json.loads(cooldown_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not record.get("active"):
        return None
    return record


def is_in_cooldown(config: Config) -> tuple[bool, str]:
    """Whether the endpoint is cooling down, and the reason when it is.

    A record whose deadline has passed cools down no longer, whether or not a later
    call got far enough to clear it.
    """
    record = get_cooldown(config)
    if record is None:
        return False, ""
    until = _time(record.get("until"))
    if until is None or until <= datetime.now(UTC):
        return False, ""
    return True, str(record.get("reason") or "")


def cooldown_until(config: Config) -> datetime | None:
    """When the endpoint's cooldown ends, or None while it is not cooling down."""
    cooling, _ = is_in_cooldown(config)
    if not cooling:
        return None
    record = get_cooldown(config) or {}
    return _time(record.get("until"))


def set_cooldown(config: Config, reason: str, duration_seconds: float = COOLDOWN_SECONDS) -> None:
    """Cool the endpoint down for ``duration_seconds`` from now (15 minutes by default).

    ``consecutive_errors`` counts the errors in a row across calls, so a report can
    tell one bad moment from an endpoint that keeps failing. Writing the record is
    best-effort: the caller already holds the error worth reporting, and a home that
    cannot be written only means the next call meets the endpoint again.
    """
    previous = get_cooldown(config) or {}
    record = {
        "active": True,
        "reason": reason,
        "until": (datetime.now(UTC) + timedelta(seconds=duration_seconds)).isoformat(),
        "consecutive_errors": int(previous.get("consecutive_errors") or 0) + 1,
    }
    try:
        config.home.mkdir(parents=True, exist_ok=True)
        cooldown_path(config).write_text(json.dumps(record) + "\n", encoding="utf-8")
    except OSError:
        pass


def clear_cooldown(config: Config) -> bool:
    """End any cooldown, True when there was a record to end.

    A file that cannot be removed is marked inactive instead, so a home that cannot
    let go of it still stops cooling down.
    """
    path = cooldown_path(config)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        record = get_cooldown(config)
        if record is None:
            return False
        record["active"] = False
        try:
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        except OSError:
            return False
    return True


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
    """Tokens counted against today's cap: used since midnight UTC, or since the user last
    reset the budget when that was later (BUILD-PLAN F40)."""
    return tokens_used(config, since=budget_day_start(config, now))


def budget_day_start(config: Config, now: datetime | None = None) -> datetime:
    """Where today's count starts: midnight UTC, or a reset made since then."""
    moment = _utc(now) if now is not None else datetime.now(UTC)
    midnight = moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    reset = budget_reset_at(config)
    return reset if reset is not None and midnight < reset <= moment else midnight


def budget_reset_at(config: Config) -> datetime | None:
    """When the user last reset the budget, or None. The ledger itself is never changed
    by a reset: it is the record of what was spent."""
    path = config.home / BUDGET_RESET
    try:
        return _time(path.read_text(encoding="utf-8").strip())
    except OSError:
        return None


def reset_budget(config: Config, now: datetime | None = None) -> datetime:
    """Start today's count again from now (BUILD-PLAN F40). Midnight UTC still starts the
    next day as usual."""
    moment = _utc(now) if now is not None else datetime.now(UTC)
    config.home.mkdir(parents=True, exist_ok=True)
    (config.home / BUDGET_RESET).write_text(moment.isoformat() + "\n", encoding="utf-8")
    return moment


# The marker a budget reset leaves: the moment today's count starts again from.
BUDGET_RESET = "budget-reset"


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
        # Built here rather than at import, so the proxy environment is read at call time.
        opener = urllib.request.build_opener(_RefuseRedirect())
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read().decode(errors="replace")
        # The endpoint answered, so it is up: no cooldown stands any longer, whatever
        # it went on to say.
        clear_cooldown(config)
    except urllib.error.HTTPError as exc:
        target = exc.headers.get("location")
        if 300 <= exc.code < 400 and target:
            named = _quoted(config, target)
            shown = named[:REPORT_CHARS]
            if len(named) > REPORT_CHARS:
                shown += " [truncated]"
            raise ModelError(f"{url} redirected to {shown}") from exc
        # A report carries REPORT_CHARS characters of the endpoint's own text at most, and
        # reading that body is a request in itself: it can stop short or never arrive. The
        # read overshoots the bound by the configured key, so a key straddling the cut
        # arrives whole and comes out of the report instead of leaving its head behind.
        try:
            detail = exc.read(REPORT_CHARS + len(_key(config).encode()))
        except (OSError, http.client.HTTPException, ValueError):
            detail = b""
        excerpt = _quoted(config, detail.decode(errors="replace"))[:REPORT_CHARS]
        message = f"{url} returned {exc.code}: {excerpt}"
        # A server error or a rate limit is the endpoint's own state, not a session's:
        # it goes down as a cooldown, so the calls after this one fail fast (F66).
        if exc.code >= 500 or exc.code == 429:
            set_cooldown(config, message)
        if exc.code >= 500:
            raise _Retryable(message) from exc
        raise ModelError(message) from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # Covers a refused connection, a DNS failure, a timeout, a reply that is not
        # HTTP, an answer cut off mid-body and a URL urllib cannot use alike: the
        # endpoint is treated as down and the work stays queued (DESIGN 14). It also
        # goes down as a cooldown, so the next call reads the record instead of meeting
        # the same refusal itself (F66).
        message = f"cannot reach {url}: {_quoted(config, str(exc))}"
        set_cooldown(config, message)
        raise ModelError(message) from exc

    try:
        reply = json.loads(raw)
    except json.JSONDecodeError as exc:
        echo = _quoted(config, raw)[:200]
        raise ModelError(f"{url} did not answer with JSON: {echo}") from exc
    if not isinstance(reply, dict):
        echo = _quoted(config, raw)[:200]
        raise ModelError(f"{url} answered with JSON that is not an object: {echo}")

    _record_usage(config, started, reply.get("usage"))
    choices = reply.get("choices")
    if not isinstance(choices, list) or not choices:
        echo = _quoted(config, raw)[:200]
        raise ModelError(f"{url} returned no choices: {echo}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        finish = choices[0].get("finish_reason") if isinstance(choices[0], dict) else None
        if finish == "length":
            # A reasoning model spends its tokens thinking before it answers, and one that
            # runs out has nothing left to answer with. The cause is the endpoint's setup,
            # so the error names the setting that fixes it rather than the reply alone.
            raise ModelError(
                f"{url} used all {body.get('max_tokens')} tokens without answering"
                " (finish_reason length); a reasoning model can be told not to think with"
                " model.extra_body in config.toml"
            )
        echo = _quoted(config, raw)[:200]
        raise ModelError(f"{url} returned no message content: {echo}")
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


def _key(config: Config) -> str:
    """The value of the environment variable ``model.api_key_env`` names, empty if unset."""
    name = config.settings.model.api_key_env
    return os.environ.get(name, "") if name else ""


def _quoted(config: Config, said: str) -> str:
    """Text the endpoint wrote, made safe to put in a report.

    Only the endpoint's own words can echo a bearer key back (DESIGN 13), so only they
    pass through here: a key long enough to cut out precisely comes out in place, and
    one too short to tell from the prose takes that text out whole. What aft wrote
    itself - the endpoint, the status, the kind of failure - never passes through, so a
    report keeps its diagnosis either way.
    """
    key = _key(config)
    if not key or key not in said:
        return said
    return said.replace(key, "[redacted]") if len(key) >= MIN_KEY_CHARS else WITHHELD


def _checked(content: str, schema: dict) -> dict:
    """Parse and validate the model's own answer, the output that can be retried."""
    try:
        # strict=False lets a string hold a raw newline or tab. A model under a JSON schema
        # writes those inside a long quote often enough, the value is unambiguous, and every
        # quote is checked against its source afterwards with whitespace folded.
        answer = json.loads(content, strict=False)
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
        reported = _quoted(config, repr(usage))
        raise ModelError(f"the endpoint reported no usable token usage: {reported}")
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

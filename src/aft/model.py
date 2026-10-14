"""The model client: one OpenAI-compatible endpoint, called over HTTP (DESIGN 13).

Every call asks for structured output with ``response_format: json_schema``, so
decoding is constrained by the schema the caller passed. vLLM and hosted endpoints
output that fails validation, and a 5xx, are each retried once before the call
raises (DESIGN 14); an answer that failed because it ran out of output tokens is
not, since a repair turn would be cut again, and the caller asks on a shorter
input or refuses instead (BUILD-PLAN T54). A retry is not the same ask again: a
schema failure sends the
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
import random
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
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
COOLDOWN_BASE_SECONDS = 30.0


class ModelError(Exception):
    pass


class _Retryable(Exception):
    """The endpoint answered badly in a way one more call can plausibly fix."""


class OutputCut(ModelError):
    """The endpoint's answer failed the schema because it ran out of output tokens.

    A repair turn asks the same question again and would be cut again, so the ordinary
    retry is skipped and the caller decides: a digest call asks again on half its input
    (BUILD-PLAN T54), and any other caller refuses the ask.
    """


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
    endpoint: str | None = None,
    timeout: float | None = None,
    record_cooldown: bool = True,
    repair: bool = True,
    purpose: str = "work",
) -> dict:
    """Ask the model for one JSON object that satisfies ``schema``.

    Raises ``ModelError`` when ``model.name`` is empty or ``schema`` misuses a keyword,
    when the endpoint is cooling down from an earlier failure, when the endpoint cannot
    be reached, answers with a non-2xx, an unusable body or no usable token count, or
    returns output that fails ``schema`` twice. An answer that failed ``schema`` while
    the endpoint reported ``finish_reason: length`` raises ``OutputCut`` at once, with
    no repair turn: the model had no room left to write the answer in (BUILD-PLAN T54).

    A retry is not the same ask again: a schema failure sends the model its own invalid
    answer beside the validator's reason as a repair turn, and every attempt after the
    first is asked at ``retry_temperature``. With ``repair=False`` there is one attempt
    and no repair turn, for a caller that falls back on its own when the model fails.
    ``timeout=None`` keeps TIMEOUT_SECONDS. With ``record_cooldown=False`` the call
    leaves the endpoint's cooldown alone on both sides: it sets none when the endpoint
    fails and clears none when it answers, so one failed call never pauses the others.
    ``purpose`` is written into the usage ledger line: ``work`` for every call but the
    search's rerank, which passes ``search``, so each budget counts only the calls
    named for it (BUILD-PLAN F86).
    """

    # A cooldown an earlier failure left is answered before anything is sent: the point
    # of the record is that a down endpoint costs no calls at all while it lasts (F66).
    target_endpoint = endpoint or config.settings.model.endpoint
    cooling, reason = is_in_cooldown(config, endpoint=target_endpoint)
    if cooling:
        record = get_cooldown(config, endpoint=target_endpoint) or {}
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
    attempts = ATTEMPTS if repair else 1
    for attempt in range(attempts):
        # A retry is a different ask: the temperature rises off the first attempt's, so
        # a model that answered twice alike at one temperature is given room not to.
        body["temperature"] = retry_temperature(config.settings.model.temperature, attempt)
        try:
            content, finish_reason = _chat(
                config,
                body,
                endpoint=target_endpoint,
                timeout=timeout,
                record_cooldown=record_cooldown,
                purpose=purpose,
            )
        except _Retryable as exc:
            problems.append(str(exc))
            continue
        try:
            answer = _checked(content, schema)
        except schemamod.SchemaError as exc:
            if finish_reason == "length":
                # The answer failed because it ran out of output tokens, not because the
                # model misread the ask: a repair turn would be cut again, so the ordinary
                # retry is skipped and the caller decides - a digest call asks again on
                # half its input (BUILD-PLAN T54), any other caller refuses the ask.
                raise OutputCut(
                    f"the answer used all {body.get('max_tokens')} output tokens"
                    " (finish_reason length)"
                ) from exc
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


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header value into seconds from now.

    Accepts integer/float delta-seconds or an HTTP-date string. Returns None if unparseable.
    """
    if not value or not isinstance(value, str):
        return None
    val = value.strip()
    try:
        return max(0.0, float(val))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        current = now if now is not None else datetime.now(UTC)
        diff = (dt - current).total_seconds()
        return max(0.0, diff)
    except Exception:
        return None


def calculate_backoff(
    consecutive_errors: int,
    base: float = COOLDOWN_BASE_SECONDS,
    cap: float = COOLDOWN_SECONDS,
    jitter: float | None = None,
) -> float:
    """Capped exponential backoff with jitter based on consecutive errors."""
    if consecutive_errors < 1:
        consecutive_errors = 1
    exp = min(consecutive_errors - 1, 10)
    if jitter is None:
        jitter = random.uniform(0.0, 1.0)
    return min(base * (2**exp) + jitter, cap)


def _is_timeout(exc: BaseException) -> bool:
    """True when exc is a socket or read timeout rather than a connection failure."""
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, TimeoutError):
        return True
    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        if "refused" not in text and "nodename nor servname provided" not in text:
            return True
    return False


def cooldown_path(config: Config) -> Path:
    """Where the endpoint's cooldown state lives."""
    return config.home / COOLDOWN_FILE


def _read_all_cooldowns(config: Config) -> dict[str, dict[str, Any]]:
    path = cooldown_path(config)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "until" in data and ("active" in data or "consecutive_errors" in data):
        endpoint = config.settings.model.endpoint
        return {endpoint: data}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _write_all_cooldowns(config: Config, data: dict[str, dict[str, Any]]) -> None:
    path = cooldown_path(config)
    try:
        config.home.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    except OSError:
        pass


def get_cooldown(config: Config, endpoint: str | None = None) -> dict[str, Any] | None:
    """The endpoint's cooldown record, or None when it is not cooling down.

    A record left with ``active`` false, or one that cannot be read, is as good as
    absent.
    """
    target = endpoint or config.settings.model.endpoint
    all_records = _read_all_cooldowns(config)
    record = all_records.get(target)
    if not isinstance(record, dict) or not record.get("active"):
        return None
    return record


def is_in_cooldown(config: Config, endpoint: str | None = None) -> tuple[bool, str]:
    """Whether the endpoint is cooling down, and the reason when it is.

    A record whose deadline has passed cools down no longer, whether or not a later
    call got far enough to clear it.
    """
    record = get_cooldown(config, endpoint=endpoint)
    if record is None:
        return False, ""
    until = _time(record.get("until"))
    if until is None or until <= datetime.now(UTC):
        return False, ""
    return True, str(record.get("reason") or "")


def cooldown_until(config: Config, endpoint: str | None = None) -> datetime | None:
    """When the endpoint's cooldown ends, or None while it is not cooling down."""
    cooling, _ = is_in_cooldown(config, endpoint=endpoint)
    if not cooling:
        return None
    record = get_cooldown(config, endpoint=endpoint) or {}
    return _time(record.get("until"))


def set_cooldown(
    config: Config,
    reason: str,
    duration_seconds: float | str | None = None,
    endpoint: str | None = None,
    *,
    retry_after: float | int | str | None = None,
    jitter: float | None = None,
) -> None:
    """Cool the endpoint down.

    If Retry-After / retry_after was provided (integer seconds or HTTP date string),
    that duration is honored. Otherwise, exponential backoff with jitter is applied
    based on consecutive errors.
    """
    target = endpoint or config.settings.model.endpoint
    all_records = _read_all_cooldowns(config)
    previous = all_records.get(target, {})
    consecutive_errors = int(previous.get("consecutive_errors") or 0) + 1

    if retry_after is not None:
        if isinstance(retry_after, (int, float)):
            wait = float(retry_after)
        else:
            parsed = parse_retry_after(str(retry_after))
            wait = (
                parsed
                if parsed is not None
                else calculate_backoff(consecutive_errors, jitter=jitter)
            )
    elif duration_seconds is not None:
        if isinstance(duration_seconds, (int, float)):
            wait = float(duration_seconds)
        else:
            parsed = parse_retry_after(str(duration_seconds))
            wait = (
                parsed
                if parsed is not None
                else calculate_backoff(consecutive_errors, jitter=jitter)
            )
    else:
        wait = calculate_backoff(consecutive_errors, jitter=jitter)

    record = {
        "active": True,
        "reason": reason,
        "until": (datetime.now(UTC) + timedelta(seconds=wait)).isoformat(),
        "consecutive_errors": consecutive_errors,
        "duration_seconds": wait,
    }
    all_records[target] = record
    _write_all_cooldowns(config, all_records)


def clear_cooldown(config: Config, endpoint: str | None = None) -> bool:
    """End any cooldown for the endpoint, True when there was an active record to end."""
    target = endpoint or config.settings.model.endpoint
    all_records = _read_all_cooldowns(config)
    if target not in all_records:
        return False
    record = all_records[target]
    if not isinstance(record, dict) or not record.get("active"):
        return False

    del all_records[target]
    path = cooldown_path(config)

    active_remaining = [
        k for k, v in all_records.items() if isinstance(v, dict) and v.get("active")
    ]
    if not active_remaining:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            all_records[target] = {**record, "active": False}
            _write_all_cooldowns(config, all_records)
            return True
    else:
        _write_all_cooldowns(config, all_records)
        return True


def tokens_used(config: Config, since: datetime | None = None, purpose: str | None = None) -> int:
    """Tokens charged to this home since ``since``, or ever when it is None.

    A stamp in the ledger or an argument with no offset is read as UTC, so every
    comparison is between like instants.
    With a ``purpose``, only ledger lines of that purpose count; a line written without
    one counts as ``work``, the purpose every call had before the ledger named one
    (BUILD-PLAN F86).
    """
    path = usage_path(config)
    if not path.exists():
        return 0
    start = _utc(since) if since is not None else None
    total = 0
    # A torn write must not spoil the ledger for good, so bytes that do not decode
    # are read as replacement characters and any line that is not a billable record
    # of this home's own shape is left out of the total.
    for line in _ledger_lines(path, start):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if purpose is not None and record.get("purpose", "work") != purpose:
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


#: The ledger is appended in time order, give or take writers racing each other, so a
#: read from the end stops once it passes a line this much older than the start it
#: counts from (BUILD-PLAN T51).
LEDGER_SLACK = timedelta(hours=1)
#: How much of the ledger a read from the end takes at a time.
LEDGER_BLOCK = 64 * 1024


def _ledger_lines(path: Path, start: datetime | None) -> list[str]:
    """The ledger's lines a check since ``start`` needs: all of them when ``start`` is
    None, otherwise the lines from the end back to the first one stamped more than
    ``LEDGER_SLACK`` before ``start`` (BUILD-PLAN T51). The file stays the only record;
    a budget check reads the day's tail instead of every day the ledger has kept."""
    if start is None:
        return path.read_text(errors="replace").splitlines()
    stop = start - LEDGER_SLACK
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        data = b""
        while position > 0:
            step = min(LEDGER_BLOCK, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
            # The first line of what is read may be cut short by the block's edge; only
            # the whole lines after it can say how far back the read has come.
            whole = data.split(b"\n")[1:] if position > 0 else data.split(b"\n")
            if any(_before(line, stop) for line in whole):
                break
    lines = data.decode(errors="replace").splitlines()
    return lines[1:] if position > 0 else lines


def _before(line: bytes, stop: datetime) -> bool:
    """Whether a ledger line is stamped before ``stop``; a line that cannot be read is
    not, so it never ends a read early."""
    try:
        record = json.loads(line)
    except ValueError:
        return False
    if not isinstance(record, dict):
        return False
    at = _time(record.get("at"))
    return at is not None and at < stop


def tokens_used_today(config: Config, now: datetime | None = None, purpose: str = "work") -> int:
    """Tokens of one purpose counted against today's cap: used since midnight UTC, or
    since the user last reset the budget when that was later (BUILD-PLAN F40). The
    purpose defaults to ``work``, which is what every line meant before F86 named
    one, so callers that ask for no purpose keep counting the work budget."""
    return tokens_used(config, since=budget_day_start(config, now), purpose=purpose)


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
    """Start today's counts again from now (BUILD-PLAN F40, F86). Midnight UTC still
    starts the next day as usual."""
    moment = _utc(now) if now is not None else datetime.now(UTC)
    config.home.mkdir(parents=True, exist_ok=True)
    (config.home / BUDGET_RESET).write_text(moment.isoformat() + "\n", encoding="utf-8")
    return moment


# The marker a budget reset leaves: the moment today's counts start again from.
BUDGET_RESET = "budget-reset"


def usage_path(config: Config) -> Path:
    return config.home / USAGE_LOG


def _chat(
    config: Config,
    body: dict,
    endpoint: str | None = None,
    timeout: float | None = None,
    record_cooldown: bool = True,
    purpose: str = "work",
) -> tuple[str, str | None]:
    """One HTTP call. Returns the assistant message content and the finish reason the
    endpoint reported for it, which tells a caller whose answer failed its schema
    whether the answer ran out of output tokens (BUILD-PLAN T54).

    ``timeout=None`` keeps TIMEOUT_SECONDS. ``record_cooldown=False`` leaves the
    cooldown file alone: no cooldown is set when the endpoint fails, and none is
    cleared when it answers. ``purpose`` is written into the usage ledger line the
    call's tokens are charged to.
    """
    target_endpoint = endpoint or config.settings.model.endpoint
    url = target_endpoint.rstrip("/") + "/chat/completions"
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
        wait = TIMEOUT_SECONDS if timeout is None else timeout
        with opener.open(request, timeout=wait) as response:
            raw = response.read().decode(errors="replace")
        # The endpoint answered, so it is up: no cooldown stands any longer, whatever
        # it went on to say. A caller that records no cooldown clears none either.
        if record_cooldown:
            clear_cooldown(config, endpoint=target_endpoint)
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
        # A caller that records no cooldown leaves the file alone: a slow or failing
        # search must not pause the background work a cooldown would queue behind it.
        if record_cooldown:
            if exc.code == 429:
                retry_after_val = exc.headers.get("Retry-After") if exc.headers else None
                parsed_wait = parse_retry_after(retry_after_val) if retry_after_val else None
                set_cooldown(
                    config,
                    message,
                    duration_seconds=parsed_wait,
                    endpoint=target_endpoint,
                )
            elif exc.code >= 500:
                set_cooldown(config, message, endpoint=target_endpoint)
        if exc.code >= 500:
            raise _Retryable(message) from exc
        raise ModelError(message) from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # Covers a refused connection, a DNS failure, a timeout, a reply that is not
        # HTTP, an answer cut off mid-body and a URL urllib cannot use alike: the
        # endpoint is treated as down and the work stays queued (DESIGN 14).
        message = f"cannot reach {url}: {_quoted(config, str(exc))}"
        if _is_timeout(exc):
            # A socket/read timeout on slow generation is transient, not an outage:
            # retry without immediately declaring a long hard outage cooldown.
            raise _Retryable(message) from exc
        if record_cooldown:
            set_cooldown(config, message, endpoint=target_endpoint)
        raise ModelError(message) from exc

    try:
        reply = json.loads(raw)
    except json.JSONDecodeError as exc:
        echo = _quoted(config, raw)[:200]
        raise ModelError(f"{url} did not answer with JSON: {echo}") from exc
    if not isinstance(reply, dict):
        echo = _quoted(config, raw)[:200]
        raise ModelError(f"{url} answered with JSON that is not an object: {echo}")

    _record_usage(config, started, reply.get("usage"), purpose)
    choices = reply.get("choices")
    if not isinstance(choices, list) or not choices:
        echo = _quoted(config, raw)[:200]
        raise ModelError(f"{url} returned no choices: {echo}")
    finish_reason = choices[0].get("finish_reason") if isinstance(choices[0], dict) else None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        if finish_reason == "length":
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
    return content, finish_reason if isinstance(finish_reason, str) else None


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


def _record_usage(config: Config, started: datetime, usage: Any, purpose: str = "work") -> None:
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
        "purpose": purpose,
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

"""The model client, driven by the fake endpoint: no network, no real model involved."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from aft import config as configmod
from aft import model, schema
from fakes.model_server import FakeEndpoint, Reply

SCHEMA = {
    "type": "object",
    "description": "An answer with optional numbers attached.",
    "properties": {
        "answer": {"type": "string", "maxLength": 20},
        "counts": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 5},
    },
    "required": ["answer"],
}


@pytest.fixture
def serve(home):
    """Start the fake with these replies and load a config that points at it. Every
    fake a test starts is stopped when the test ends."""
    started: list[FakeEndpoint] = []

    def start(*replies: object, api_key_env: str = "") -> tuple[FakeEndpoint, configmod.Config]:
        fake = FakeEndpoint(replies)
        fake.start()
        started.append(fake)
        return fake, point_at(home, fake.endpoint, api_key_env)

    yield start
    for fake in started:
        fake.stop()


def point_at(home: Path, endpoint: str, api_key_env: str = "") -> configmod.Config:
    """Name the endpoint in ``config.toml`` and load the config, as aft does."""
    setting = f'api_key_env = "{api_key_env}"\n' if api_key_env else ""
    with open(home / "config.toml", "a") as handle:
        handle.write(f'[model]\nendpoint = "{endpoint}"\nname = "fake-model"\n{setting}')
    return configmod.load()


def call(cfg: configmod.Config) -> dict:
    return model.complete_json(cfg, "You answer briefly.", "Is it working?", SCHEMA, 128)


def write_ledger(cfg: configmod.Config, lines: list[dict]) -> None:
    """Put records in the usage ledger, as the calls of an earlier run would have."""
    model.usage_path(cfg).write_text("".join(json.dumps(line) + "\n" for line in lines))


def test_valid_response_is_returned_as_a_dict(serve):
    fake, cfg = serve({"answer": "yes", "counts": [1, 2]})
    assert call(cfg) == {"answer": "yes", "counts": [1, 2]}
    assert len(fake.requests) == 1


def test_request_carries_the_prompt_the_cap_and_the_schema(serve):
    fake, cfg = serve({"answer": "yes"})
    call(cfg)
    sent = fake.requests[0]
    assert sent["model"] == "fake-model"
    assert sent["max_tokens"] == 128
    assert sent["temperature"] == 0.0
    assert sent["messages"] == [
        {"role": "system", "content": "You answer briefly."},
        {"role": "user", "content": "Is it working?"},
    ]
    assert sent["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "aft", "strict": True, "schema": SCHEMA},
    }


def test_request_with_json_schema_sets_strict_true(serve):
    """L11: a request with json_schema sets strict: true on the request body."""
    fake, cfg = serve({"answer": "yes"})
    call(cfg)
    sent = fake.requests[0]
    rf = sent["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True


def test_invalid_output_is_retried_once(serve):
    fake, cfg = serve({"answer": 7}, {"answer": "second"})
    assert call(cfg) == {"answer": "second"}
    assert len(fake.requests) == 2


def test_a_schema_failure_sends_a_repair_turn_and_raises_the_temperature(serve):
    """A retry is not the same ask again: the failed answer and the validator's reason go
    back to the model as a repair turn, at a temperature raised off the first ask's."""
    fake, cfg = serve({"answer": 7}, {"answer": "fixed"})
    assert call(cfg) == {"answer": "fixed"}
    assert len(fake.requests) == 2
    assert fake.requests[0]["temperature"] == 0.0
    assert fake.requests[1]["temperature"] == pytest.approx(model.RETRY_TEMPERATURE_STEP)
    assert fake.requests[1]["messages"] == [
        {"role": "system", "content": "You answer briefly."},
        {"role": "user", "content": "Is it working?"},
        {"role": "assistant", "content": json.dumps({"answer": 7})},
        {
            "role": "user",
            "content": (
                "Your previous response failed JSON schema validation: "
                "$.answer should be string, found integer. "
                "Please output valid JSON strictly conforming to the required schema."
            ),
        },
    ]


def test_the_retry_temperature_rises_by_the_step_and_stops_at_the_cap():
    assert model.retry_temperature(0.0, 0) == 0.0
    assert model.retry_temperature(0.0, 1) == pytest.approx(0.2)
    assert model.retry_temperature(0.0, 2) == pytest.approx(0.4)
    assert model.retry_temperature(0.0, 5) == pytest.approx(model.RETRY_TEMPERATURE_CAP)
    # A base above the cap is a retry's floor as well: a retry never asks colder.
    assert model.retry_temperature(0.9, 1) == pytest.approx(0.9)


def test_a_5xx_retry_is_asked_at_the_raised_temperature(serve):
    fake, cfg = serve(Reply(status=503), {"answer": "later"})
    assert call(cfg) == {"answer": "later"}
    assert fake.requests[0]["temperature"] == 0.0
    assert fake.requests[1]["temperature"] == pytest.approx(model.RETRY_TEMPERATURE_STEP)


def test_output_that_is_not_json_counts_as_invalid(serve):
    fake, cfg = serve(Reply(content="I would rather talk in prose."), {"answer": "ok"})
    assert call(cfg) == {"answer": "ok"}
    assert len(fake.requests) == 2


def test_a_raw_newline_inside_a_string_is_still_an_answer(serve):
    """A model under a JSON schema writes a raw newline or tab inside a long string often
    enough. The value is unambiguous, so it is read rather than failed and asked for again."""
    fake, cfg = serve(Reply(content='{"answer": "one\ntwo\tthree"}'))
    assert call(cfg) == {"answer": "one\ntwo\tthree"}
    assert len(fake.requests) == 1


def test_extra_body_fields_travel_with_every_request(serve, home):
    """model.extra_body adds what one endpoint needs to the request aft sends."""
    fake, cfg = serve({"answer": "ok"})
    with open(home / "config.toml", "a") as handle:
        handle.write("extra_body = { chat_template_kwargs = { enable_thinking = false } }\n")
    cfg = configmod.load()
    assert call(cfg) == {"answer": "ok"}
    sent = fake.requests[0]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["model"] == "fake-model" and sent["max_tokens"] == 128


def test_an_answer_lost_to_reasoning_names_the_setting_that_fixes_it(serve):
    """A reasoning model that spends every token thinking returns no content with
    finish_reason length. That is the endpoint's setup, so the error says how to fix it and
    the call is not repeated: a second attempt would think just as long."""
    fake, cfg = serve(Reply(null_content=True, finish_reason="length", usage=(200, 128)))
    with pytest.raises(model.ModelError, match="model.extra_body") as caught:
        call(cfg)
    assert "used all 128 tokens without answering" in str(caught.value)
    assert len(fake.requests) == 1


def test_two_invalid_answers_raise_after_one_retry(serve):
    fake, cfg = serve({"answer": "x" * 40}, {"answer": 7})
    with pytest.raises(model.ModelError) as answer:
        call(cfg)
    assert "at most 20 characters" in str(answer.value)
    assert "should be string" in str(answer.value)
    assert len(fake.requests) == 2


def test_a_5xx_is_retried_once(serve):
    fake, cfg = serve(Reply(status=503), {"answer": "later"})
    assert call(cfg) == {"answer": "later"}
    assert len(fake.requests) == 2


def test_two_5xx_answers_raise(serve):
    fake, cfg = serve(Reply(status=503), Reply(status=500))
    with pytest.raises(model.ModelError, match="503.*500"):
        call(cfg)
    assert len(fake.requests) == 2


def test_both_attempts_are_reported_when_they_fail_the_same_way(serve):
    # Two failures that read alike are still two failures: the report says so twice
    # rather than collapsing them into one.
    fake, cfg = serve({"answer": 7}, {"answer": 7})
    with pytest.raises(model.ModelError) as answer:
        call(cfg)
    assert str(answer.value).count("should be string") == 2
    assert len(fake.requests) == 2


def test_a_4xx_raises_without_retrying(serve):
    fake, cfg = serve(Reply(status=400), {"answer": "yes"})
    with pytest.raises(model.ModelError, match="400"):
        call(cfg)
    assert len(fake.requests) == 1


def test_an_endpoint_that_never_answers_raises(serve, monkeypatch):
    monkeypatch.setattr(model, "TIMEOUT_SECONDS", 0.2)
    _, cfg = serve(Reply(content={"answer": "eventually"}, delay=5.0))
    with pytest.raises(model.ModelError, match="cannot reach"):
        call(cfg)


def test_an_endpoint_that_is_down_raises(home):
    # Started and stopped, so the port is one nothing is listening on any more.
    fake = FakeEndpoint()
    fake.start()
    endpoint = fake.endpoint
    fake.stop()
    with pytest.raises(model.ModelError, match="cannot reach"):
        call(point_at(home, endpoint))


def test_an_endpoint_with_no_model_name_raises(cfg):
    with pytest.raises(model.ModelError, match="model.name"):
        call(cfg)


@pytest.mark.parametrize("endpoint", ["", "127.0.0.1:8000/v1", "http://[::1"])
def test_an_endpoint_that_is_not_a_usable_url_raises_model_error(home, endpoint):
    # An empty setting, a bare host, a truncated IPv6 address: aft reports the
    # endpoint as down, not the ValueError urllib raises over a URL it cannot use.
    with pytest.raises(model.ModelError, match="cannot reach"):
        call(point_at(home, endpoint))


def test_a_url_urllib_cannot_use_raises_model_error(home):
    # The path carries bytes that cannot go on the wire: the endpoint is down, so the
    # work stays queued rather than the caller getting a UnicodeEncodeError.
    with FakeEndpoint([{"answer": "yes"}]) as fake:
        with pytest.raises(model.ModelError, match="cannot reach"):
            call(point_at(home, f"{fake.endpoint}/chät"))
        assert fake.requests == []


@pytest.mark.parametrize(
    "raw",
    [
        b"NOT-HTTP 1.0 Confusing\r\n\r\n",
        b'HTTP/1.1 200 OK\r\nContent-Length: 200\r\n\r\n{"choices": [',
    ],
)
def test_a_reply_that_is_not_usable_http_raises_model_error(serve, raw):
    # A status line aft cannot parse, and a body cut short, both read as a down
    # endpoint: neither reaches the caller as an http.client exception.
    fake, cfg = serve(Reply(raw=raw))
    with pytest.raises(model.ModelError, match="cannot reach"):
        call(cfg)
    assert len(fake.requests) == 1


def test_a_5xx_reports_only_a_bounded_piece_of_its_error_body(serve):
    # The error body is the endpoint's to write as long as it likes; a report carries
    # 500 characters of it: this one promises 5000 bytes, sends 600, and 500 are reported.
    raw = b"HTTP/1.1 500 Oops\r\nContent-Length: 5000\r\n\r\n" + b"z" * 600
    fake, cfg = serve(Reply(raw=raw))
    with pytest.raises(model.ModelError) as answer:
        call(cfg)
    reported = str(answer.value)
    assert "returned 500" in reported
    assert "z" * 500 in reported
    assert "z" * 501 not in reported
    assert len(fake.requests) == 2


def test_a_4xx_reports_the_reason_the_endpoint_gave(serve):
    # The bound is on what a report carries, not on what the endpoint said: this reason
    # arrives short of its declared length and still reaches the report, in one attempt.
    body = b'{"error": {"message": "no api key"}}'
    raw = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 200\r\n\r\n" + body
    fake, cfg = serve(Reply(raw=raw))
    with pytest.raises(model.ModelError, match="no api key"):
        call(cfg)
    assert len(fake.requests) == 1


def test_a_redirect_is_refused_rather_than_followed(serve, monkeypatch):
    # aft posts to the endpoint the operator configured and nowhere else, so a 302 names
    # the host it tried to send the call to and stops there with the key behind it.
    monkeypatch.setenv(KEY_VARIABLE, KEY_VALUE)
    raw = (
        b"HTTP/1.1 302 Found\r\nLocation: http://somewhere-else.invalid/login\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    fake, cfg = serve(Reply(raw=raw), api_key_env=KEY_VARIABLE)
    with pytest.raises(model.ModelError) as refusal:
        call(cfg)
    assert "redirected to http://somewhere-else.invalid/login" in str(refusal.value)
    assert KEY_VALUE not in str(refusal.value)
    assert len(fake.requests) == 1


KEY_VARIABLE = "AFT_TEST_API_KEY"
KEY_VALUE = "sekret-key-value"


def test_an_endpoint_with_no_key_setting_sends_no_authorization_header(serve):
    fake, cfg = serve({"answer": "yes"})
    assert call(cfg) == {"answer": "yes"}
    assert "authorization" not in fake.headers[0]


def test_the_key_named_in_config_goes_as_a_bearer_token(serve, monkeypatch):
    monkeypatch.setenv(KEY_VARIABLE, KEY_VALUE)
    fake, cfg = serve({"answer": "yes"}, api_key_env=KEY_VARIABLE)
    assert call(cfg) == {"answer": "yes"}
    assert fake.headers[0]["authorization"] == f"Bearer {KEY_VALUE}"


def test_a_key_named_in_config_that_is_not_set_fails_naming_it(serve, monkeypatch):
    monkeypatch.delenv(KEY_VARIABLE, raising=False)
    fake, cfg = serve({"answer": "yes"}, api_key_env=KEY_VARIABLE)
    with pytest.raises(model.ModelError, match=KEY_VARIABLE):
        call(cfg)
    assert fake.requests == []


def test_an_error_from_a_keyed_call_does_not_print_the_key(serve, monkeypatch):
    # How a key reaches a report is the endpoint quoting the request that carried it: a
    # refused call quotes it in an error body, and an accepted one can echo it back in an
    # answer that then fails validation. DESIGN 13 says neither may.
    monkeypatch.setenv(KEY_VARIABLE, KEY_VALUE)
    detail = json.dumps({"error": {"message": f"refused: Bearer {KEY_VALUE}"}}).encode()
    header = b"HTTP/1.1 400 Bad Request\r\nContent-Length: " + str(len(detail)).encode()
    answer_schema = {"type": "object", "properties": {"kind": {"enum": ["note", "fact"]}}}
    fake, cfg = serve(
        Reply(raw=header + b"\r\n\r\n" + detail),
        Reply(content={"kind": KEY_VALUE}),
        api_key_env=KEY_VARIABLE,
    )
    with pytest.raises(model.ModelError) as refusal:
        call(cfg)
    assert "returned 400" in str(refusal.value)
    assert KEY_VALUE not in str(refusal.value)

    with pytest.raises(model.ModelError) as rejection:
        model.complete_json(cfg, "You answer briefly.", "Is it working?", answer_schema, 128)
    assert "should be one of" in str(rejection.value)
    assert KEY_VALUE not in str(rejection.value)
    assert len(fake.requests) == 3


def test_a_short_key_leaves_a_down_endpoint_still_named(home, monkeypatch):
    # A placeholder value exported while an endpoint is being wired up is too short to cut
    # out of prose, and the report of an endpoint that never answered still has to name
    # it: DESIGN 14 leaves the queued work to be reported with exactly that text.
    monkeypatch.setenv(KEY_VARIABLE, "dummy")
    fake = FakeEndpoint()
    fake.start()
    endpoint = fake.endpoint
    fake.stop()
    with pytest.raises(model.ModelError) as refusal:
        call(point_at(home, endpoint, KEY_VARIABLE))
    reported = str(refusal.value)
    assert "cannot reach" in reported
    assert endpoint in reported
    assert "dummy" not in reported


def test_a_short_key_withholds_only_what_the_endpoint_said(serve, monkeypatch):
    # The endpoint's own body is the one piece that can echo a key back, so a short key
    # takes that body out and nothing else: the endpoint and the status stay on the line.
    monkeypatch.setenv(KEY_VARIABLE, "dummy")
    detail = b'{"error": {"message": "no dummy key accepted"}}'
    header = b"HTTP/1.1 400 Bad Request\r\nContent-Length: " + str(len(detail)).encode()
    fake, cfg = serve(Reply(raw=header + b"\r\n\r\n" + detail), api_key_env=KEY_VARIABLE)
    with pytest.raises(model.ModelError) as refusal:
        call(cfg)
    reported = str(refusal.value)
    assert "returned 400" in reported
    assert "127.0.0.1" in reported
    assert "[details withheld]" in reported
    assert "dummy" not in reported
    assert len(fake.requests) == 1


def test_a_key_across_the_report_bound_still_does_not_appear(serve, monkeypatch):
    # A gateway that quotes the request it refused can place the key across whatever bound
    # a report cuts at, so the key comes out before the cut decides what is shown.
    monkeypatch.setenv(KEY_VARIABLE, KEY_VALUE)
    detail = b"x" * (model.REPORT_CHARS - len(KEY_VALUE) // 2) + KEY_VALUE.encode()
    header = b"HTTP/1.1 400 Bad Request\r\nContent-Length: " + str(len(detail)).encode()
    fake, cfg = serve(Reply(raw=header + b"\r\n\r\n" + detail), api_key_env=KEY_VARIABLE)
    with pytest.raises(model.ModelError) as refusal:
        call(cfg)
    reported = str(refusal.value)
    fragments = {KEY_VALUE[i : i + 4] for i in range(len(KEY_VALUE) - 3)}
    assert [piece for piece in fragments if piece in reported] == []
    assert "returned 400" in reported
    assert "127.0.0.1" in reported
    assert len(fake.requests) == 1


def test_a_ledger_that_cannot_be_written_names_the_ledger(serve, monkeypatch):
    # The answer arrived and was usable: a write that did not happen is a local failure,
    # so its report names the file and gains no endpoint and no status it never touched.
    monkeypatch.setenv(KEY_VARIABLE, "dummy")
    fake, cfg = serve({"answer": "yes"}, api_key_env=KEY_VARIABLE)
    ledger = model.usage_path(cfg)
    ledger.mkdir()
    with pytest.raises(model.ModelError) as refusal:
        call(cfg)
    reported = str(refusal.value)
    assert "cannot record token usage" in reported
    assert str(ledger) in reported
    assert "127.0.0.1" not in reported
    assert "returned" not in reported
    assert len(fake.requests) == 1


def test_every_attempt_is_charged_to_the_budget(serve):
    fake, cfg = serve(
        Reply(content={"answer": 7}, usage=(100, 20)),
        Reply(content={"answer": "ok"}, usage=(110, 30)),
    )
    call(cfg)
    assert len(fake.requests) == 2
    assert model.tokens_used(cfg) == 260
    assert model.tokens_used_today(cfg) == 260


def test_tokens_from_earlier_days_stay_out_of_todays_count(cfg):
    yesterday = datetime.now(UTC) - timedelta(days=1)
    today = datetime.now(UTC)
    lines = [
        {"at": yesterday.isoformat(), "prompt_tokens": 500, "completion_tokens": 100},
        {"at": today.isoformat(), "prompt_tokens": 50, "completion_tokens": 10},
    ]
    write_ledger(cfg, lines)
    assert model.tokens_used(cfg) == 660
    assert model.tokens_used_today(cfg, now=today) == 60


def test_a_response_that_reports_no_usage_raises_and_bills_nothing(serve):
    # Free work would keep the daily cap from ever tripping, so an unknown cost is
    # a failure, not a zero.
    fake, cfg = serve(Reply(content={"answer": "yes"}, usage=None))
    with pytest.raises(model.ModelError, match="no usable token usage"):
        call(cfg)
    assert len(fake.requests) == 1
    assert not model.usage_path(cfg).exists()


@pytest.mark.parametrize("usage", [(-5, 3), (1.5, 3), ("100", "3"), (0, 0)])
def test_usage_that_is_not_a_bill_raises_and_bills_nothing(serve, usage):
    # Two zeros is what a server with usage accounting off reports, and work billed
    # as free keeps the daily cap from ever tripping just as a missing figure does.
    _, cfg = serve(Reply(content={"answer": "yes"}, usage=usage))
    with pytest.raises(model.ModelError, match="no usable token usage"):
        call(cfg)
    assert not model.usage_path(cfg).exists()


def test_a_ledger_line_that_is_not_a_bill_is_left_out_of_the_total(cfg):
    stamp = datetime.now(UTC).isoformat()
    lines = [
        {"at": stamp, "prompt_tokens": 100, "completion_tokens": 20},
        {"at": stamp, "prompt_tokens": -100, "completion_tokens": 20},
        {"at": stamp, "prompt_tokens": 1000000.0, "completion_tokens": "500"},
        {"at": stamp, "prompt_tokens": 5},
    ]
    write_ledger(cfg, lines)
    assert model.tokens_used(cfg) == 120
    assert model.tokens_used_today(cfg) == 120


def test_a_torn_ledger_line_does_not_spoil_the_total(cfg):
    stamp = datetime.now(UTC).isoformat()
    billed = json.dumps({"at": stamp, "prompt_tokens": 100, "completion_tokens": 20})
    model.usage_path(cfg).write_bytes(
        billed.encode()
        + b'\n{"at": "'
        + b"a half byte written: \xff"
        + b"\n"
        + billed.encode()
        + b"\n"
    )
    assert model.tokens_used(cfg) == 240
    assert model.tokens_used_today(cfg) == 240


def test_a_stamp_with_no_offset_is_read_as_utc(cfg):
    now = datetime.now(UTC)
    lines = [
        {"at": now.replace(tzinfo=None).isoformat(), "prompt_tokens": 60, "completion_tokens": 40},
        {"at": now.isoformat(), "prompt_tokens": 5, "completion_tokens": 5},
    ]
    write_ledger(cfg, lines)
    assert model.tokens_used(cfg, since=now.replace(tzinfo=None)) == 110
    assert model.tokens_used_today(cfg, now=now) == 110
    assert model.tokens_used_today(cfg, now=now.replace(tzinfo=None)) == 110


def test_the_daily_window_is_counted_from_utc_midnight(cfg):
    # A caller's own offset must not shift the day: this now is 18:00 UTC, so the
    # window starts at 00:00 UTC and the earlier evening line stays out of it.
    now = datetime(2025, 3, 4, 2, 0, tzinfo=timezone(timedelta(hours=8)))
    lines = [
        {"at": "2025-03-02T20:00:00+00:00", "prompt_tokens": 70, "completion_tokens": 30},
        {"at": "2025-03-03T10:00:00+00:00", "prompt_tokens": 40, "completion_tokens": 10},
    ]
    write_ledger(cfg, lines)
    assert model.tokens_used_today(cfg, now=now) == 50


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not an object", r"\$ should be object"),
        ({"answer": 7}, r"\.answer should be string"),
        ({"answer": "ok", "counts": [1, "two"]}, r"\$\.counts\[1\] should be integer"),
        ({}, "missing required key answer"),
        ({"answer": "x" * 21}, "at most 20 characters"),
        ({"answer": "ok", "counts": []}, "at least 1 items"),
        ({"answer": "ok", "counts": [1, 2, 3, 4, 5, 6]}, "at most 5 items"),
        ({"answer": "ok", "extra": True}, None),
    ],
)
def test_validate_checks_the_keywords_the_schemas_use(value, message):
    if message is None:
        # Keywords the validator does not enforce, like extra keys, are left alone.
        assert schema.validate(value, SCHEMA) is None
        return
    with pytest.raises(schema.SchemaError, match=message):
        schema.validate(value, SCHEMA)


def test_validate_checks_enums_and_accepts_a_list_of_types():
    enum = {"type": "string", "enum": ["decision", "fact"]}
    schema.validate("fact", enum)
    with pytest.raises(schema.SchemaError, match="one of"):
        schema.validate("opinion", enum)
    nullable = {"type": ["string", "null"]}
    schema.validate(None, nullable)
    with pytest.raises(schema.SchemaError, match=r"should be string or null"):
        schema.validate(3, nullable)


@pytest.mark.parametrize(
    "declared",
    [
        None,
        "object",
        {"type": "object", "required": "answer"},
        {"type": "object", "required": None},
        {"type": "object", "properties": None},
        {"type": "string", "maxLength": True},
        {"type": "object", "properties": "answer"},
        {"type": "object", "properties": {"answer": "string"}},
        {"type": "string", "enum": "decision"},
        {"type": "string", "minLength": "3"},
        {"type": "array", "items": "string"},
        {"type": "array", "maxItems": "5"},
    ],
)
def test_a_malformed_schema_reports_as_a_schema_problem(declared):
    # Every keyword shape says so as a SchemaError, so a typo in a committed schema is
    # charged to the schema rather than arriving as an AttributeError over an answer.
    with pytest.raises(schema.SchemaError, match="the schema for"):
        schema.validate({"answer": "ok", "counts": [1]}, declared)


@pytest.mark.parametrize(
    "broken",
    [
        {"required": "answer"},
        {"type": "nonsense"},
        {"properties": {"answer": "string"}},
    ],
)
def test_a_malformed_schema_costs_no_call_and_blames_the_right_side(serve, broken):
    # The schema is the caller's own code, so a defect in it fails before the endpoint is
    # reached: never retried, never billed, never charged to the model.
    fake, cfg = serve({"answer": "ok"})
    with pytest.raises(model.ModelError) as refusal:
        model.complete_json(cfg, "You answer briefly.", "Is it working?", broken, 128)
    assert "the schema aft was given is faulty" in str(refusal.value)
    assert fake.requests == []
    assert not model.usage_path(cfg).exists()


# The cooldown: an endpoint that answered with a server error, a rate limit, or nothing
# at all is written down, and the calls after it fail without touching the network
# (BUILD-PLAN F66).


def test_a_500_sets_a_cooldown(serve):
    fake, cfg = serve(Reply(status=500))
    with pytest.raises(model.ModelError, match="500"):
        call(cfg)
    record = model.get_cooldown(cfg)
    assert record is not None
    assert record["active"] is True
    assert "500" in record["reason"]
    assert record["consecutive_errors"] >= 1
    assert datetime.fromisoformat(record["until"]) > datetime.now(UTC)
    # Both attempts of the one call met the endpoint before the record was written.
    assert len(fake.requests) == 2


def test_a_429_sets_a_cooldown(serve):
    fake, cfg = serve(Reply(status=429))
    with pytest.raises(model.ModelError, match="429"):
        call(cfg)
    record = model.get_cooldown(cfg)
    assert record is not None
    assert "429" in record["reason"]
    # A rate limit is not worth the retry a 5xx gets: one request, then the record.
    assert len(fake.requests) == 1


def test_an_endpoint_that_cannot_be_reached_sets_a_cooldown(home):
    # Started and stopped, so the port is one nothing is listening on any more.
    fake = FakeEndpoint()
    fake.start()
    endpoint = fake.endpoint
    fake.stop()
    cfg = point_at(home, endpoint)
    with pytest.raises(model.ModelError, match="cannot reach"):
        call(cfg)
    record = model.get_cooldown(cfg)
    assert record is not None
    assert "cannot reach" in record["reason"]


def test_a_call_in_cooldown_fails_fast_without_touching_the_endpoint(serve):
    fake, cfg = serve({"answer": "yes"})
    model.set_cooldown(cfg, "cannot reach http://endpoint: connection refused")
    with pytest.raises(model.ModelError) as refused:
        call(cfg)
    assert "Model endpoint is in cooldown until" in str(refused.value)
    assert "connection refused" in str(refused.value)
    assert fake.requests == []


def test_a_successful_call_clears_the_cooldown(serve):
    # A first attempt that hits a 500 and a retry that succeeds leave nothing behind:
    # the endpoint answered, so no record stands against the calls after it.
    fake, cfg = serve(Reply(status=500), {"answer": "yes"})
    assert call(cfg) == {"answer": "yes"}
    assert model.get_cooldown(cfg) is None
    assert len(fake.requests) == 2


def test_an_expired_cooldown_stands_down(serve):
    fake, cfg = serve({"answer": "yes"})
    model.set_cooldown(cfg, "cannot reach the endpoint", duration_seconds=-1.0)
    assert model.is_in_cooldown(cfg) == (False, "")
    assert call(cfg) == {"answer": "yes"}
    assert len(fake.requests) == 1


def test_clear_cooldown_ends_the_record(cfg):
    model.set_cooldown(cfg, "the endpoint answered 500")
    assert model.is_in_cooldown(cfg) == (True, "the endpoint answered 500")
    assert model.clear_cooldown(cfg) is True
    assert model.get_cooldown(cfg) is None
    assert model.is_in_cooldown(cfg) == (False, "")
    assert model.clear_cooldown(cfg) is False


def test_a_cooldown_record_that_cannot_be_read_is_no_cooldown(cfg):
    model.cooldown_path(cfg).write_text("{not json", encoding="utf-8")
    assert model.get_cooldown(cfg) is None
    assert model.is_in_cooldown(cfg) == (False, "")


def test_set_cooldown_counts_the_errors_in_a_row(cfg):
    model.set_cooldown(cfg, "first failure")
    model.set_cooldown(cfg, "second failure", duration_seconds=-1.0)
    record = model.get_cooldown(cfg)
    assert record is not None
    assert record["reason"] == "second failure"
    assert record["consecutive_errors"] == 2


def test_consecutive_errors_increase_cooldown_duration_up_to_cap(cfg):
    """Consecutive errors trigger exponential backoff until reaching the 900s cap."""
    durations: list[float] = []
    for i in range(1, 10):
        model.set_cooldown(cfg, f"failure {i}", jitter=0.0)
        record = model.get_cooldown(cfg)
        assert record is not None
        assert record["consecutive_errors"] == i
        durations.append(record["duration_seconds"])

    # Duration doubles each step until reaching 900.0s
    assert durations[0] == 30.0
    assert durations[1] == 60.0
    assert durations[2] == 120.0
    assert durations[3] == 240.0
    assert durations[4] == 480.0
    assert durations[5] == 900.0
    assert durations[6] == 900.0
    for prev, nxt in zip(durations[:5], durations[1:6], strict=True):
        assert nxt > prev


def test_consecutive_errors_with_jitter_strictly_increase_up_to_cap(cfg):
    """With default jitter, consecutive error durations still increase until cap."""
    durations: list[float] = []
    for i in range(1, 8):
        model.set_cooldown(cfg, f"failure {i}")
        record = model.get_cooldown(cfg)
        assert record is not None
        durations.append(record["duration_seconds"])

    # Each step before cap strictly increases
    for prev, nxt in zip(durations[:5], durations[1:6], strict=True):
        assert nxt > prev
    assert durations[-1] == 900.0


def test_http_429_retry_after_integer_seconds_sets_cooldown(serve):
    """HTTP 429 with Retry-After delta seconds sets cooldown duration to that value."""
    fake, cfg = serve(Reply(status=429, headers={"Retry-After": "45"}))
    with pytest.raises(model.ModelError, match="429"):
        call(cfg)
    record = model.get_cooldown(cfg)
    assert record is not None
    assert record["active"] is True
    assert record["duration_seconds"] == 45.0
    until = datetime.fromisoformat(record["until"])
    now = datetime.now(UTC)
    assert 40 <= (until - now).total_seconds() <= 50
    assert len(fake.requests) == 1


def test_http_429_retry_after_http_date_sets_cooldown(serve):
    """HTTP 429 with Retry-After HTTP date string sets cooldown to the target time."""
    target_time = datetime.now(UTC) + timedelta(seconds=75)
    date_str = format_datetime(target_time, usegmt=True)
    fake, cfg = serve(Reply(status=429, headers={"Retry-After": date_str}))
    with pytest.raises(model.ModelError, match="429"):
        call(cfg)
    record = model.get_cooldown(cfg)
    assert record is not None
    assert record["active"] is True
    until = datetime.fromisoformat(record["until"])
    now = datetime.now(UTC)
    assert 70 <= (until - now).total_seconds() <= 80
    assert len(fake.requests) == 1


def test_http_429_without_retry_after_falls_back_to_backoff(serve):
    """HTTP 429 with no Retry-After uses standard backoff."""
    fake, cfg = serve(Reply(status=429))
    with pytest.raises(model.ModelError, match="429"):
        call(cfg)
    record = model.get_cooldown(cfg)
    assert record is not None
    assert record["consecutive_errors"] == 1
    assert 30.0 <= record["duration_seconds"] <= 31.0


def test_cooldown_records_for_different_endpoints_do_not_interfere(serve, home):
    """A cooldown on a secondary verifier endpoint does not block the primary model."""
    fake, cfg = serve({"answer": "yes"})
    verifier_endpoint = "http://127.0.0.1:9999/v1"

    # Put the verifier endpoint in cooldown
    model.set_cooldown(cfg, "verifier unreachable", endpoint=verifier_endpoint)
    assert model.is_in_cooldown(cfg, endpoint=verifier_endpoint) == (True, "verifier unreachable")

    # Main model endpoint is not in cooldown
    assert model.is_in_cooldown(cfg) == (False, "")
    assert model.get_cooldown(cfg) is None

    # Main model can be called successfully
    assert call(cfg) == {"answer": "yes"}
    assert len(fake.requests) == 1

    # Verifier endpoint is still in cooldown after successful main model call
    assert model.is_in_cooldown(cfg, endpoint=verifier_endpoint) == (True, "verifier unreachable")

    # Clearing main model cooldown does not clear verifier cooldown
    assert model.clear_cooldown(cfg) is False
    assert model.is_in_cooldown(cfg, endpoint=verifier_endpoint) == (True, "verifier unreachable")

    # Clearing verifier cooldown ends its record
    assert model.clear_cooldown(cfg, endpoint=verifier_endpoint) is True
    assert model.is_in_cooldown(cfg, endpoint=verifier_endpoint) == (False, "")


def test_socket_timeout_does_not_declare_outage_cooldown(serve, monkeypatch):
    """A slow generation socket timeout retries without setting an outage cooldown."""
    monkeypatch.setattr(model, "TIMEOUT_SECONDS", 0.2)
    fake, cfg = serve(Reply(content={"answer": "slow"}, delay=2.0))
    with pytest.raises(model.ModelError, match="cannot reach"):
        call(cfg)
    # Slow generation timed out both attempts
    assert len(fake.requests) == 2
    # No cooldown is declared for a transient slow timeout
    assert model.get_cooldown(cfg) is None
    assert model.is_in_cooldown(cfg) == (False, "")

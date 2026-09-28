"""The model client, driven by the fake endpoint: no network, no real model involved."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
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

    def start(*replies: object) -> tuple[FakeEndpoint, configmod.Config]:
        fake = FakeEndpoint(replies)
        fake.start()
        started.append(fake)
        return fake, point_at(home, fake.endpoint)

    yield start
    for fake in started:
        fake.stop()


def point_at(home: Path, endpoint: str) -> configmod.Config:
    """Name the endpoint in ``config.toml`` and load the config, as aft does."""
    with open(home / "config.toml", "a") as handle:
        handle.write(f'[model]\nendpoint = "{endpoint}"\nname = "fake-model"\n')
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
    assert sent["messages"] == [
        {"role": "system", "content": "You answer briefly."},
        {"role": "user", "content": "Is it working?"},
    ]
    assert sent["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "aft", "schema": SCHEMA},
    }


def test_invalid_output_is_retried_once(serve):
    fake, cfg = serve({"answer": 7}, {"answer": "second"})
    assert call(cfg) == {"answer": "second"}
    assert len(fake.requests) == 2


def test_output_that_is_not_json_counts_as_invalid(serve):
    fake, cfg = serve(Reply(content="I would rather talk in prose."), {"answer": "ok"})
    assert call(cfg) == {"answer": "ok"}
    assert len(fake.requests) == 2


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
    # The error body is the endpoint's to write, so aft reads and reports only what
    # fits: this one promises 5000 bytes, sends 600, and 500 reach the report.
    raw = b"HTTP/1.1 500 Oops\r\nContent-Length: 5000\r\n\r\n" + b"z" * 600
    fake, cfg = serve(Reply(raw=raw))
    with pytest.raises(model.ModelError) as answer:
        call(cfg)
    reported = str(answer.value)
    assert "returned 500" in reported
    assert "z" in reported
    assert "z" * 501 not in reported
    assert len(fake.requests) == 2


def test_a_failure_message_does_not_carry_a_credential_from_the_endpoint(home):
    # A hosted endpoint addressed with a token in the URL is named by host and path in
    # the report, so the token stays in the config and out of what DESIGN 14 writes.
    with FakeEndpoint([Reply(status=400)]) as fake:
        host = fake.endpoint.split("//")[1]
        cfg = point_at(home, f"http://aft:sekret-token@{host}/v1?key=sekret-token")
        with pytest.raises(model.ModelError) as answer:
            call(cfg)
        reported = str(answer.value)
        assert "sekret-token" not in reported
        assert host in reported


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

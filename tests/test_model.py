"""The model client, driven by the fake endpoint: no network, no real model involved."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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
        "counts": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["answer"],
}


@pytest.fixture(autouse=True)
def direct_connection(monkeypatch):
    """urllib sends loopback requests through a proxy when the environment names one,
    which would take them away from the fake."""
    proxies = ("http_proxy", "https_proxy", "all_proxy")
    for name in (*proxies, *(name.upper() for name in proxies)):
        monkeypatch.delenv(name, raising=False)


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
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["schema"] == SCHEMA


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
    with pytest.raises(model.ModelError):
        call(cfg)
    assert len(fake.requests) == 2


def test_a_5xx_is_retried_once(serve):
    fake, cfg = serve(Reply(status=503), {"answer": "later"})
    assert call(cfg) == {"answer": "later"}
    assert len(fake.requests) == 2


def test_two_5xx_answers_raise(serve):
    fake, cfg = serve(Reply(status=503), Reply(status=500))
    with pytest.raises(model.ModelError, match="503"):
        call(cfg)
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
    model.usage_path(cfg).write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert model.tokens_used(cfg) == 660
    assert model.tokens_used_today(cfg, now=today) == 60


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not an object", r"\$ should be object"),
        ({"answer": 7}, r"\.answer should be string"),
        ({"answer": "ok", "counts": [1, "two"]}, r"\$\.counts\[1\] should be integer"),
        ({}, "missing required key answer"),
        ({"answer": "x" * 21}, "at most 20 characters"),
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

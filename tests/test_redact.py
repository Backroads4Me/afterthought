from __future__ import annotations

import pytest

from aft.redact import REDACTED, BadPattern, compile_patterns, redact


@pytest.mark.parametrize(
    "text",
    [
        "password = hunter2",
        '"max_tokens": 4000',
        "daily_token_budget = 200_000",
        "mysql -uroot -pNotRealPw7 widgetdb",
        "AWS_SECRET_ACCESS_KEY=notarealsecretaccesskeyvalue01",
        '{"password": "hunter2notreal"}',
        '{"api_key":"notrealapikeyvalue0123"}',
        "X-Api-Key: NotARealKeyValue12345",
    ],
)
def test_name_and_value_assignments_survive_redaction(text: str) -> None:
    sample = f"the setting was {text} in the file"
    assert redact(sample) == sample


@pytest.mark.parametrize(
    ("secret", "body"),
    [
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaApub3QtcmVhbAo=\n"
            "-----END OPENSSH PRIVATE KEY-----",
            "b3BlbnNzaApu",
        ),
        ("sk-notarealmodelkey0123456", "notarealmodelkey"),
        ("ghp_notarealgithutpat0123456789", "notarealgithutpat"),
        ("github_pat_notarealfinegrained_0123456789abcdef", "notarealfinegrained"),
        ("xoxb-not-a-real-slack-token-1234", "not-a-real-slack"),
        ("xoxp-not-a-real-slack-token-1234", "not-a-real-slack"),
        ("AKIANOTAREALKEY01234", "NOTAREALKEY"),
        ("ASIANOTAREALKEY01234", "NOTAREALKEY"),
        ("AIzaSyNotARealGoogleApiKey0123456789012345", "NotARealGoogle"),
        ("glpat-NotARealGitlabToken01", "NotARealGitlab"),
        ("npm_NotARealNpmToken0123456", "NotARealNpm"),
        (
            "Authorization: Bearer not.a.real.bearer.token.0123456789",
            "not.a.real.bearer.token",
        ),
        (
            "bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abcDEF",
            "eyJhbGciOiJIUzI1NiI",
        ),
        ("https://deploy:notrealpw@api.example.com/v1", "notrealpw"),
        ("sk_live_NotARealStripeSecretKey0123456", "NotARealStripeSecretKey"),
        ("rk_live_NotARealStripeRestrictedKey0123", "NotARealStripeRestrictedKey"),
        ("whsec_NotARealStripeWebhookSigningSecret012", "NotARealStripeWebhookSigning"),
        (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            "SflKxwRJSMeKKF2QT4fwp",
        ),
        ("hf_NotARealHuggingFaceAccessToken0123456789", "NotARealHuggingFace"),
        (
            "pypi-AgEIcHlwaS5vcmcCJGIwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMAA"
            "CJHsiYWxsb3ciOiAidGVzdCJ9",
            "CJGIwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMAA",
        ),
        (
            "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
            "T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
        ),
        (
            "SG.AbCdEf123456AbCdEf1234.AbcdEf_123-AbCdEf_123-AbCdEf_123-AbCdEf_123",
            "AbcdEf_123-AbCdEf_123-AbCdEf_123",
        ),
        (
            "dop_v1_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "0123456789abcdef0123456789",
        ),
    ],
)
def test_builtin_shapes_are_redacted(secret: str, body: str) -> None:
    text = f"the run used {secret} and moved on"
    result = redact(text)
    assert body not in result
    assert REDACTED in result


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("sk_test_NotARealStripeTestKey0123456789", id="stripe test-mode key"),
        pytest.param("rk_test_NotARealStripeTestKey0123456789", id="restricted test key"),
        pytest.param("pk_live_NotARealStripePublishableKey012", id="publishable key"),
        pytest.param("the task_live_reportfromyesterday ran", id="prefix inside a word"),
        pytest.param("eyJhbGciOiJIUzI1NiJ9", id="jwt header alone"),
        pytest.param("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0", id="jwt without a signature"),
        pytest.param("release.2024.11.02", id="ordinary dotted text"),
        pytest.param("hf_model_weights.bin", id="hf_ file name"),
        pytest.param("the hf_ prefix alone", id="hf_ prefix alone"),
        pytest.param("pypi-install-the-package-and-run-the-tests-please", id="pypi- phrase"),
        pytest.param("pypi-AgEIcHlwaS5vcmc", id="pypi header without a body"),
        pytest.param("pypi.org", id="pypi.org host"),
        pytest.param("https://hooks.slack.com/services/", id="webhook without a path"),
        pytest.param(
            "https://slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
            id="webhook on another host",
        ),
        pytest.param("see hooks.slack.com for details", id="slack host mention"),
        pytest.param("SG.short.body", id="sendgrid short parts"),
        pytest.param(
            "MSG.AbCdEf123456AbCdEf1234.AbcdEf_123-AbCdEf_123-AbCdEf_123-AbCdEf_123",
            id="sendgrid prefix inside a word",
        ),
        pytest.param(
            "SG.AbCdEf123456AbCdEf123.1234567890123456789012345678901234567890123",
            id="sendgrid part one one character short",
        ),
        pytest.param("dop_v1_0123456789abcde", id="digitalocean short hex run"),
        pytest.param(
            "dop_v1_gggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggg",
            id="digitalocean not hex",
        ),
        pytest.param("dop_v1_", id="digitalocean prefix alone"),
    ],
)
def test_prefix_lookalikes_survive_redaction(text: str) -> None:
    sample = f"the config mentioned {text} in the log"
    assert redact(sample) == sample


def test_slack_webhook_path_is_redacted_and_the_host_survives() -> None:
    url = "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
    result = redact(f"posted to {url} at once")
    assert f"https://hooks.slack.com/services/{REDACTED}" in result
    assert "XXXXXXXX" not in result


def test_builtin_redaction_false_skips_builtin_shapes() -> None:
    token = "ghp_notarealgithutpat0123456789"
    text = f"token is {token} and custom is secret-12345"
    result = redact(text, patterns=[r"secret-\d+"], builtin=False)
    assert token in result
    assert "secret-12345" not in result
    assert f"token is {token} and custom is {REDACTED}" == result


def test_compile_patterns_and_bad_patterns() -> None:
    with pytest.raises(BadPattern, match="holds nothing to match"):
        compile_patterns(["   "])
    with pytest.raises(BadPattern, match="not a regular expression"):
        compile_patterns(["[invalid"])

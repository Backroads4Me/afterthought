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
    ],
)
def test_builtin_shapes_are_redacted(secret: str, body: str) -> None:
    text = f"the run used {secret} and moved on"
    result = redact(text)
    assert body not in result
    assert REDACTED in result


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

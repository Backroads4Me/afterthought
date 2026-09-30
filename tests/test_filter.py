"""The filter over hand-built entries: spans, keeps, caps, and every secret shape.

Nothing here reads a transcript or writes a home directory. The entries are built by hand,
and the configuration comes from a temporary ``config.toml``, so the secret values in these
tests are synthetic and never reach anything real.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from aft import config as configmod
from aft.filter import CAP_MARKER, FLAG_HEAD_CHARS, MAX_CHARS, filter_entries
from aft.readers import Entry
from aft.redact import REDACTED, BadPattern

TIME = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

# A model key that could never be one: the body says what it is.
FAKE_KEY = "sk-notarealmodelkey0123456"


def said(text: str, entry_id: str = "u") -> Entry:
    return Entry(entry_id, TIME, "user", text, False)


def answered(text: str, entry_id: str = "a") -> Entry:
    return Entry(entry_id, TIME, "assistant", text, False)


def output(text: str, entry_id: str = "t", is_error: bool = False) -> Entry:
    return Entry(entry_id, TIME, "tool", text, is_error)


def kept(entries: list[Entry], config: configmod.Config) -> list[str]:
    return [entry.text for entry in filter_entries(entries, config)[0]]


@pytest.fixture
def make_config(tmp_path, monkeypatch):
    """A Config from a temporary home, with ``capture.redact_patterns`` set as given."""

    def make(redact_patterns=()) -> configmod.Config:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        (home / "config.toml").write_text(
            f'vault = "{tmp_path / "vault"}"\nhost = "testhost"\n'
            "[capture]\n"
            f"redact_patterns = {json.dumps(list(redact_patterns))}\n"
        )
        monkeypatch.setenv(configmod.HOME_ENV, str(home))
        return configmod.load()

    return make


# The forms the plan names, and the punctuation, case and spacing that do not matter.
OFF_FORMS = [
    "aft off",
    "AFT OFF",
    "aft off.",
    "aft off: ",
    "  aft off",
    "off the record",
    "Off The Record!",
]
ON_FORMS = ["aft on", "AFT ON", "aft on.", "on the record", "On The Record?"]


@pytest.mark.parametrize("marker", OFF_FORMS)
def test_each_off_marker_form_starts_a_span(marker, cfg):
    entries = [
        said("the plan", "e1"),
        said(marker, "e2"),
        said("the staging host is bellwether", "e3"),
        answered("noted", "e4"),
        said("aft on", "e5"),
        answered("back to it", "e6"),
    ]
    assert kept(entries, cfg) == ["the plan", "back to it"]


@pytest.mark.parametrize("marker", ON_FORMS)
def test_each_on_marker_form_ends_a_span(marker, cfg):
    entries = [
        said("the plan", "e1"),
        said("aft off", "e2"),
        said("the staging host is bellwether", "e3"),
        said(marker, "e4"),
        answered("back to it", "e5"),
    ]
    assert kept(entries, cfg) == ["the plan", "back to it"]


def test_a_hyphen_joined_marker_phrase_changes_nothing(cfg):
    # The markers are the phrases the design names, spelled the way it spells them. Joining
    # the words with hyphens makes another phrase, which neither opens nor closes a span.
    entries = [
        said("off-the-record", "e1"),
        said("still talking", "e2"),
        said("aft-off", "e3"),
        said("aft off", "e4"),
        said("hidden", "e5"),
        said("on-the-record", "e6"),
        answered("still hidden", "e7"),
        said("aft on", "e8"),
        said("back to it", "e9"),
    ]
    assert kept(entries, cfg) == ["off-the-record", "still talking", "aft-off", "back to it"]


def test_the_span_between_two_markers_reopens_and_closes_again(cfg):
    entries = [
        said("before", "e1"),
        said("aft off", "e2"),
        said("hidden", "e3"),
        said("aft on", "e4"),
        said("between", "e5"),
        said("aft off", "e6"),
        said("hidden again", "e7"),
        said("aft on", "e8"),
        said("after", "e9"),
    ]
    assert kept(entries, cfg) == ["before", "between", "after"]


def test_an_off_marker_repeated_inside_a_span_keeps_the_span_open(cfg):
    entries = [said("aft off", "e1"), said("aft off", "e2"), said("hidden", "e3")]
    assert kept(entries, cfg) == []


def test_a_marker_said_mid_sentence_does_nothing(cfg):
    entries = [
        said("why do we even call it off the record?"),
        said("I am not doing any aft offline thing, just notes"),
        answered("the aft off marker is documented in the design"),
        said("should the docs keep on the record wording in full"),
        said("off-the-rack deployment tools again?"),
    ]
    assert kept(entries, cfg) == [entry.text for entry in entries]


def test_an_unclosed_span_runs_to_the_end_of_the_entries_of_one_call(cfg):
    entries = [
        said("first", "e1"),
        said("aft off", "e2"),
        said("second", "e3"),
        answered("third", "e4"),
        said("fourth", "e5"),
    ]
    assert kept(entries, cfg) == ["first"]


def test_only_a_prompt_carries_a_marker(cfg):
    # A marker is the user speaking for themselves, so the assistant saying the words opens no
    # span; the design puts the markers on prompts (DESIGN 12).
    entries = [
        answered("aft off would be a name for that flag"),
        said("and the next step"),
        answered("on the record, then: the flag is --hush"),
        said("noted"),
    ]
    assert kept(entries, cfg) == [entry.text for entry in entries]


# Capture delivers one session in chunks, so a span is decided across calls rather than
# inside one: the state crosses a boundary in the caller's hands (M2.3, M2.2).


def test_a_span_left_open_by_one_chunk_still_holds_over_the_next(cfg):
    first, off_record = filter_entries(
        [said("the plan", "e1"), said("aft off", "e2"), said("a note", "e3")], cfg
    )
    assert [entry.text for entry in first] == ["the plan"]
    assert off_record is True, "the chunk ends inside the span it opened"

    second, off_record = filter_entries(
        [said("the staging host is bellwether", "e4"), answered("noted", "e5")],
        cfg,
        off_record,
    )
    assert [entry.text for entry in second] == []
    assert off_record is True, "nothing in this chunk ended the span either"


def test_the_resume_marker_of_a_later_chunk_ends_the_span_carried_to_it(cfg):
    _, off_record = filter_entries([said("aft off", "e1"), said("a note", "e2")], cfg)
    assert off_record is True

    kept_entries, off_record = filter_entries(
        [
            said("still hidden", "e3"),
            said("aft on", "e4"),
            said("back to it", "e5"),
            answered("the next step", "e6"),
        ],
        cfg,
        off_record,
    )
    assert [entry.text for entry in kept_entries] == ["back to it", "the next step"]
    assert off_record is False, "the session is on the record again from here on"


def test_a_chunk_that_closes_a_span_hands_no_state_on_to_the_next(cfg):
    # A span opened and closed inside one chunk leaves nothing for the next one to honour:
    # the boundary cuts the entries, not the promise, and what the promise needs is that a
    # span closed before the boundary stays closed.
    _, off_record = filter_entries(
        [said("aft off", "e1"), said("a note", "e2"), said("aft on", "e3")], cfg
    )
    assert off_record is False

    kept_entries, _ = filter_entries([said("back to it", "e4")], cfg, off_record)
    assert [entry.text for entry in kept_entries] == ["back to it"]


def test_a_chunk_handed_an_open_span_carries_it_on_untouched(cfg):
    # No marker, no change of state: a chunk of prose neither ends a span nor starts one.
    kept_entries, off_record = filter_entries(
        [said("the deploy notes", "e1"), answered("noted", "e2")], cfg, off_record=True
    )
    assert kept_entries == []
    assert off_record is True


def test_a_prompt_handed_an_open_span_is_dropped_before_the_rest(cfg):
    # A span that came in open is dropped by the first step of the filter, so the secret it
    # holds is never capped, never extracted from, and never redacted on its way out.
    text = f"the staging key is {FAKE_KEY}"
    kept_entries, off_record = filter_entries([said(text, "e1")], cfg, off_record=True)
    assert kept_entries == []
    assert off_record is True


def test_an_error_flag_carried_into_an_open_span_does_not_keep_the_entry(cfg):
    # Off the record outranks everything the keep step would have saved, a flagged tool
    # output included.
    text = "the run ended\nerror: the vault is locked"
    entries = [output(text, "e1", is_error=True), said("aft on", "e2"), said("after", "e3")]
    kept_entries, off_record = filter_entries(entries, cfg, off_record=True)
    assert [entry.text for entry in kept_entries] == ["after"]
    assert off_record is False


def test_a_role_the_readers_do_not_produce_adds_no_text(cfg):
    # The design keeps prompts, answers and the useful lines of tool output; a role the
    # readers never emit is none of those, marker-shaped text or not.
    assert kept([Entry("e1", TIME, "system", "aft off", False)], cfg) == []


# Each value is synthetic and says so in its own body: nothing here is a credential shape
# that could belong to anything. ``remain`` is the text that has to survive in
# "the run used <secret> and moved on".
SECRET_FORMS = [
    pytest.param(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaApub3QtcmVhbAo=\n"
        "-----END OPENSSH PRIVATE KEY-----",
        "b3BlbnNzaApu",
        ("and moved on",),
        id="private key block",
    ),
    pytest.param(FAKE_KEY, "notarealmodelkey", ("and moved on",), id="sk- key"),
    pytest.param(
        "ghp_notarealgithutpat0123456789", "notarealgithutpat", ("and moved on",), id="ghp_"
    ),
    pytest.param(
        "github_pat_notarealfinegrained_0123456789abcdef",
        "notarealfinegrained",
        ("and moved on",),
        id="github_pat_",
    ),
    pytest.param(
        "xoxb-not-a-real-slack-token-1234",
        "not-a-real-slack",
        ("and moved on",),
        id="xoxb- bot token",
    ),
    pytest.param(
        "xoxp-not-a-real-slack-token-1234",
        "not-a-real-slack",
        ("and moved on",),
        id="xoxp- user token",
    ),
    pytest.param("AKIANOTAREALKEY01234", "NOTAREALKEY", ("and moved on",), id="AWS key id"),
    pytest.param(
        # The value of the header runs to the end of the line, so the sentence around it
        # goes with it: which scheme was used is not worth a partial redaction.
        "Authorization: Bearer not.a.real.bearer.token.0123456789",
        "not.a.real.bearer.token",
        (),
        id="Authorization header",
    ),
    pytest.param(
        'password = "not a real password"', "not a real password", ("and moved on",), id="password="
    ),
    pytest.param(
        "AWS_SECRET_ACCESS_KEY=notarealsecretaccesskeyvalue01",
        "notarealsecretaccesskeyvalue",
        ("and moved on",),
        id="environment-style assignment",
    ),
    pytest.param(
        "https://deploy:notrealpw@api.example.com/v1",
        "notrealpw",
        ("api.example.com/v1", "and moved on"),
        id="credentials in a URL",
    ),
]


@pytest.mark.parametrize(("secret", "body", "remain"), SECRET_FORMS)
def test_each_secret_form_becomes_the_marker(secret, body, remain, cfg):
    text = f"the run used {secret} and moved on"
    [result] = kept([said(text)], cfg)
    assert body not in result
    assert REDACTED in result
    assert result.startswith("the run used")
    for kept_text in remain:
        assert kept_text in result


@pytest.mark.parametrize(("secret", "body", "remain"), SECRET_FORMS)
def test_no_secret_form_leaves_a_hint_of_what_it_removed(secret, body, remain, cfg):
    # The marker says nothing about length, prefix or kind, so the removed run is gone
    # whole rather than shortened.
    [result] = kept([said(f"context {secret} tail")], cfg)
    assert body[:6] not in result
    assert body[-6:] not in result


def test_a_word_that_happens_to_end_in_the_token_prefix_survives(cfg):
    # The sk- family recognizes a prefix, not any word ending in those characters: these are
    # the ticket ids and file names a build session quotes, and the archive is permanent.
    texts = [
        "TASK-2026-0115 is the ticket",
        "the task-list-view.tsx component",
        "see the risk-assessment doc",
        "no /api/v1/task-tracking endpoint changes",
    ]
    assert kept([said(text) for text in texts], cfg) == texts

    # The guard stops at a hyphen, which is not a word character: a key that a name is
    # joined to with one is still a key, and the archive keeps no half of it.
    [joined] = kept([said(f"the openai-{FAKE_KEY} is live")], cfg)
    assert joined == f"the openai-{REDACTED} is live"


def test_a_configured_pattern_adds_to_the_builtin_list(make_config):
    config = make_config([r"ACME-\d{6}"])
    text = f"the tenant is ACME-004915 and the key is {FAKE_KEY}"
    assert kept([said(text)], config) == [f"the tenant is {REDACTED} and the key is {REDACTED}"]


def test_a_configured_pattern_that_is_not_a_regex_is_an_error(make_config):
    # A pattern that cannot compile would quietly stop redacting what it was written for.
    config = make_config(["sk-[A-Z("])
    with pytest.raises(BadPattern):
        filter_entries([said("anything at all")], config)


def test_a_broken_configured_pattern_is_refused_before_anything_is_kept(make_config):
    # A stream that keeps no text still reports the pattern. Going quiet here is how a
    # capture goes on archiving exactly what the pattern was written to clear.
    config = make_config(["sk-["])
    with pytest.raises(BadPattern):
        filter_entries([said("aft off"), said("a hidden planning note")], config)


def test_an_empty_configured_pattern_is_an_error(make_config):
    # An empty pattern matches between every character, so an empty entry in
    # capture.redact_patterns would shred every kept text rather than redact nothing.
    for source in ("", "   "):
        with pytest.raises(BadPattern):
            filter_entries([said("an ordinary prompt")], make_config([source]))


def test_redaction_runs_on_every_path_that_returns_kept_text(cfg):
    entries = [
        said(f"my key is {FAKE_KEY}", "e1"),
        answered(f"understood, {FAKE_KEY} it is", "e2"),
        output(f"running the job\nerror: auth rejected for {FAKE_KEY}\nthat is all", "e3"),
    ]
    texts = kept(entries, cfg)
    assert len(texts) == 3
    assert all(FAKE_KEY not in text for text in texts)
    assert all(REDACTED in text for text in texts)


def test_redaction_runs_on_the_remnant_of_a_cap(cfg):
    text = f"the run used {FAKE_KEY} first\n" + "padded here " * 300
    [result] = kept([said(text)], cfg)
    assert FAKE_KEY not in result
    assert REDACTED in result
    assert result.endswith(CAP_MARKER)


def test_an_entry_at_the_cap_is_untouched(cfg):
    text = "abcd " * (MAX_CHARS // 5)
    assert len(text) == MAX_CHARS
    assert kept([said(text)], cfg) == [text]


def test_an_entry_just_under_the_cap_is_untouched(cfg):
    text = "abcd " * 299 + "ab"
    assert len(text) == MAX_CHARS - 3
    assert kept([said(text)], cfg) == [text]


def test_the_cap_cuts_at_a_word_boundary_and_marks_the_cut(cfg):
    text = "abcd " * 301
    assert len(text) == MAX_CHARS + 5
    assert kept([said(text)], cfg) == ["abcd " * 299 + "abcd" + CAP_MARKER]


def test_a_cut_that_would_land_inside_a_word_takes_the_whole_word(cfg):
    # A hard cut at 1,500 would leave "extraord" behind; the boundary rule drops the word.
    text = "ab " * 499 + "extraordinaryword"  # the word straddles the limit
    assert len(text) > MAX_CHARS
    assert kept([said(text)], cfg) == ["ab " * 498 + "ab" + CAP_MARKER]


def test_a_cut_that_would_land_inside_a_key_run_redacts_what_remains(cfg):
    body = "b3BlbnNzaC1rZXktdjE0IG5vdC1yZWFsCg=="
    text = "the deploy log said: -----BEGIN RSA PRIVATE KEY-----\n" + body + "\n" + body
    [result] = kept([said(text)], cfg)
    assert result == "the deploy log said: [redacted]"
    assert "PRIVATE KEY" not in result
    assert body not in result


ERROR_LINES = [
    "error: cannot open the lock file",
    "FAILED tests/test_water.py::test_valve",
    "Traceback (most recent call last):",
    "ValueError: could not parse the schedule",
    "exception in worker 3",
    "the pipeline failed after 3 steps",
    "subprocess returned non-zero exit status 1",
    "the command exited with code 2",
]
QUIET_LINES = [
    "collected 12 items",
    "the command exited with code 0",
    "reading config from the home directory",
    "connected to 10.0.0.1:8080 and it answered",
]


def test_a_tool_entry_keeps_only_the_lines_the_design_names(cfg):
    text = "\n".join(["listing the directory", *ERROR_LINES, *QUIET_LINES, "done"])
    assert kept([output(text)], cfg) == ["\n".join(ERROR_LINES)]


def test_a_tool_entry_keeps_a_line_that_only_names_a_place(cfg):
    text = "14 files checked\nsrc/aft/filter.py:112\n2 files skipped"
    assert kept([output(text)], cfg) == ["src/aft/filter.py:112"]


def test_a_reference_without_an_extension_keeps_its_line(cfg):
    # The design keeps path:line references, and the places a build session points at are not
    # all named for their extension.
    text = "14 files checked\nMakefile:12\nsrc/aft/cli:5\n2 files skipped"
    assert kept([output(text)], cfg) == ["Makefile:12\nsrc/aft/cli:5"]


def test_a_time_and_a_host_with_a_port_are_not_references(cfg):
    text = "\n".join(
        [
            "the job ran at 12:30 and finished",
            "GET https://api.example.com:8443/v1/health answered 200",
            "listening on 0.0.0.0:9100 since boot",
        ]
    )
    assert kept([output(text)], cfg) == []


def test_a_reference_that_is_itself_a_credential_shape_keeps_its_line(cfg):
    # A dump file named after the key it holds. The line's only claim on the filter is the
    # path:line reference, and the reference is the credential: extraction runs first, so
    # the line is kept and redaction then takes the key out of its name. Run the other way
    # round, what redaction leaves of the name is no reference at all and the line goes
    # with the noise - which is how a redaction-first filter loses a failure report.
    text = "wrote the record\n/tmp/agent/AKIANOTAREALKEY01234.json:8\n3 files skipped"
    assert kept([output(text)], cfg) == ["/tmp/agent/[redacted].json:8"]
    # A credential in a directory of the path leaves the rest of the reference readable.
    text = f"wrote the build log\nlogs/{FAKE_KEY}/handler.py:12"
    assert kept([output(text)], cfg) == ["logs/[redacted]/handler.py:12"]


def test_a_tool_entry_keeps_its_identity_and_its_error_flag(cfg):
    [entry], _ = filter_entries([output("listing files\nerror: nope", "t-77", is_error=True)], cfg)
    assert (entry.id, entry.role, entry.time, entry.is_error) == ("t-77", "tool", TIME, True)
    # The agent flagged this output, so its head is kept whole: all 24 characters of it.
    assert entry.text == "listing files\nerror: nope"


def test_an_unflagged_tool_output_with_nothing_to_report_is_dropped(cfg):
    # An output the agent did not flag, with no failing line and no place to point at, is
    # something a later session could re-run for itself, so the entry goes rather than
    # becoming an empty one. One the agent did flag is kept whole by design: see
    # test_a_flagged_output_keeps_its_head_when_no_line_matches.
    assert kept([output("all tests passed\n34 seconds\nno changes")], cfg) == []


# What an entry the agent flagged as an error keeps is the other half of the ruled keep step
# (M2.3): the agent's own verdict makes the output itself into evidence.


def test_a_flagged_output_keeps_its_head_when_no_line_matches(cfg):
    # The plan's own case: an output the agent flagged, with no line any pattern names.
    # Its first 500 characters are kept, by that number and only those, with the marker
    # that says the head was cut short of the rest.
    quiet = "collected 12 items\nall quiet on the worker\nno changes detected\n" * 40
    assert len(quiet) > FLAG_HEAD_CHARS
    [text] = kept([output(quiet, "t1", is_error=True)], cfg)
    head = text.removesuffix(CAP_MARKER)
    assert text.endswith(CAP_MARKER), "a head cut short says it is cut"
    assert head == quiet[:FLAG_HEAD_CHARS], "the head is those 500 characters, by the number"


def test_a_flagged_output_keeps_the_matching_lines_below_its_head(cfg):
    # Below the head the ordinary rule still runs, so what a long dump adds is its failure
    # lines and its references rather than the rest of the dump.
    quiet = "collected 12 items\nno changes detected\n" * 40
    below = "error: the pipe broke\nnothing to report\nsrc/aft/queue.py:41\nthe end"
    text = quiet + below
    [kept_text] = kept([output(text, "t1", is_error=True)], cfg)
    head, lines = kept_text.split(CAP_MARKER, 1)
    assert lines == "\nerror: the pipe broke\nsrc/aft/queue.py:41"
    assert text.startswith(head)
    assert len(head) == FLAG_HEAD_CHARS, "the head is the budget, by the number"


def test_the_error_flag_is_what_lets_an_outputs_head_through(cfg):
    # The same text, flagged and unflagged: the flag is the whole difference between an
    # entry the archive can quote and the few lines its patterns happen to name.
    text = "listing the directory\nno changes detected\nerror: the vault is locked"
    [flagged] = kept([output(text, "t1", is_error=True)], cfg)
    [unflagged] = kept([output(text, "t2")], cfg)
    assert flagged == text
    assert unflagged == "error: the vault is locked"


def test_a_flagged_output_is_capped_like_every_other_kept_text(cfg):
    # A head plus the failure lines of a long dump can run past the budget. The same cap
    # that bounds every other kept text bounds this one, and cuts it on a word: the long
    # word here crosses the 1,500 limit, so it goes whole rather than leaving half of it
    # archived. The head is the first 500 of the z run, none of which matches a pattern.
    dump = "error: " + "ab " * 329 + "extraordinaryword"
    [text] = kept([output("z" * 600 + "\n" + dump, "t1", is_error=True)], cfg)
    assert text.endswith(CAP_MARKER)
    assert "extraordinaryword" not in text
    assert text.startswith("z" * FLAG_HEAD_CHARS)
    assert len(text) <= MAX_CHARS + len(CAP_MARKER)


def test_a_flagged_output_is_redacted_like_every_other_kept_text(cfg):
    text = f"the deploy failed\nkey {FAKE_KEY} was rejected for the run"
    [text] = kept([output(text, "t1", is_error=True)], cfg)
    assert FAKE_KEY not in text
    assert REDACTED in text
    assert "the deploy failed" in text


def test_a_secret_that_straddles_the_head_cut_leaves_no_fragment_behind(cfg):
    # The budget cuts at 500 whatever sits there, so redaction runs first: the key is the
    # marker before the cut reaches it, and no piece of a key can be left behind.
    text = "the deploy failed\n" + "word " * 94 + "one " + FAKE_KEY + " was rejected"
    start = text.index(FAKE_KEY)
    assert start < FLAG_HEAD_CHARS < start + len(FAKE_KEY), "the key straddles the cut"
    [kept_text] = kept([output(text, "t1", is_error=True)], cfg)
    assert kept_text.endswith(CAP_MARKER), "the head says it stopped short"
    assert "sk-" not in kept_text, "no piece of the key reaches the archive"


def test_a_flagged_output_holding_nothing_at_all_is_still_dropped(cfg):
    # The head of an empty output is empty: there is nothing to keep, flagged or not.
    assert kept([output("", "t1", is_error=True)], cfg) == []


def test_a_flagged_output_of_nothing_but_space_keeps_nothing(cfg):
    # Space is no more evidence than nothing: a flagged entry keeps neither a blank text
    # nor a cap marker describing content that is not in it.
    assert kept([output("   ", "t1", is_error=True)], cfg) == []
    long_blank = "\n" * 600 + "error: the pipe broke\n"
    assert kept([output(long_blank, "t2", is_error=True)], cfg) == ["error: the pipe broke"]


def test_a_flagged_output_whose_run_outruns_the_head_budget_keeps_its_head(cfg):
    # Compact JSON leaves no whitespace for a cut to land on: a flagged error whose first
    # 500 characters are one unbroken run is cut by the budget, not dropped for a word.
    text = "\n" + '{"a":"b",' * 125
    assert len(text) > FLAG_HEAD_CHARS
    [kept_text] = kept([output(text, "t1", is_error=True)], cfg)
    assert kept_text == text[:FLAG_HEAD_CHARS] + CAP_MARKER


def test_entries_keep_their_order_and_every_field_but_their_text(cfg):
    entries = [
        said("one", "u1"),
        output("listing\nerror: x", "t2", is_error=True),
        answered("two", "a3"),
        output("nothing to see here", "t4"),
        said("three", "u5"),
    ]
    result, _ = filter_entries(entries, cfg)
    assert [(entry.id, entry.role) for entry in result] == [
        ("u1", "user"),
        ("t2", "tool"),
        ("a3", "assistant"),
        ("u5", "user"),
    ]
    assert [entry.is_error for entry in result] == [False, True, False, False]
    assert [entry.time for entry in result] == [TIME] * 4


def test_a_secret_inside_a_dropped_span_never_appears(cfg):
    # The span goes before anything else touches it, so the secret in it is never even
    # looked at: the one marker in the output stands for the one secret on a kept path.
    entries = [
        said("aft off", "e1"),
        said(f"the staging key is {FAKE_KEY}", "e2"),
        answered(f"noted, {FAKE_KEY} is staging only", "e3"),
        said("aft on", "e4"),
        said(f"and the model key is {FAKE_KEY}", "e5"),
    ]
    result, _ = filter_entries(entries, cfg)
    assert [entry.text for entry in result] == [f"and the model key is {REDACTED}"]
    assert result[0].text.count(REDACTED) == 1


def test_a_marker_phrase_beyond_the_cap_does_not_open_a_span(cfg):
    # The phrase sits past 1,500 characters, where the cap will cut it away. It is prose
    # either way, so this prompt is kept and capped and the session goes on. A span
    # searched for anywhere in an entry, or searched for in text the cap had already
    # trimmed, both take the rest of the session with them.
    text = "the deploy notes for tonight. " * 55 + " off the record, nothing here."
    entries = [said(text, "e1"), answered("noted", "e2")]
    texts = kept(entries, cfg)
    assert texts[1] == "noted"
    assert texts[0].endswith(CAP_MARKER)
    assert "off the record" not in texts[0]


def test_a_long_prompt_that_begins_with_a_marker_is_dropped_whole(cfg):
    # Nothing of a dropped span comes back, not the capped remnant the cap would have
    # produced from a prompt this long.
    text = "aft off " + "secret planning note. " * 200
    assert kept([said(text, "e1"), answered("as you wish", "e2")], cfg) == []

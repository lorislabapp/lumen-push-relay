"""Tests for MatchEvaluator (any/equals/regex with optional JSONPath)."""
import pytest

from src.auto_call.buttons.match import MatchEvaluator
from src.auto_call.buttons.models import MatchRule


def make_rule(pattern_type, pattern_value=None, path=None):
    return MatchRule(path=path, pattern_type=pattern_type, pattern_value=pattern_value)


@pytest.fixture
def m():
    return MatchEvaluator()


# --- any ---

def test_any_pattern_matches_any_payload(m):
    assert m.matches(make_rule("any"), b"anything")
    assert m.matches(make_rule("any"), b"")
    assert m.matches(make_rule("any"), b'{"action":"single"}')


# --- equals on full payload ---

def test_equals_full_payload_match(m):
    assert m.matches(make_rule("equals", "ring"), b"ring")
    assert not m.matches(make_rule("equals", "ring"), b"ringggg")
    assert not m.matches(make_rule("equals", "ring"), b"")


# --- equals with JSONPath ---

def test_equals_jsonpath_simple_key(m):
    assert m.matches(
        make_rule("equals", "single", path="$.action"),
        b'{"action":"single"}',
    )
    assert not m.matches(
        make_rule("equals", "single", path="$.action"),
        b'{"action":"hold"}',
    )


def test_equals_jsonpath_nested(m):
    assert m.matches(
        make_rule("equals", "yes", path="$.foo.bar"),
        b'{"foo":{"bar":"yes"}}',
    )


def test_equals_jsonpath_array_index(m):
    assert m.matches(
        make_rule("equals", "first", path="$.arr[0]"),
        b'{"arr":["first","second"]}',
    )


def test_equals_jsonpath_missing_key(m):
    assert not m.matches(
        make_rule("equals", "x", path="$.missing"),
        b'{"action":"single"}',
    )


def test_equals_jsonpath_invalid_json(m):
    assert not m.matches(
        make_rule("equals", "x", path="$.action"),
        b"not-json",
    )


def test_equals_numeric_value_coerced_to_string(m):
    # Shelly publishes {"input": 1, ...} — JSON int — match against "1".
    assert m.matches(
        make_rule("equals", "1", path="$.input"),
        b'{"input":1}',
    )


# --- regex ---

def test_regex_full_payload_match(m):
    assert m.matches(make_rule("regex", "^press_.+"), b"press_1")
    assert not m.matches(make_rule("regex", "^press_.+"), b"release_1")


def test_regex_invalid_pattern_returns_false(m):
    # "(" is invalid regex — should not raise.
    assert not m.matches(make_rule("regex", "("), b"anything")


def test_regex_too_long_pattern_rejected(m):
    long_pat = "a" * 257
    assert not m.matches(make_rule("regex", long_pat), b"a" * 257)


def test_regex_with_jsonpath(m):
    assert m.matches(
        make_rule("regex", r"^single.*$", path="$.action"),
        b'{"action":"single_press"}',
    )


# --- edge cases ---

def test_unknown_pattern_type_returns_false(m):
    assert not m.matches(make_rule("xyz", "v"), b"v")


def test_payload_decode_invalid_utf8_replaces(m):
    # Invalid UTF-8 byte sequence — should not raise.
    assert m.matches(make_rule("any"), b"\xff\xfe\x00\xff")

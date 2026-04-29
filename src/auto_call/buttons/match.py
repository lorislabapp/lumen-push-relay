"""Match evaluator for physical-button MQTT payloads.

Supports three pattern types:
- "any":    any message on the topic matches.
- "equals": exact string equality (against full payload OR a JSONPath result).
- "regex":  re.search on full payload OR JSONPath result. Length-capped on both
            pattern (REGEX_MAX_LEN=256) and target (TARGET_MAX_LEN=65536); invalid
            patterns silently return False (cached). Length cap is the v1 ReDoS
            mitigation — a third-party `regex` package with timeout flag is the
            v2 plan if pathological patterns become an issue.

JSONPath subset (no external deps):
- "$.foo"         dict key
- "$.foo.bar"     nested key
- "$.arr[0]"      array index
- "$.arr[0][1]"   nested array index
No wildcards, no filters, no bracket-quoted keys. If users want more, regex
on the full payload covers it. iOS-side validation must mirror this restriction.
"""
import json
import logging
import re
from typing import Any, Optional

from .models import MatchRule

log = logging.getLogger("lumen.auto_call.buttons.match")

REGEX_MAX_LEN = 256
TARGET_MAX_LEN = 65536

_TOKEN_RE = re.compile(r"\.([A-Za-z_][\w]*)|\[(\d+)\]")


def _eval_jsonpath(path: str, payload_text: str) -> Optional[str]:
    """Evaluate a JSONPath subset against a JSON payload.

    Returns the value as a string (json.dumps for non-strings without quotes
    around primitives), or None if the path doesn't resolve.
    """
    if not path.startswith("$"):
        return None
    if path == "$":
        return None
    try:
        obj: Any = json.loads(payload_text)
    except (json.JSONDecodeError, ValueError):
        return None

    rest = path[1:]
    cur: Any = obj
    pos = 0
    while pos < len(rest):
        m = _TOKEN_RE.match(rest, pos)
        if not m:
            return None
        key, idx = m.group(1), m.group(2)
        if key is not None:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        else:
            i = int(idx)
            if not isinstance(cur, list) or i < 0 or i >= len(cur):
                return None
            cur = cur[i]
        pos = m.end()

    if isinstance(cur, str):
        return cur
    if isinstance(cur, bool):
        return "true" if cur else "false"
    if cur is None:
        return "null"
    return str(cur)


class MatchEvaluator:
    """Stateless evaluator with a regex-compile cache.

    `_cache` maps a regex source string to either a compiled `re.Pattern`
    or the sentinel `False` (invalid pattern — don't retry compile).
    """

    def __init__(self) -> None:
        self._cache: dict[str, re.Pattern | bool] = {}

    def matches(self, rule: MatchRule, payload: bytes) -> bool:
        text = payload.decode("utf-8", errors="replace")
        target: Optional[str]
        if rule.path:
            target = _eval_jsonpath(rule.path, text)
            if target is None:
                return False
        else:
            target = text

        if rule.pattern_type == "any":
            return True
        if rule.pattern_type == "equals":
            return target == (rule.pattern_value or "")
        if rule.pattern_type == "regex":
            return self._regex_match(rule.pattern_value or "", target)
        return False

    def _regex_match(self, pattern: str, target: str) -> bool:
        if not pattern or len(pattern) > REGEX_MAX_LEN:
            return False
        if len(target) > TARGET_MAX_LEN:
            return False
        cached = self._cache.get(pattern)
        if cached is False:
            return False
        if cached is None:
            try:
                cached = re.compile(pattern)
                self._cache[pattern] = cached
            except re.error as e:
                log.debug("invalid regex %r: %s", pattern, e)
                self._cache[pattern] = False
                return False
        try:
            return bool(cached.search(target))
        except re.error as e:
            log.debug("regex search raised on %r: %s", pattern, e)
            return False

"""Fail-closed Frigate Review correlation for notification producers.

Raw ``frigate/events`` messages are evidence inputs, never notification
authority.  A notification envelope is emitted only after a ``frigate/reviews``
message references a cached detection and the Review/event identities agree.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import logging
import time
from typing import Any, Callable


VALID_SEVERITIES = frozenset({"detection", "alert"})
# Frigate publishes "new", "update" and "end" on both frigate/events and
# frigate/reviews. The final top_score of a tracked object frequently arrives
# only on "end", so "end" is evidence too.
ACCEPTED_MESSAGE_TYPES = frozenset({"new", "update", "end"})
# A Review that is still open stays eligible this long after it started, even
# when its confidence crosses the threshold late. Fresh activity is still
# required from the detection itself (max_age_seconds).
DEFAULT_MAX_OPEN_SECONDS = 900.0
# After a Review ends, wait this long for a straggling event "end" message
# before finalizing it as not sent.
DEFAULT_END_GRACE_SECONDS = 30.0

log = logging.getLogger("frigate-push")


@dataclass(frozen=True)
class ReviewEnvelope:
    payload: dict[str, Any]


class ReviewCorrelator:
    def __init__(
        self,
        *,
        source: str = "cloudflare-relay",
        max_age_seconds: float = 120,
        future_skew_seconds: float = 30,
        max_events: int = 2048,
        max_pending_reviews: int = 256,
        state_path: str | Path | None = None,
        event_filter: Callable[[dict[str, Any]], bool] | None = None,
        event_rejection: Callable[[dict[str, Any]], str | None] | None = None,
        allowed_severities: frozenset[str] = VALID_SEVERITIES,
        max_open_seconds: float = DEFAULT_MAX_OPEN_SECONDS,
        end_grace_seconds: float = DEFAULT_END_GRACE_SECONDS,
    ) -> None:
        if not allowed_severities or not allowed_severities <= VALID_SEVERITIES:
            raise ValueError("review_severities must contain alert and/or detection")
        self.event_filter = event_filter
        # Optional: returns None when the event may notify, otherwise a stable
        # reason code. Takes precedence over event_filter and feeds the
        # "why was this Review not sent" log.
        self.event_rejection = event_rejection
        self.max_open_seconds = max(float(max_open_seconds), float(max_age_seconds))
        self.end_grace_seconds = float(end_grace_seconds)
        self.allowed_severities = allowed_severities
        self._notified: OrderedDict[str, float] = OrderedDict()
        self.source = source
        self.max_age_seconds = max_age_seconds
        self.future_skew_seconds = future_skew_seconds
        self.max_events = max_events
        self.max_pending_reviews = max_pending_reviews
        self.state_path = Path(state_path) if state_path else None
        self._events: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._pending: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._delivered: OrderedDict[str, float] = OrderedDict()
        self._inflight: set[str] = set()
        # Frigate resolves a face or writes a GenAI description seconds after
        # the detection, on frigate/tracked_object_update. Keep the latest per
        # event so a Review that resolves later already carries them, and so a
        # Review already sent can be followed by exactly one enrichment.
        self._identity: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._enriched: OrderedDict[str, float] = OrderedDict()
        self._load_state()

    def ingest_event_message(self, message: Any, *, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        if not isinstance(message, dict) or message.get("type") not in ACCEPTED_MESSAGE_TYPES:
            return []
        event = message.get("after")
        if not isinstance(event, dict) or not self._valid_event(event, now):
            self._expire(now)
            return []
        event_id = event["id"]
        previous = self._events.get(event_id, {})
        self._events[event_id] = dict(event)
        if message.get("type") == "end":
            self._events[event_id]["_ended"] = True
        for key in ("_review_id", "_review_severity"):
            if key in previous:
                self._events[event_id][key] = previous[key]
        self._events.move_to_end(event_id)
        self._trim(self._events, self.max_events)

        ready: list[dict[str, Any]] = []
        for review_id, review in list(self._pending.items()):
            if event_id in self._detection_ids(review):
                resolved = self._resolve(review, now)
                ready.extend(resolved)
                if (not resolved and review.get("_ended_at") is not None
                        and self._all_detections_ended(review)):
                    self._finalize(review_id, now)
        self._expire(now)
        return ready

    def ingest_review_message(self, message: Any, *, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        if not isinstance(message, dict) or message.get("type") not in ACCEPTED_MESSAGE_TYPES:
            return []
        review = message.get("after")
        if not isinstance(review, dict) or not self._valid_review(review, now):
            self._expire(now)
            return []
        review_id = review["id"]
        delivery_key = f"{review_id}/{review['severity']}"
        if delivery_key in self._delivered or delivery_key in self._inflight:
            # Already sent (or being sent) at this severity: never resend and
            # never re-queue it as pending.
            self._expire(now)
            return []
        resolved = self._resolve(review, now)
        if resolved:
            return resolved
        pending = dict(review)
        previous = self._pending.get(review_id)
        if message.get("type") == "end" or (previous or {}).get("_ended_at") is not None:
            pending["_ended_at"] = (previous or {}).get("_ended_at", now)
        self._pending[review_id] = pending
        self._pending.move_to_end(review_id)
        while len(self._pending) > self.max_pending_reviews:
            evicted_id, evicted = self._pending.popitem(last=False)
            self._log_not_sent(evicted_id, evicted, "capacity", now)
        if pending.get("_ended_at") is not None and self._all_detections_ended(pending):
            # Review closed and every referenced detection has reported its
            # final score: nothing can still make it eligible.
            self._finalize(review_id, now)
        self._expire(now)
        return []

    def ingest_identity_update(self, message: Any, *, now: float | None = None) -> list[dict[str, Any]]:
        """Absorb a frigate/tracked_object_update message.

        Returns an enrichment envelope only when the Review for that event has
        already been delivered — the notification is on the user's screen and
        needs its title or body corrected. Otherwise the new identity is simply
        remembered so the first delivery already carries it.
        """
        now = time.time() if now is None else now
        if not isinstance(message, dict):
            return []
        event_id = message.get("id")
        if not isinstance(event_id, str) or not event_id:
            return []

        update_type = message.get("type")
        fields: dict[str, Any] = {}
        if update_type == "face":
            name = message.get("name")
            if isinstance(name, str) and name:
                fields["sub_label"] = name[:128]
        elif update_type == "description":
            description = message.get("description")
            if isinstance(description, str) and description:
                fields["description"] = description[:500]
        if not fields:
            return []

        known = self._identity.get(event_id, {})
        known.update(fields)
        self._identity[event_id] = known
        self._identity.move_to_end(event_id)
        self._trim(self._identity, self.max_events)

        event = self._events.get(event_id)
        if not event:
            return []
        review_id = event.get("_review_id")
        severity = event.get("_review_severity")
        if not isinstance(review_id, str) or not isinstance(severity, str):
            return []
        delivery_key = f"{review_id}/{severity}"
        if delivery_key not in self._notified or delivery_key in self._enriched:
            return []

        if not self._valid_event(event, now):
            return []
        if self._rejection(event) is not None:
            return []

        # One correction per event: a face and a description arriving moments
        # apart must not buzz the phone twice.
        self._enriched[delivery_key] = now
        self._trim(self._enriched, self.max_pending_reviews)

        envelope: dict[str, Any] = {
            "source": self.source,
            "sourceSchema": "frigate-review-v1",
            "reviewId": review_id,
            "reviewSeverity": severity,
            "eventId": event_id,
            "camera": event["camera"],
            "label": event["label"],
            "detected_at": float(event["start_time"]),
            "score": float(event["top_score"]),
            "zones": list(dict.fromkeys(event.get("current_zones") or event.get("zones") or []))[:32],
            "isEnrichment": True,
        }
        for key in ("stationary", "has_clip"):
            if isinstance(event.get(key), bool):
                envelope[key] = event[key]
        envelope.update(known)
        return [envelope]

    def _resolve(self, review: dict[str, Any], now: float) -> list[dict[str, Any]]:
        if not self._valid_review(review, now) or review["severity"] not in self.allowed_severities:
            return []
        review_id = review["id"]
        severity = review["severity"]
        delivery_key = f"{review_id}/{severity}"
        if delivery_key in self._delivered or delivery_key in self._inflight:
            return []

        candidates = [self._events[event_id] for event_id in self._detection_ids(review) if event_id in self._events]
        candidates = [event for event in candidates if self._valid_event(event, now)
                      and event["camera"] == review["camera"]
                      and event["start_time"] + self.future_skew_seconds >= review["start_time"]
                      and self._rejection(event) is None]
        if not candidates:
            return []
        event = max(candidates, key=lambda item: (float(item.get("top_score", 0)), str(item.get("id", ""))))

        review_camera = review.get("camera")
        if isinstance(review_camera, str) and review_camera and event.get("camera") != review_camera:
            return []
        review_start = review.get("start_time")
        event_start = event.get("start_time")
        if isinstance(review_start, (int, float)) and isinstance(event_start, (int, float)):
            if event_start + self.future_skew_seconds < review_start:
                return []

        envelope: dict[str, Any] = {
            "source": self.source,
            "sourceSchema": "frigate-review-v1",
            "reviewId": review_id,
            "reviewSeverity": severity,
            "eventId": event["id"],
            "camera": event["camera"],
            "label": event["label"],
            "detected_at": float(event["start_time"]),
            "score": float(event["top_score"]),
            "zones": list(dict.fromkeys(event.get("current_zones") or event.get("zones") or []))[:32],
        }
        for source_key in ("sub_label", "compressed_snapshot", "custom_title", "custom_body"):
            value = event.get(source_key)
            if isinstance(value, str) and value:
                envelope[source_key] = value

        # A face recognised after the detection is fresher than the event's own
        # sub_label, so it wins. The description has no other source.
        identity = self._identity.get(event["id"])
        if identity:
            name = identity.get("sub_label")
            if isinstance(name, str) and name:
                envelope["sub_label"] = name[:128]
            description = identity.get("description")
            if isinstance(description, str) and description:
                envelope["description"] = description[:500]

        # Frigate 0.17 writes a short GenAI summary onto the review itself.
        metadata = review.get("data", {}).get("metadata") if isinstance(review.get("data"), dict) else None
        if "description" not in envelope and isinstance(metadata, dict):
            summary = metadata.get("title") or metadata.get("shortSummary")
            if isinstance(summary, str) and summary:
                envelope["description"] = summary[:500]
        for source_key in ("stationary", "has_clip"):
            value = event.get(source_key)
            if isinstance(value, bool):
                envelope[source_key] = value
        event_data = event.get("data")
        if isinstance(event_data, dict):
            plate = event_data.get("recognized_license_plate")
            if isinstance(plate, str) and 0 < len(plate) <= 64:
                envelope["recognized_plate"] = plate

        # Remember the review this event was delivered under, so a later
        # identity update knows which notification to correct.
        event["_review_id"] = review_id
        event["_review_severity"] = severity

        self._pending[review_id] = dict(review)
        self._pending.move_to_end(review_id)
        self._trim(self._pending, self.max_pending_reviews)
        self._inflight.add(delivery_key)
        return [envelope]

    def _rejection(self, event: dict[str, Any]) -> str | None:
        """None when the event passes local policy, else a reason code."""
        if self.event_rejection is not None:
            return self.event_rejection(event)
        if self.event_filter is not None and not self.event_filter(event):
            return "filtered"
        return None

    def _all_detections_ended(self, review: dict[str, Any]) -> bool:
        ids = self._detection_ids(review)
        return bool(ids) and all(self._events.get(i, {}).get("_ended") for i in ids)

    def explain_not_sent(self, review: dict[str, Any], now: float, *, cap_reached: bool = False) -> str:
        """Best-effort reason code for a pending Review that was never sent."""
        if review.get("severity") not in self.allowed_severities:
            return "severity"
        cached = [self._events[i] for i in self._detection_ids(review) if i in self._events]
        cached = [e for e in cached if e.get("camera") == review.get("camera")]
        if not cached:
            return "expired" if cap_reached else "no_detection_evidence"
        best = max(cached, key=lambda item: (float(item.get("top_score", 0) or 0), str(item.get("id", ""))))
        if not self._valid_event(best, now):
            return "expired"
        reason = self._rejection(best)
        if reason:
            return reason
        return "expired" if cap_reached else "unresolved"

    def _finalize(self, review_id: str, now: float, *, cap_reached: bool = False) -> None:
        review = self._pending.pop(review_id, None)
        if review is None:
            return
        reason = self.explain_not_sent(review, now, cap_reached=cap_reached)
        self._log_not_sent(review_id, review, reason, now)

    def _log_not_sent(self, review_id: str, review: dict[str, Any], reason: str, now: float) -> None:
        delivery_key = f"{review_id}/{review.get('severity')}"
        if delivery_key in self._delivered or delivery_key in self._inflight:
            return
        scores = [self._events[i].get("top_score") for i in self._detection_ids(review) if i in self._events]
        scores = [float(s) for s in scores if type(s) in (int, float) and math.isfinite(s)]
        log.info(
            "Review %s not sent: %s (camera=%s severity=%s best_score=%s)",
            str(review_id)[:64], reason, str(review.get("camera", ""))[:64],
            review.get("severity"), f"{max(scores):.3f}" if scores else "n/a",
        )

    def complete_delivery(self, envelope: dict[str, Any], *, terminal: bool, notified: bool = True, now: float | None = None) -> None:
        """Release an in-flight Review and persist only terminal outcomes.

        A successful send and an intentional local-policy suppression are
        terminal. A transport failure is not: a repeated MQTT Review may retry.
        """
        review_id = envelope.get("reviewId")
        severity = envelope.get("reviewSeverity")
        if not isinstance(review_id, str) or severity not in VALID_SEVERITIES:
            return
        delivery_key = f"{review_id}/{severity}"
        self._inflight.discard(delivery_key)
        if not terminal:
            if envelope.get("isEnrichment"):
                self._enriched.pop(delivery_key, None)
            return
        if notified:
            self._notified[delivery_key] = time.time() if now is None else now
            self._trim(self._notified, 4096)
        pending = self._pending.get(review_id)
        if pending and pending.get("severity") == severity:
            self._pending.pop(review_id, None)
        claimed_at = time.time() if now is None else now
        self._delivered[delivery_key] = claimed_at
        self._delivered.move_to_end(delivery_key)
        self._trim(self._delivered, 4096)
        self._persist_state()

    def _valid_event(self, event: dict[str, Any], now: float) -> bool:
        if not all(isinstance(event.get(key), str) and 0 < len(event[key]) <= 256 for key in ("id", "camera", "label")):
            return False
        score = event.get("top_score")
        detected_at = event.get("start_time")
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            return False
        if type(detected_at) not in (int, float) or not math.isfinite(detected_at) or detected_at <= 0:
            return False
        for key in ("zones", "current_zones"):
            zones = event.get(key)
            if zones is not None and (not isinstance(zones, list) or any(not isinstance(z, str) or not z for z in zones)):
                return False
        if "stationary" in event and not isinstance(event["stationary"], bool):
            return False
        age = now - float(detected_at)
        if not -self.future_skew_seconds <= age <= self.max_open_seconds:
            return False
        # Freshness follows the object's latest Frigate-reported activity
        # (frame_time / end_time), so a late "end" carrying the final score is
        # still evidence while an idle replayed object is not.
        return now - last_activity(event) <= self.max_age_seconds

    def _valid_review(self, review: dict[str, Any], now: float) -> bool:
        if not isinstance(review.get("id"), str) or not review["id"]:
            return False
        if review.get("severity") not in VALID_SEVERITIES:
            return False
        if not self._detection_ids(review):
            return False
        if not isinstance(review.get("camera"), str) or not review["camera"]:
            return False
        start = review.get("start_time")
        if type(start) not in (int, float) or not math.isfinite(start) or start <= 0:
            return False
        age = now - float(start)
        if age < -self.future_skew_seconds or age > self.max_open_seconds:
            return False
        end = review.get("end_time")
        if type(end) in (int, float) and math.isfinite(end) and end > 0:
            # A closed Review only stays actionable briefly after it ended.
            if now - float(end) > self.max_age_seconds:
                return False
        return True

    @staticmethod
    def _detection_ids(review: dict[str, Any]) -> list[str]:
        data = review.get("data")
        detections = data.get("detections") if isinstance(data, dict) else None
        if not isinstance(detections, list):
            return []
        return [item for item in detections if isinstance(item, str) and item][:256]

    def _expire(self, now: float) -> None:
        # Pending Reviews first, so their reason can still see cached events.
        for key, review in list(self._pending.items()):
            start = review.get("start_time")
            ended_at = review.get("_ended_at")
            if isinstance(start, (int, float)) and now - start > self.max_open_seconds:
                self._finalize(key, now, cap_reached=True)
            elif not self._valid_review(review, now):
                self._finalize(key, now, cap_reached=True)
            elif isinstance(ended_at, (int, float)) and now - ended_at > self.end_grace_seconds:
                self._finalize(key, now)
        for key, event in list(self._events.items()):
            detected_at = event.get("start_time")
            if not isinstance(detected_at, (int, float)) or now - detected_at > self.max_open_seconds:
                self._events.pop(key, None)
            elif event.get("_ended") and now - last_activity(event) > self.max_age_seconds:
                self._events.pop(key, None)
        for key, claimed_at in list(self._delivered.items()):
            if now - claimed_at > 86_400:
                self._delivered.pop(key, None)
                self._notified.pop(key, None)
                self._enriched.pop(key, None)

    @staticmethod
    def _trim(mapping: OrderedDict, limit: int) -> None:
        while len(mapping) > limit:
            mapping.popitem(last=False)

    def _load_state(self) -> None:
        if self.state_path is None:
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            delivered = raw.get("delivered", {})
            if isinstance(delivered, dict):
                for key, value in delivered.items():
                    if isinstance(key, str) and isinstance(value, (int, float)):
                        self._delivered[key] = float(value)
        except (OSError, ValueError, TypeError):
            return

    def _persist_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        payload = json.dumps({"delivered": self._delivered}, separators=(",", ":"))
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.state_path)


def last_activity(item: dict[str, Any]) -> float:
    """Latest Frigate-reported timestamp of an event or Review (start/frame/end)."""
    values = [item.get(key) for key in ("start_time", "frame_time", "end_time")]
    finite = [float(v) for v in values if type(v) in (int, float) and math.isfinite(v) and v > 0]
    return max(finite) if finite else 0.0

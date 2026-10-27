"""Identity and description corrections (frigate/tracked_object_update).

Frigate recognises a face and writes a GenAI description seconds after the
detection itself. If the Review has not gone out yet, the first notification
should already carry them; if it has, the user gets exactly one silent
correction that rewrites the banner rather than a second alert.
"""

from src.review_contract import ReviewCorrelator


NOW = 1_712_345_700.0


def event(event_id="event-1", camera="front_door"):
    return {
        "type": "new",
        "after": {
            "id": event_id,
            "camera": camera,
            "label": "person",
            "zones": ["mailbox"],
            "top_score": 0.94,
            "start_time": NOW - 5,
        },
    }


def review(detections=None, severity="alert", metadata=None):
    data = {"detections": detections or ["event-1"]}
    if metadata is not None:
        data["metadata"] = metadata
    return {
        "type": "new",
        "after": {
            "id": "review-1",
            "camera": "front_door",
            "severity": severity,
            "start_time": NOW - 6,
            "data": data,
        },
    }


def face_update(name="Facteur", event_id="event-1"):
    return {"type": "face", "id": event_id, "name": name, "score": 0.91,
            "camera": "front_door", "timestamp": NOW}


def description_update(text="Someone in uniform at the mailbox.", event_id="event-1"):
    return {"type": "description", "id": event_id, "description": text,
            "camera": "front_door"}


def delivered_correlator():
    correlator = ReviewCorrelator()
    correlator.ingest_event_message(event(), now=NOW)
    envelopes = correlator.ingest_review_message(review(), now=NOW)
    assert len(envelopes) == 1
    correlator.complete_delivery(envelopes[0], terminal=True, now=NOW)
    return correlator, envelopes[0]


def test_identity_known_before_delivery_rides_the_first_notification():
    correlator = ReviewCorrelator()
    correlator.ingest_event_message(event(), now=NOW)
    assert correlator.ingest_identity_update(face_update(), now=NOW) == []

    envelopes = correlator.ingest_review_message(review(), now=NOW)
    assert len(envelopes) == 1
    assert envelopes[0]["sub_label"] == "Facteur"
    assert "isEnrichment" not in envelopes[0]


def test_identity_after_delivery_produces_one_correction():
    correlator, original = delivered_correlator()

    corrections = correlator.ingest_identity_update(face_update(), now=NOW + 4)
    assert len(corrections) == 1
    correction = corrections[0]
    assert correction["isEnrichment"] is True
    assert correction["sub_label"] == "Facteur"
    # Same review identity, so APNs collapses it onto the original banner.
    assert correction["reviewId"] == original["reviewId"]
    assert correction["reviewSeverity"] == original["reviewSeverity"]


def test_face_then_description_do_not_buzz_the_phone_twice():
    correlator, _ = delivered_correlator()

    assert len(correlator.ingest_identity_update(face_update(), now=NOW + 3)) == 1
    assert correlator.ingest_identity_update(description_update(), now=NOW + 5) == []


def test_repeating_the_same_identity_says_nothing_new():
    correlator = ReviewCorrelator()
    correlator.ingest_event_message(event(), now=NOW)
    correlator.ingest_identity_update(face_update(), now=NOW)
    assert correlator.ingest_identity_update(face_update(), now=NOW + 1) == []


def test_description_update_carries_the_text():
    correlator = ReviewCorrelator()
    correlator.ingest_event_message(event(), now=NOW)
    correlator.ingest_identity_update(description_update(), now=NOW)

    envelopes = correlator.ingest_review_message(review(), now=NOW)
    assert envelopes[0]["description"] == "Someone in uniform at the mailbox."


def test_review_metadata_supplies_a_description_when_the_event_has_none():
    correlator = ReviewCorrelator()
    correlator.ingest_event_message(event(), now=NOW)
    envelopes = correlator.ingest_review_message(
        review(metadata={"title": "A delivery van stops outside."}), now=NOW
    )
    assert envelopes[0]["description"] == "A delivery van stops outside."


def test_event_description_wins_over_the_review_summary():
    correlator = ReviewCorrelator()
    correlator.ingest_event_message(event(), now=NOW)
    correlator.ingest_identity_update(description_update("Closer detail."), now=NOW)
    envelopes = correlator.ingest_review_message(
        review(metadata={"title": "Generic summary."}), now=NOW
    )
    assert envelopes[0]["description"] == "Closer detail."


def test_unknown_event_or_empty_update_is_ignored():
    correlator, _ = delivered_correlator()
    assert correlator.ingest_identity_update(face_update(event_id="other"), now=NOW) == []
    assert correlator.ingest_identity_update({"type": "face", "id": "event-1"}, now=NOW) == []
    assert correlator.ingest_identity_update({"type": "lpr", "id": "event-1"}, now=NOW) == []
    assert correlator.ingest_identity_update("not a dict", now=NOW) == []


def test_long_values_are_bounded():
    correlator = ReviewCorrelator()
    correlator.ingest_event_message(event(), now=NOW)
    correlator.ingest_identity_update(face_update(name="N" * 400), now=NOW)
    correlator.ingest_identity_update(description_update("D" * 900), now=NOW)

    envelopes = correlator.ingest_review_message(review(), now=NOW)
    assert len(envelopes[0]["sub_label"]) == 128
    assert len(envelopes[0]["description"]) == 500

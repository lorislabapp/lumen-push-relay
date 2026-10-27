import json

from src.review_contract import ReviewCorrelator


NOW = 1_712_345_700.0


def event(event_id="event-1", score=0.94, camera="front_door"):
    return {
        "type": "new",
        "after": {
            "id": event_id,
            "camera": camera,
            "label": "person",
            "zones": ["porch"],
            "top_score": score,
            "start_time": NOW - 5,
        },
    }


def review(detections=None, severity="alert", camera="front_door"):
    return {
        "type": "new",
        "after": {
            "id": "review-1",
            "camera": camera,
            "severity": severity,
            "start_time": NOW - 6,
            "data": {"detections": detections or ["event-1"]},
        },
    }


def test_correlates_in_both_arrival_orders_and_deduplicates():
    first = ReviewCorrelator()
    assert first.ingest_event_message(event(), now=NOW) == []
    envelopes = first.ingest_review_message(review(), now=NOW)
    assert len(envelopes) == 1
    assert envelopes[0]["sourceSchema"] == "frigate-review-v1"
    assert envelopes[0]["reviewId"] == "review-1"
    assert envelopes[0]["eventId"] == "event-1"
    assert first.ingest_review_message(review(), now=NOW) == []
    first.complete_delivery(envelopes[0], terminal=True, now=NOW)
    assert first.ingest_review_message(review(), now=NOW) == []

    second = ReviewCorrelator()
    assert second.ingest_review_message(review(), now=NOW) == []
    assert len(second.ingest_event_message(event(), now=NOW)) == 1


def test_selects_highest_score_detection_deterministically():
    correlator = ReviewCorrelator()
    correlator.ingest_event_message(event("event-low", 0.6), now=NOW)
    correlator.ingest_event_message(event("event-high", 0.97), now=NOW)
    envelope = correlator.ingest_review_message(review(["event-low", "event-high"]), now=NOW)[0]
    assert envelope["eventId"] == "event-high"
    assert envelope["score"] == 0.97


def test_carries_only_canonical_automation_evidence_fields():
    correlator = ReviewCorrelator()
    message = event()
    message["after"].update({
        "stationary": False,
        "has_clip": True,
        "data": {"recognized_license_plate": "AB-123-CD"},
    })
    correlator.ingest_event_message(message, now=NOW)
    envelope = correlator.ingest_review_message(review(), now=NOW)[0]
    assert envelope["stationary"] is False
    assert envelope["has_clip"] is True
    assert envelope["recognized_plate"] == "AB-123-CD"


def test_rejects_raw_stale_future_and_mismatched_inputs():
    correlator = ReviewCorrelator()
    assert correlator.ingest_event_message({"type": "new", "after": {}}, now=NOW) == []
    stale = event()
    stale["after"]["start_time"] = NOW - 121
    assert correlator.ingest_event_message(stale, now=NOW) == []
    future = event()
    future["after"]["start_time"] = NOW + 31
    assert correlator.ingest_event_message(future, now=NOW) == []
    correlator.ingest_event_message(event(), now=NOW)
    assert correlator.ingest_review_message(review(camera="garage"), now=NOW) == []


def test_restart_keeps_review_severity_claim(tmp_path):
    state = tmp_path / "review-state.json"
    first = ReviewCorrelator(state_path=state)
    first.ingest_event_message(event(), now=NOW)
    envelope = first.ingest_review_message(review(), now=NOW)[0]
    first.complete_delivery(envelope, terminal=True, now=NOW)
    assert json.loads(state.read_text())["delivered"]["review-1/alert"] == NOW

    restarted = ReviewCorrelator(state_path=state)
    restarted.ingest_event_message(event(), now=NOW)
    assert restarted.ingest_review_message(review(), now=NOW) == []
    escalation = review(severity="detection")
    assert len(restarted.ingest_review_message(escalation, now=NOW)) == 1


def test_transport_failure_releases_claim_for_retry():
    correlator = ReviewCorrelator()
    correlator.ingest_event_message(event(), now=NOW)
    envelope = correlator.ingest_review_message(review(), now=NOW)[0]
    correlator.complete_delivery(envelope, terminal=False, now=NOW)
    assert len(correlator.ingest_review_message(review(), now=NOW)) == 1

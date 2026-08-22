import unittest

from relay import (
    ReviewTracker,
    build_apns_payload,
    effective_mqtt_topic,
    review_to_event,
    should_notify,
)


def review_message(message_type="new", before_severity=None, severity="alert"):
    before = None if before_severity is None else {"severity": before_severity}
    return {
        "type": message_type,
        "before": before,
        "after": {
            "id": "review-1",
            "camera": "front_door",
            "start_time": 1787344282.0,
            "severity": severity,
            "data": {
                "detections": ["1787344282.000403-u8lbl4"],
                "objects": ["car", "person"],
                "zones": ["driveway"],
            },
        },
    }


class ReviewRelayTests(unittest.TestCase):
    def test_legacy_events_default_auto_migrates_to_reviews(self):
        topic, is_review = effective_mqtt_topic(
            {"frigate": {"topic": "frigate/events"}}
        )
        self.assertEqual(topic, "frigate/reviews")
        self.assertTrue(is_review)

    def test_raw_events_require_explicit_opt_in(self):
        topic, is_review = effective_mqtt_topic(
            {"frigate": {"topic": "frigate/events", "allow_raw_events": True}}
        )
        self.assertEqual(topic, "frigate/events")
        self.assertFalse(is_review)

    def test_new_alert_maps_to_linked_event(self):
        event, reason = review_to_event(review_message(), {"alert"})
        self.assertIsNone(reason)
        self.assertEqual(event["id"], "1787344282.000403-u8lbl4")
        self.assertEqual(event["review_id"], "review-1")
        self.assertEqual(event["source_schema"], "frigate-review-v1")

    def test_detection_is_suppressed_by_default(self):
        event, reason = review_to_event(review_message(severity="detection"), {"alert"})
        self.assertIsNone(event)
        self.assertIn("not enabled", reason)

    def test_update_only_notifies_on_severity_transition(self):
        event, _ = review_to_event(
            review_message(message_type="update", before_severity="detection"), {"alert"}
        )
        self.assertIsNotNone(event)
        event, reason = review_to_event(
            review_message(message_type="update", before_severity="alert"), {"alert"}
        )
        self.assertIsNone(event)
        self.assertIn("already", reason)

    def test_review_without_detection_is_fail_closed(self):
        message = review_message()
        message["after"]["data"]["detections"] = []
        event, reason = review_to_event(message, {"alert"})
        self.assertIsNone(event)
        self.assertIn("no linked detection", reason)

    def test_review_without_score_uses_matching_object_label(self):
        event, _ = review_to_event(review_message(), {"alert"})
        reason = should_notify(
            event,
            {"filters": {"labels": ["person"], "min_score": 0.9, "max_event_age_seconds": 0}},
        )
        self.assertIsNone(reason)
        self.assertEqual(event["label"], "person")

    def test_direct_payload_carries_review_contract(self):
        event, _ = review_to_event(review_message(), {"alert"})
        payload = build_apns_payload(event)
        self.assertEqual(payload["reviewId"], "review-1")
        self.assertEqual(payload["reviewSeverity"], "alert")
        self.assertEqual(payload["sourceSchema"], "frigate-review-v1")
        self.assertEqual(payload["eventId"], "1787344282.000403-u8lbl4")

    def test_tracker_deduplicates_and_evicts(self):
        tracker = ReviewTracker(max_entries=2)
        self.assertTrue(tracker.claim("r1", "alert"))
        self.assertFalse(tracker.claim("r1", "alert"))
        self.assertTrue(tracker.claim("r2", "alert"))
        self.assertTrue(tracker.claim("r3", "alert"))
        self.assertTrue(tracker.claim("r1", "alert"))


if __name__ == "__main__":
    unittest.main()

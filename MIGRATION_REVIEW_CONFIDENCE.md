# Review confidence migration

This release fixes a gap in the September 16 Review-only relay: Reviews carry detection IDs but do not establish each object's confidence. The relay now subscribes to Reviews AND objects, correlates their IDs/camera/time, and sends a canonical `frigate-review-v1` envelope only after the notification filters pass. No score or Review authority is invented.

The previous 1.2.0 image sends raw objects. Upgrade a controlled relay before enabling strict rejection in the push Worker. This image works with both the existing Worker and its strict successor; changing the Worker first can stop old relay notifications. The broader development branch's Android billing/FCM and home-automation changes are not included in this release.

## Updating an existing installation

1. Preserve your current image digest, compose file/config, MQTT credentials and push destinations. Do not share the push URL in public logs.
2. Select `lorislabapp/lumen-push-relay:review-confidence-20260920`, preferably pinned to the digest recorded by the successful image build. Do not assume changing a tag updates an existing container.
3. Allow the MQTT credentials to subscribe to the matching `/reviews`, `/events`, and optionally `/tracked_object_update` topics. Custom topic prefixes are preserved. `ALLOW_RAW_EVENTS` cannot authorize raw-only pushes in this release.
4. Persist the delivery state, for example a named volume at `/state` with `LUMEN_REVIEW_STATE_PATH=/state/review-delivery-state.json`. The default is `/config/review-delivery-state.json`. Do not delete state to force a notification. An unwritable state path prevents reliable dedupe after recreation; verify the file is created after the first successful controlled notification.
5. For person/car >=75% set `FILTER_LABELS=person,car`, `FILTER_MIN_SCORE=0.75`, `REVIEW_SEVERITIES=alert`. With a mounted config file, use `filters: {labels: [person, car], min_score: 0.75, review_severities: [alert], cooldown_seconds: 120}` instead. The file takes precedence. Existing per-camera restrictions continue to apply.
6. Test one qualifying Review, a lower-score object, a stationary object and a repeated Review. Only the qualifying Review should alert. Check the receiving device; a relay acceptance log is not proof that APNs displayed it. The 120-second per-camera/label cooldown may intentionally suppress a different event in that interval.

Frigate may continue detecting/recording other classes. Smart Notifications text generation is not the detector-confidence gate. Use only one overlapping local/remote notification rule path when duplicate interruption is unwanted. A missing score is suppressed; a pending Review can become eligible when later MQTT evidence reaches the threshold.

## Recovery and limitations

If delivery fails, its claim/cooldown is released for the next matching MQTT update. Successful Review claims persist across restarts. A crash between external acceptance and local state persistence can still duplicate an alert; exactly-once APNs delivery is not promised. Enrichment after restart is not reconstructed; this avoids inventing an initial notification receipt. No autonomous timer retry is added.

If the new relay fails, stop the controlled rollout and retain logs/state. Roll back to the previous image only while its payload is still accepted by the Worker. Once strict Worker rejection is active, a raw-event relay rollback will stop pushes; use a compatible pair. Never reopen global raw acceptance silently to conceal a failed rollout.

The image build runs Python 3.12 unit/integration tests and a Linux-container import smoke check before publishing amd64/arm64 images. These do not substitute for your Frigate/MQTT/APNs/device test. This versioned image does not overwrite `latest`.

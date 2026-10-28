# Changelog

## 1.2.2 — 2026-10-28

### Fixed
- Reviews whose confidence crosses the threshold late are now sent. The relay
  processes Frigate `end` messages for events and Reviews (the final
  `top_score` often arrives only there) and keeps an open Review eligible for
  up to 15 minutes instead of 120 s. Detection freshness is measured from
  Frigate's latest `frame_time`/`end_time`. A Review is never sent twice at
  the same severity.
- README documented `FILTER_REVIEW_SEVERITIES`, but the relay reads
  `REVIEW_SEVERITIES`. README fixed; `FILTER_REVIEW_SEVERITIES` is accepted as
  a deprecated alias with a warning (`REVIEW_SEVERITIES` wins).

### Added
- INFO log explaining why a pending Review was finally not sent
  (`below_min_score`, `label_filtered`, `stationary`, `severity`, `expired`, …).
- Understands the lumen-push Worker JSON response (`delivered`, `suppressed`,
  `outcomes`). Rate-limited with nothing delivered → one retry after 6 s.
  Duplicate/filtered/cooldown → recorded as handled, not retried. Plain `OK`
  responses behave as before.
- Optional camera display names: set `FRIGATE_URL` (plus `FRIGATE_TOKEN` or
  `FRIGATE_USER`/`FRIGATE_PASSWORD` if needed). The relay reads `/api/config`
  at start and every 10 minutes and adds `camera_display_name` (≤ 64 chars).
  Failures never block delivery.
- Version 1.2.2 logged at start and set as the image label.

## 1.2.1 — 2026-10-27
- Review alert correlation and detection evidence enforced before push.

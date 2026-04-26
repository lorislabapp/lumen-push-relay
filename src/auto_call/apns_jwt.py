"""APNs ES256 JWT signer.

Extracted from DirectAPNsSender so VoIPDispatcher can sign even when the relay
runs in worker mode (where DirectAPNsSender isn't instantiated). The signing
logic mirrors DirectAPNsSender._get_token in relay.py — same algorithm (ES256),
same headers (kid + alg), same payload (iss + iat), same ~50-minute cache.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import jwt  # PyJWT


class APNsJWTSigner:
    """Caches and refreshes an APNs ES256 JWT.

    APNs accepts tokens up to 1 hour old; we refresh just under that to keep a
    safety margin. The token() method is sync and fast — call it on every push
    (or wrap it in a `lambda: signer.token()` to pass as a jwt_provider).
    """

    def __init__(
        self,
        team_id: str,
        key_id: str,
        private_key_pem: str,
        cache_seconds: int = 3000,  # under APNs' 1h ceiling, with margin
    ) -> None:
        self._team_id = team_id
        self._key_id = key_id
        self._private_key = private_key_pem
        self._cache_seconds = cache_seconds
        self._cached_token: Optional[str] = None
        self._cached_at: float = 0

    def token(self) -> str:
        now = time.time()
        if self._cached_token and (now - self._cached_at) < self._cache_seconds:
            return self._cached_token
        payload = {"iss": self._team_id, "iat": int(now)}
        headers = {"kid": self._key_id, "alg": "ES256"}
        token = jwt.encode(payload, self._private_key, algorithm="ES256", headers=headers)
        # PyJWT >= 2.x returns str; tolerate the old bytes form just in case.
        self._cached_token = token if isinstance(token, str) else token.decode("utf-8")
        self._cached_at = now
        return self._cached_token

    @classmethod
    def from_env(cls) -> "APNsJWTSigner":
        """Load credentials from env, matching relay.py's existing names.

        Required:
          APNS_TEAM_ID, APNS_KEY_ID, APNS_KEY_FILE
        """
        team_id = os.environ["APNS_TEAM_ID"]
        key_id = os.environ["APNS_KEY_ID"]
        key_path = os.environ["APNS_KEY_FILE"]
        with open(key_path, "r") as f:
            private_key = f.read()
        return cls(team_id=team_id, key_id=key_id, private_key_pem=private_key)

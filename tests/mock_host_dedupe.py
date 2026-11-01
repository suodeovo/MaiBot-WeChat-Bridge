"""Mock of MaiBot Host MessageDeduplicator for offline testing."""
import time


class MessageDeduplicator:
    """Simulates the Host platform_io dedupe behavior for offline tests."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl_seconds = ttl_seconds
        self._seen: dict = {}
        self._clock = time

    def mark_seen(self, key: str) -> bool:
        """Returns True if this is a new (unseen) key, False if duplicate."""
        now = self._clock.monotonic()
        # Clean expired entries
        expired = [k for k, ts in self._seen.items() if now - ts > self.ttl_seconds]
        for k in expired:
            del self._seen[k]
        if key in self._seen:
            return False
        self._seen[key] = now
        return True

"""Conservative failure labels and bounded retry delays.

A timeout alone never establishes which side is responsible.
"""
from dataclasses import dataclass
import errno
import random
import socket


@dataclass(frozen=True)
class Diagnosis:
    category: str
    detail: str
    # A reported Discord incident or an unavailable network means preserve the
    # existing voice session and let the SDK recover it; do not force a reset.
    preserve_session: bool = False


class VoiceFailure(Exception):
    def __init__(self, category: str, detail: str):
        super().__init__(detail)
        self.category = category


def classify_failure(error: BaseException) -> Diagnosis:
    if isinstance(error, VoiceFailure):
        return Diagnosis(error.category, str(error), error.category in {"discord", "network"})
    if isinstance(error, socket.gaierror):
        return Diagnosis("network", "DNS lookup failed; hosting/DNS/network path is suspect.", True)
    if isinstance(error, OSError) and error.errno in {
        errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
    }:
        return Diagnosis("network", "The host reports an unreachable network.", True)
    if isinstance(error, TimeoutError):
        return Diagnosis("unknown", "Connection timed out; either endpoint or the network may be responsible.")
    status = getattr(error, "status", None)
    if status in {401, 403, 404}:
        return Diagnosis("configuration", f"Discord returned HTTP {status}; check token, IDs, and permissions.")
    if status == 429:
        return Diagnosis("rate_limit", "Discord rate limit; allow the SDK to honor Retry-After.")
    if isinstance(status, int) and status >= 500:
        return Diagnosis("discord", f"Discord returned HTTP {status}; preserve existing sessions.", True)
    code = getattr(error, "code", None)
    if code in {4014, 4017}:
        return Diagnosis("configuration", f"Voice close code {code}; check access and current voice dependencies.")
    return Diagnosis("unknown", f"{type(error).__name__}; consult adjacent SDK logs.")


def next_retry_delay(attempt: int, category: str = "unknown", *, jitter: bool = True) -> float:
    base = min(60.0, float(2 ** min(max(attempt - 1, 0), 6)))
    if category in {"configuration", "rate_limit"}:
        base = max(base, 30.0)
    elif category in {"discord", "network"}:
        base = max(base, 10.0)
    return min(60.0, base * random.uniform(1.0, 1.2)) if jitter else base

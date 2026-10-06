"""Whose ``X-Forwarded-For`` the listener believes, and noticing when it
believes nobody's but plainly sits behind a proxy.

The auth limiter keys failures on the client address. Behind a reverse
proxy whose headers are not believed, every client has the proxy's
address, and ten failed sign-ins from anyone lock everyone out. A request
from a loopback or private peer that carries ``X-Forwarded-For`` while no
proxy is trusted is that deployment: the daemon says so once per process
in its log and leaves a note in its store, which ``lantern doctor`` reads.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable

from lantern.config import ApiConfig
from lantern.log import get_logger

log = get_logger(__name__)

#: The daemon-store key the note is left under; doctor reads it.
UNTRUSTED_FORWARDING_KEY = "api_untrusted_forwarding"


def _local_peer(peer: str) -> bool:
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


class ForwardingWatch:
    """Notes, once, a proxied request whose forwarded address was ignored."""

    def __init__(self, api: ApiConfig, record: Callable[[str], None]) -> None:
        self._believes_someone = bool(api.forwarding_proxies)
        self._record = record
        self._seen = False

    def wants(self, peer: str | None, forwarded: bool) -> bool:
        """Whether this request is the first sign of an ignored proxy."""
        return (
            not self._seen
            and forwarded
            and not self._believes_someone
            and peer is not None
            and _local_peer(peer)
        )

    def note(self, peer: str, now: float) -> None:
        if self._seen:
            return
        self._seen = True
        log.warning(
            "api.forwarded_for_ignored",
            peer=peer,
            hint=f'every client looks like {peer}; set [api] trusted_proxies = ["{peer}"]',
        )
        try:
            self._record(json.dumps({"peer": peer, "seen_at": now}))
        except Exception:  # the note is a diagnostic; a request never fails on it
            log.warning("api.forwarding_note_failed", exc_info=True)


def untrusted_forwarding_detail(note: str) -> str | None:
    """The doctor row's words for a stored note, or ``None`` when unreadable."""
    try:
        data = json.loads(note)
    except ValueError:
        return None
    peer = data.get("peer") if isinstance(data, dict) else None
    if not isinstance(peer, str) or not peer:
        return None
    return (
        f"requests from {peer} carry X-Forwarded-For but [api] trusted_proxies "
        f"believes no proxy: every client looks like {peer} to the sign-in limiter, "
        "so one client's failed sign-ins lock everyone out — set "
        f'`[api] trusted_proxies = ["{peer}"]`'
    )

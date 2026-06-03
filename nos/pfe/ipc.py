"""Python-side IPC client for the C PFE process.

Protocol: newline-delimited JSON over a Unix SOCK_STREAM socket.
One request → one response line; C server is at /run/nos/pfe.sock.
"""
import json
import socket
import threading
import time
from typing import Optional

from nos.utils.logger import get_logger

_SOCK_PATH = "/run/nos/pfe.sock"
_MAX_RETRIES = 3
_RETRY_DELAY = 0.5   # seconds between reconnect attempts
_RECV_SIZE = 65536

log = get_logger(__name__)


class PFEError(Exception):
    """Raised on unrecoverable PFE IPC errors."""


class PFEClient:
    """Unix-socket client that talks to the C PFE process.

    Thread-safe: all socket operations are serialised by an internal lock.
    """

    def __init__(self, sock_path: str = _SOCK_PATH) -> None:
        self._sock_path = sock_path
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._rbuf = b""

    # ── connection lifecycle ────────────────────────────────────────────────

    def connect(self) -> None:
        """Open the connection to the PFE.  No-op if already connected."""
        with self._lock:
            self._connect_locked()

    def disconnect(self) -> None:
        """Close the socket.  No-op if already disconnected."""
        with self._lock:
            self._close_locked()

    def is_connected(self) -> bool:
        with self._lock:
            return self._sock is not None

    # ── messaging ───────────────────────────────────────────────────────────

    def send_message(self, msg: dict) -> dict:
        """Send *msg* as JSON and return the parsed JSON reply.

        Reconnects transparently on transient connection failures, up to
        _MAX_RETRIES times.  Raises PFEError if all attempts fail.
        """
        last_exc: Optional[Exception] = None

        for attempt in range(_MAX_RETRIES):
            try:
                return self._send_once(msg)
            except (OSError, ConnectionError) as exc:
                last_exc = exc
                log.warning(
                    "PFE send failed (attempt %d/%d): %s — reconnecting",
                    attempt + 1, _MAX_RETRIES, exc,
                )
                with self._lock:
                    self._close_locked()
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_RETRY_DELAY)
                        try:
                            self._connect_locked()
                        except PFEError as connect_exc:
                            log.warning("reconnect attempt %d failed: %s", attempt + 1, connect_exc)

        raise PFEError(
            f"PFE unreachable after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    # ── internals ───────────────────────────────────────────────────────────

    def _connect_locked(self) -> None:
        """Open socket.  Caller must hold _lock."""
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self._sock_path)
        except OSError as exc:
            sock.close()
            raise PFEError(
                f"cannot connect to PFE at {self._sock_path}: {exc}"
            ) from exc
        self._sock = sock
        self._rbuf = b""
        log.info("connected to PFE at %s", self._sock_path)

    def _close_locked(self) -> None:
        """Close socket and reset buffer.  Caller must hold _lock."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._rbuf = b""
            log.debug("disconnected from PFE")

    def _send_once(self, msg: dict) -> dict:
        """Single send/receive cycle under the lock.

        Raises OSError / ConnectionError on socket problems.
        Raises PFEError on JSON decode failure of the reply.
        """
        with self._lock:
            if self._sock is None:
                self._connect_locked()

            payload = json.dumps(msg, separators=(",", ":")).encode() + b"\n"
            self._sock.sendall(payload)
            raw = self._readline_locked()

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PFEError(f"PFE returned invalid JSON: {raw!r}") from exc

    def port_vlan_set(self, ifindex: int, vlan_id: int, mode: int) -> None:
        """Insert or update an entry in the XDP port_vlan_map.

        mode: 0 = access (XDP pushes tag), 1 = trunk (pass through).
        Raises PFEError on failure.
        """
        reply = self.send_message({
            "type":    "port_vlan_set",
            "ifindex": ifindex,
            "vlan_id": vlan_id,
            "mode":    mode,
        })
        if reply.get("status") != "ok":
            raise PFEError(
                f"port_vlan_set ifindex={ifindex}: {reply.get('message')}"
            )

    def _readline_locked(self) -> bytes:
        """Read bytes up to and including the next '\\n'.  Caller must hold _lock."""
        while b"\n" not in self._rbuf:
            chunk = self._sock.recv(_RECV_SIZE)
            if not chunk:
                raise ConnectionError("PFE closed the connection unexpectedly")
            self._rbuf += chunk
        line, self._rbuf = self._rbuf.split(b"\n", 1)
        return line

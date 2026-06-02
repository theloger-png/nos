"""Unit tests for nos.pfe.ipc.PFEClient."""
import json
import socket
import threading
from unittest.mock import MagicMock, call, patch

import pytest

from nos.pfe.ipc import PFEClient, PFEError

_SOCK = "/run/nos/pfe.sock"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_sock(reply: dict | None = None, recv_data: bytes | None = None) -> MagicMock:
    """Return a mock socket whose recv() delivers *reply* as newline-JSON."""
    sock = MagicMock(spec=socket.socket)
    if reply is not None:
        recv_data = json.dumps(reply).encode() + b"\n"
    sock.recv.return_value = recv_data or b""
    return sock


def _patched_client(sock_mock: MagicMock, sock_path: str = _SOCK) -> PFEClient:
    """Return a PFEClient with socket.socket patched to return *sock_mock*."""
    client = PFEClient(sock_path=sock_path)
    with patch("nos.pfe.ipc.socket.socket", return_value=sock_mock):
        client.connect()
    return client


# ---------------------------------------------------------------------------
# connect / disconnect / is_connected
# ---------------------------------------------------------------------------

class TestConnectionLifecycle:
    def test_connect_opens_socket(self):
        sock = _make_sock()
        client = _patched_client(sock)
        assert client.is_connected()
        sock.connect.assert_called_once_with(_SOCK)

    def test_connect_is_idempotent(self):
        sock = _make_sock()
        client = _patched_client(sock)
        with patch("nos.pfe.ipc.socket.socket", return_value=sock):
            client.connect()  # second call — should not create another socket
        assert sock.connect.call_count == 1

    def test_disconnect_closes_socket(self):
        sock = _make_sock()
        client = _patched_client(sock)
        client.disconnect()
        sock.close.assert_called_once()
        assert not client.is_connected()

    def test_disconnect_is_idempotent(self):
        sock = _make_sock()
        client = _patched_client(sock)
        client.disconnect()
        client.disconnect()   # should not raise
        assert not client.is_connected()

    def test_connect_raises_pfe_error_on_os_error(self):
        sock = MagicMock(spec=socket.socket)
        sock.connect.side_effect = OSError("no such file")
        with patch("nos.pfe.ipc.socket.socket", return_value=sock):
            with pytest.raises(PFEError, match="cannot connect"):
                PFEClient().connect()

    def test_not_connected_initially(self):
        assert not PFEClient().is_connected()


# ---------------------------------------------------------------------------
# send_message — happy path
# ---------------------------------------------------------------------------

class TestSendMessage:
    def test_sends_newline_delimited_json(self):
        sock = _make_sock(reply={"status": "ok"})
        client = _patched_client(sock)
        client.send_message({"type": "ping"})
        sent = sock.sendall.call_args[0][0]
        assert sent.endswith(b"\n")
        assert json.loads(sent) == {"type": "ping"}

    def test_returns_parsed_reply(self):
        reply = {"status": "ok", "data": {"rx_packets": 42}}
        sock = _make_sock(reply=reply)
        client = _patched_client(sock)
        result = client.send_message({"type": "stats_get", "ifindex": 2})
        assert result == reply

    def test_reply_split_across_chunks(self):
        """recv() delivers the reply in two chunks."""
        payload = json.dumps({"status": "ok"}).encode() + b"\n"
        sock = MagicMock(spec=socket.socket)
        sock.recv.side_effect = [payload[:5], payload[5:]]
        client = _patched_client(sock)
        result = client.send_message({"type": "ping"})
        assert result == {"status": "ok"}

    def test_connects_lazily_if_not_connected(self):
        sock = _make_sock(reply={"status": "ok"})
        client = PFEClient()
        with patch("nos.pfe.ipc.socket.socket", return_value=sock):
            result = client.send_message({"type": "ping"})
        assert result == {"status": "ok"}

    def test_error_reply_is_returned_not_raised(self):
        """PFE logical errors come back as dicts; PFEClient does not raise."""
        reply = {"status": "error", "message": "prefix not found"}
        sock = _make_sock(reply=reply)
        client = _patched_client(sock)
        result = client.send_message({"type": "fib_del", "prefix": "10.0.0.0/8"})
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# send_message — reconnect / retry logic
# ---------------------------------------------------------------------------

class TestAutoReconnect:
    def test_reconnects_on_connection_error_and_succeeds(self):
        good_reply = json.dumps({"status": "ok"}).encode() + b"\n"

        broken_sock = MagicMock(spec=socket.socket)
        broken_sock.recv.return_value = b""   # signals EOF → ConnectionError

        good_sock = MagicMock(spec=socket.socket)
        good_sock.recv.return_value = good_reply

        client = PFEClient()
        with patch("nos.pfe.ipc.socket.socket", side_effect=[broken_sock, good_sock]):
            with patch("nos.pfe.ipc.time.sleep"):
                result = client.send_message({"type": "ping"})

        assert result == {"status": "ok"}

    def test_raises_pfe_error_after_max_retries(self):
        sock = MagicMock(spec=socket.socket)
        sock.recv.return_value = b""  # always EOF

        client = PFEClient()
        with patch("nos.pfe.ipc.socket.socket", return_value=sock):
            with patch("nos.pfe.ipc.time.sleep"):
                with pytest.raises(PFEError, match="unreachable"):
                    client.send_message({"type": "ping"})

    def test_sleeps_between_retries(self):
        sock = MagicMock(spec=socket.socket)
        sock.recv.return_value = b""  # always EOF

        client = PFEClient()
        with patch("nos.pfe.ipc.socket.socket", return_value=sock):
            with patch("nos.pfe.ipc.time.sleep") as mock_sleep:
                with pytest.raises(PFEError):
                    client.send_message({"type": "ping"})

        # sleep is called between attempts (not after the last failure)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(0.5)

    def test_invalid_json_reply_raises_pfe_error(self):
        sock = MagicMock(spec=socket.socket)
        sock.recv.return_value = b"not-json\n"

        client = _patched_client(sock)
        with pytest.raises(PFEError, match="invalid JSON"):
            client.send_message({"type": "ping"})


# ---------------------------------------------------------------------------
# thread safety — smoke test
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_sends_do_not_interleave(self):
        """Multiple threads calling send_message must not corrupt the buffer."""
        replies = [
            json.dumps({"status": "ok", "n": i}).encode() + b"\n"
            for i in range(10)
        ]
        recv_iter = iter(replies)
        recv_lock = threading.Lock()

        sock = MagicMock(spec=socket.socket)

        def _recv(_size):
            with recv_lock:
                try:
                    return next(recv_iter)
                except StopIteration:
                    return b""

        sock.recv.side_effect = _recv

        client = _patched_client(sock)
        results = []
        errors = []

        def _worker():
            try:
                results.append(client.send_message({"type": "ping"}))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 10
        for r in results:
            assert r["status"] == "ok"

"""Exercise the real proxy against an isolated Varlink resolver socket."""

import json
import socket
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from spaces import resolver


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="spaces-resolver-test-")
        self.directory = Path(self.temporary.name)
        self.calls = []
        self.reply = {"parameters": {"configuration": [{"ifname": "eth0"}]}}
        owner = self

        class Host(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(2)
                frame = next(resolver._frames(self.request))
                owner.calls.append(json.loads(frame))
                self.request.sendall(json.dumps(owner.reply).encode() + b"\0")

        self.host = socketserver.ThreadingUnixStreamServer(
            str(self.directory / "host.sock"), Host
        )
        self.host_thread = threading.Thread(
            target=self.host.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self.host_thread.start()
        self.proxy = resolver.ResolverService(self.directory, Path(self.host.server_address))
        self.proxy.start()
        self.connections = []

    def tearDown(self):
        self.proxy.stop()
        for connection in self.connections:
            connection.close()
        self.host.shutdown()
        self.host.server_close()
        self.host_thread.join()
        self.temporary.cleanup()

    def connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2)
        connection.connect(str(self.proxy.socket))
        self.connections.append(connection)
        return connection

    def request(self, message):
        connection = self.connect()
        connection.sendall(json.dumps(message).encode() + b"\0")
        return json.loads(next(resolver._frames(connection)))

    def test_dns_configuration_and_lookup_replies(self):
        for method, parameters in (
            ("DumpDNSConfiguration", {}),
            ("ResolveHostname", {"name": "example.org"}),
            ("ResolveAddress", {"family": 2, "address": [127, 0, 0, 1]}),
            ("ResolveRecord", {"name": "example.org", "type": 1}),
            ("ResolveService", {"domain": "example.org"}),
        ):
            request = {"method": "io.systemd.Resolve." + method, "parameters": parameters}
            self.assertEqual(self.request(request), self.reply)
            self.assertEqual(self.calls[-1], request)
        self.assertEqual(self.proxy.socket.stat().st_mode & 0o777, 0o666)

    def test_configuration_and_other_interfaces_never_reach_host(self):
        for method in (
            "io.systemd.Resolve.SetLinkDNS",
            "io.systemd.LogControl.SetLogLevel",
            "io.systemd.Resolve.Monitor.ResetStatistics",
            "io.systemd.Resolve.BrowseServices",
            "io.systemd.Resolve.DumpDNSConfiguration.extra",
        ):
            reply = self.request({"method": method})
            self.assertEqual(reply["error"], "org.varlink.service.MethodNotFound")
        self.assertEqual(self.calls, [])

    def test_invalid_and_streaming_requests_never_reach_host(self):
        for request in (
            [],
            {"method": []},
            {"method": "io.systemd.Resolve.DumpDNSConfiguration", "parameters": []},
            *(
                {"method": "io.systemd.Resolve.DumpDNSConfiguration", flag: value}
                for flag, value in (
                    ("more", True),
                    ("oneway", True),
                    ("upgrade", True),
                    ("more", 0),
                    ("unknown", False),
                )
            ),
        ):
            self.assertEqual(
                self.request(request)["error"], "org.varlink.service.InvalidParameter"
            )
        connection = self.connect()
        connection.sendall(
            b'{"method":"io.systemd.Resolve.DumpDNSConfiguration",'
            b'"method":"io.systemd.LogControl.SetLogLevel"}\0'
        )
        self.assertEqual(
            json.loads(next(resolver._frames(connection)))["error"],
            "org.varlink.service.InvalidParameter",
        )
        self.assertEqual(self.calls, [])

    def test_fragmented_and_pipelined_requests(self):
        connection = self.connect()
        frame = b'{"method":"io.systemd.Resolve.DumpDNSConfiguration"}\0'
        connection.sendall(frame[:10])
        connection.sendall(frame[10:] + frame)
        replies = resolver._frames(connection)
        self.assertEqual(json.loads(next(replies)), self.reply)
        self.assertEqual(json.loads(next(replies)), self.reply)
        self.assertEqual(len(self.calls), 2)

    def test_host_errors_and_missing_host(self):
        self.reply = {"error": "io.systemd.Resolve.NoNameServers", "parameters": {}}
        self.assertEqual(
            self.request(
                {
                    "method": "io.systemd.Resolve.ResolveHostname",
                    "parameters": {"name": "example.org"},
                }
            ),
            self.reply,
        )
        self.proxy.server.host_socket = self.directory / "missing.sock"
        self.assertEqual(
            self.request({"method": "io.systemd.Resolve.DumpDNSConfiguration"})["error"],
            "io.systemd.System.Errno",
        )
        self.proxy.server.host_socket = Path(self.host.server_address)
        self.assertEqual(
            self.request({"method": "io.systemd.Resolve.DumpDNSConfiguration"}), self.reply
        )

    def test_oversized_frames_are_closed_without_forwarding(self):
        with mock.patch.object(resolver, "MAX_MESSAGE", 128):
            connection = self.connect()
            connection.sendall(b"x" * 129 + b"\0")
            self.assertEqual(connection.recv(1), b"")
        self.assertEqual(self.calls, [])

    def test_stop_closes_idle_clients_and_removes_socket(self):
        connection = self.connect()
        # Receiving a reply ensures the client was accepted before stopping.
        connection.sendall(b'{"method":"io.systemd.LogControl.SetLogLevel"}\0')
        next(resolver._frames(connection))
        self.proxy.stop()
        self.assertEqual(connection.recv(1), b"")
        self.assertFalse(self.proxy.socket.exists())

    def test_excess_clients_are_closed_while_existing_client_stays_usable(self):
        self.proxy.stop()
        with mock.patch.object(resolver, "MAX_CLIENTS", 1):
            self.proxy.start()
        connection = self.connect()
        frame = b'{"method":"io.systemd.Resolve.DumpDNSConfiguration"}\0'
        connection.sendall(frame)
        self.assertEqual(json.loads(next(resolver._frames(connection))), self.reply)
        excess = self.connect()
        self.assertEqual(excess.recv(1), b"")
        connection.sendall(frame)
        self.assertEqual(json.loads(next(resolver._frames(connection))), self.reply)

    def test_host_reply_size_and_streaming_are_bounded(self):
        with mock.patch.object(resolver, "MAX_MESSAGE", 128):
            self.reply = {"parameters": {"value": "x" * 129}}
            result = self.request({"method": "io.systemd.Resolve.DumpDNSConfiguration"})
            self.assertEqual(result["error"], "io.systemd.System.Errno")
        self.reply = {"parameters": {}, "continues": True}
        result = self.request({"method": "io.systemd.Resolve.DumpDNSConfiguration"})
        self.assertEqual(result["error"], "io.systemd.System.Errno")


if __name__ == "__main__":
    unittest.main()

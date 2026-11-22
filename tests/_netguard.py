"""pytest plugin for tests/test_no_live_github.py: record and block every outbound connection.

Loaded with `-p tests._netguard` in a child pytest. Each attempted connection is
appended to the file named by $NETGUARD_LOG and refused, so the parent test can
assert that nothing tried to reach the network.
"""

import os
import socket

_LOG = os.environ.get("NETGUARD_LOG")
_real_connect = socket.socket.connect


def _record(address) -> None:
    if _LOG:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(f"{address!r}\n")


def _guarded_connect(self, address):
    if self.family == getattr(socket, "AF_UNIX", None):
        return _real_connect(self, address)  # local IPC is not the network
    _record(address)
    raise ConnectionRefusedError(f"netguard: blocked connection to {address!r}")


def _guarded_getaddrinfo(host, *args, **kwargs):
    _record(("getaddrinfo", host))
    raise socket.gaierror(f"netguard: blocked DNS lookup of {host!r}")


socket.socket.connect = _guarded_connect
socket.getaddrinfo = _guarded_getaddrinfo

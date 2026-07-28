"""Bounded wire protocol shared by authentication clients and the broker."""

from __future__ import annotations

import array
import json
import os
import socket
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


MAGIC = b"SPAU"
VERSION = 2
MAX_PAYLOAD = 16 * 1024
MAX_DESCRIPTORS = 1
TOKEN_HEX_SIZE = 64
MAX_IDENTIFIER_SIZE = 128
HEADER = struct.Struct("!4sBBI")


class MessageType(IntEnum):
    REGISTER = 1
    AUTHENTICATE = 2
    TOKEN = 3
    ERROR = 5
    RESULT = 6
    SESSION_OPEN = 10
    SESSION_CLOSE = 11


REGISTER = MessageType.REGISTER
AUTHENTICATE = MessageType.AUTHENTICATE
TOKEN = MessageType.TOKEN
ERROR = MessageType.ERROR
RESULT = MessageType.RESULT
SESSION_OPEN = MessageType.SESSION_OPEN
SESSION_CLOSE = MessageType.SESSION_CLOSE


def valid_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == TOKEN_HEX_SIZE
        and all(character in "0123456789abcdef" for character in value)
    )


def valid_integer(value: object, *, minimum: int = 0) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= minimum
    )


def valid_host_session_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= MAX_IDENTIFIER_SIZE
        and "\0" not in value
        and "/" not in value
    )


def valid_guest_session_id(value: object) -> bool:
    return (
        valid_host_session_id(value)
        and all(
            character.isascii()
            and (character.isalnum() or character in {"_", "-"})
            for character in value
        )
    )


def decode_object(payload: bytes, fields: set[str]) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid authentication protocol object")
    return value


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame_with_descriptors(
    connection: socket.socket,
) -> tuple[int, bytes, tuple[int, ...]]:
    descriptors: list[int] = []
    try:
        header_start, ancillary, flags, _address = connection.recvmsg(
            HEADER.size,
            socket.CMSG_SPACE(MAX_DESCRIPTORS * array.array("i").itemsize),
            socket.MSG_CMSG_CLOEXEC,
        )
        if not header_start:
            raise EOFError
        if flags & socket.MSG_CTRUNC:
            raise ValueError("authentication descriptor data was truncated")
        for level, kind, data in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                continue
            values = array.array("i")
            usable = len(data) - (len(data) % values.itemsize)
            values.frombytes(data[:usable])
            descriptors.extend(values)
        if len(descriptors) > MAX_DESCRIPTORS:
            raise ValueError("too many authentication descriptors")

        header = header_start + _recv_exact(
            connection,
            HEADER.size - len(header_start),
        )
        magic, version, message_type, size = HEADER.unpack(header)
        if magic != MAGIC or version != VERSION or size > MAX_PAYLOAD:
            raise ValueError("invalid authentication protocol frame")
        return message_type, _recv_exact(connection, size), tuple(descriptors)
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


def recv_frame(connection: socket.socket) -> tuple[int, bytes]:
    message_type, payload, descriptors = recv_frame_with_descriptors(connection)
    for descriptor in descriptors:
        os.close(descriptor)
    if descriptors:
        raise ValueError("unexpected authentication descriptor")
    return message_type, payload


def send_frame(
    connection: socket.socket,
    message_type: int,
    payload: bytes = b"",
) -> None:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("authentication payload is too large")
    connection.sendall(
        HEADER.pack(MAGIC, VERSION, message_type, len(payload)) + payload
    )


@dataclass(frozen=True)
class LeaseSubject:
    pid: int
    start_time: int
    uid: int
    gid: int
    session_id: str

    @classmethod
    def from_payload(cls, payload: bytes) -> LeaseSubject:
        value = decode_object(
            payload,
            {"pid", "start_time", "uid", "gid", "session_id"},
        )
        if (
            not valid_integer(value["pid"], minimum=1)
            or not valid_integer(value["start_time"], minimum=1)
            or not valid_integer(value["uid"])
            or not valid_integer(value["gid"])
            or not valid_host_session_id(value["session_id"])
        ):
            raise ValueError("invalid registration metadata")
        return cls(
            pid=value["pid"],
            start_time=value["start_time"],
            uid=value["uid"],
            gid=value["gid"],
            session_id=value["session_id"],
        )

    def payload(self) -> bytes:
        return json.dumps(
            {
                "pid": self.pid,
                "start_time": self.start_time,
                "uid": self.uid,
                "gid": self.gid,
                "session_id": self.session_id,
            },
            separators=(",", ":"),
        ).encode()

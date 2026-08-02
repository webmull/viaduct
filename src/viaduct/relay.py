"""Bidirectional byte piping — the tunnel data path."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable

CHUNK = 65536
#: cap on a single request head we'll buffer before giving up and piping raw
MAX_HEAD = 262144

#: Close a spliced connection after this much silence in BOTH directions.
#: Long-lived WebSockets stay up as long as either side sends anything
#: (browser WS clients ping on their own).
DEFAULT_IDLE_TIMEOUT = 300.0


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy bytes reader → writer until EOF or either side dies, then close the writer.

    Closing the writer is what lets the paired pipe (the opposite direction)
    see EOF and finish — connections are one-shot, there is no half-close.
    """
    await _pipe_tracking(reader, writer, None)


async def splice(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
    idle_timeout: float | None = DEFAULT_IDLE_TIMEOUT,
    on_b_first: Callable[[bytes], None] | None = None,
    rewrite_host: bytes | None = None,
    rewrite_initial: bytes = b"",
) -> None:
    """Pipe a→b and b→a concurrently; returns when both directions are done.

    The idle timeout is shared across both directions: a connection dies only
    when NEITHER side has sent anything for *idle_timeout* seconds, so a
    one-way-quiet stream (e.g. server-push WebSocket) survives.

    *on_b_first*, if given, is called once with the first chunk the b side sends
    (used to peek at a response status line for --inspect). It never affects the
    bytes on the wire and its exceptions are swallowed.

    *rewrite_host*, if given, makes the a→b direction rewrite each request's Host
    header to it (for --host-header); *rewrite_initial* is any request bytes
    already read off the a side (the first head) that must go through the rewrite.
    """
    loop = asyncio.get_running_loop()
    last_activity = [loop.time()]
    if rewrite_host is not None:
        a_to_b = rewrite_request_host(
            a_reader, b_writer, rewrite_host, last_activity, rewrite_initial
        )
    else:
        a_to_b = _pipe_tracking(a_reader, b_writer, last_activity)
    pipes = asyncio.gather(
        a_to_b,
        _pipe_tracking(b_reader, a_writer, last_activity, on_first=on_b_first),
        return_exceptions=True,
    )
    if idle_timeout is None:
        await pipes
        return
    watchdog = asyncio.create_task(_watchdog(last_activity, idle_timeout, a_writer, b_writer))
    try:
        await pipes
    finally:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog


async def _pipe_tracking(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    last_activity: list[float] | None,
    on_first: Callable[[bytes], None] | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    try:
        while data := await reader.read(CHUNK):
            if on_first is not None:
                with contextlib.suppress(Exception):
                    on_first(data)  # inspection only; must never break the pipe
                on_first = None
            if last_activity is not None:
                last_activity[0] = loop.time()
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


def _rewrite_host(head: bytes, host: bytes) -> bytes:
    """Replace (or insert) the Host header value in an HTTP/1.1 request head."""
    if re.search(rb"(?im)^Host:", head):
        return re.sub(rb"(?im)^Host:[ \t]*[^\r\n]*", b"Host: " + host, head, count=1)
    i = head.find(b"\r\n")  # no Host header: inject it right after the request line
    return head if i == -1 else head[: i + 2] + b"Host: " + host + b"\r\n" + head[i + 2 :]


def _content_length(head: bytes) -> int | None:
    m = re.search(rb"(?im)^Content-Length:[ \t]*(\d+)", head)
    return int(m.group(1)) if m else None


def _wants_raw(head: bytes) -> bool:
    # a chunked body or a protocol upgrade (WebSocket): stop framing, pipe raw
    return bool(
        re.search(rb"(?im)^Transfer-Encoding:[^\r\n]*chunked", head)
        or re.search(rb"(?im)^Upgrade:[ \t]*\S", head)
    )


async def rewrite_request_host(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: bytes,
    last_activity: list[float] | None = None,
    initial: bytes = b"",
) -> None:
    """Pipe reader → writer, rewriting the Host header of each HTTP/1.1 request.

    Frames requests that have no body or a Content-Length precisely; on a chunked
    body, a protocol upgrade (WebSocket), an oversized head, or EOF it flips to a
    raw pipe, so the byte stream is always intact, it just stops rewriting there.
    """
    loop = asyncio.get_running_loop()
    buf = bytearray(initial)

    async def _more() -> bool:
        data = await reader.read(CHUNK)
        if not data:
            return False
        if last_activity is not None:
            last_activity[0] = loop.time()
        buf.extend(data)
        return True

    raw = False
    try:
        while not raw:
            while (i := buf.find(b"\r\n\r\n")) == -1:
                if len(buf) > MAX_HEAD or not await _more():
                    raw = True
                    break
            if raw:
                break
            head = bytes(buf[: i + 4])
            del buf[: i + 4]
            writer.write(_rewrite_host(head, host))
            if _wants_raw(head):
                raw = True
                break
            n = _content_length(head)  # forward exactly the declared body, if any
            while n:
                if not buf and not await _more():
                    await writer.drain()
                    return
                take = min(n, len(buf))
                writer.write(bytes(buf[:take]))
                del buf[:take]
                n -= take
            await writer.drain()
        if buf:  # raw tail: flush anything buffered, then pipe the rest verbatim
            writer.write(bytes(buf))
            buf.clear()
        while data := await reader.read(CHUNK):
            if last_activity is not None:
                last_activity[0] = loop.time()
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def _watchdog(
    last_activity: list[float], idle_timeout: float, *writers: asyncio.StreamWriter
) -> None:
    loop = asyncio.get_running_loop()
    while True:
        remaining = last_activity[0] + idle_timeout - loop.time()
        if remaining <= 0:
            for writer in writers:
                writer.close()
            return
        await asyncio.sleep(remaining)

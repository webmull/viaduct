"""Bidirectional byte piping — the tunnel data path."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

CHUNK = 65536

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
) -> None:
    """Pipe a→b and b→a concurrently; returns when both directions are done.

    The idle timeout is shared across both directions: a connection dies only
    when NEITHER side has sent anything for *idle_timeout* seconds, so a
    one-way-quiet stream (e.g. server-push WebSocket) survives.

    *on_b_first*, if given, is called once with the first chunk the b side sends
    (used to peek at a response status line for --inspect). It never affects the
    bytes on the wire and its exceptions are swallowed.
    """
    loop = asyncio.get_running_loop()
    last_activity = [loop.time()]
    pipes = asyncio.gather(
        _pipe_tracking(a_reader, b_writer, last_activity),
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

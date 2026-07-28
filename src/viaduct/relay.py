"""Bidirectional byte piping — the tunnel data path."""

from __future__ import annotations

import asyncio
import contextlib

CHUNK = 65536


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy bytes reader → writer until EOF or either side dies, then close the writer.

    Closing the writer is what lets the paired pipe (the opposite direction)
    see EOF and finish — connections are one-shot, there is no half-close.
    """
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def splice(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
) -> None:
    """Pipe a→b and b→a concurrently; returns when both directions are done."""
    await asyncio.gather(
        pipe(a_reader, b_writer),
        pipe(b_reader, a_writer),
        return_exceptions=True,
    )

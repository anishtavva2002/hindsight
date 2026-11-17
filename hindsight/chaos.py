"""Fault-injecting TCP proxy. Works for HTTP/1.x; faults are chosen per connection."""
from __future__ import annotations

import asyncio
import random
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

_BODY_503 = b"chaos: injected failure\n"
ERROR_503 = (
    b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain\r\n"
    + f"Content-Length: {len(_BODY_503)}\r\n".encode()
    + b"Connection: close\r\nX-Chaos: injected\r\n\r\n"
    + _BODY_503
)
_BODY_502 = b"chaos proxy: upstream unreachable\n"
BAD_GATEWAY = (
    b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\n"
    + f"Content-Length: {len(_BODY_502)}\r\n".encode()
    + b"Connection: close\r\n\r\n"
    + _BODY_502
)


@dataclass
class FaultConfig:
    latency_ms: int = 0
    jitter_ms: int = 0
    error_rate: float = 0.0      # answer with HTTP 503, never touch upstream
    reset_rate: float = 0.0      # drop the connection immediately (TCP RST)
    corrupt_rate: float = 0.0    # forward, but garble the response body
    blackhole_rate: float = 0.0  # accept, then never respond
    seed: int | None = None

    def validate(self) -> None:
        rates = (self.error_rate, self.reset_rate, self.corrupt_rate, self.blackhole_rate)
        if any(r < 0 or r > 1 for r in rates):
            raise ValueError("rates must be between 0 and 1")
        if sum(rates) > 1.0 + 1e-9:
            raise ValueError("error+reset+corrupt+blackhole rates must sum to <= 1.0")
        if self.latency_ms < 0 or self.jitter_ms < 0:
            raise ValueError("latency/jitter must be >= 0")

    def describe(self) -> str:
        parts = []
        if self.latency_ms:
            parts.append(f"latency={self.latency_ms}ms±{self.jitter_ms}")
        for name in ("error_rate", "reset_rate", "corrupt_rate", "blackhole_rate"):
            v = getattr(self, name)
            if v:
                parts.append(f"{name.replace('_rate', '')}={v:.0%}")
        return ", ".join(parts) or "none (pass-through)"


class ChaosProxy:
    def __init__(self, listen_host, listen_port, upstream_host, upstream_port,
                 faults: FaultConfig | None = None, logger=None):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.faults = faults or FaultConfig()
        self.faults.validate()
        self.stats: Counter = Counter()
        self._rng = random.Random(self.faults.seed)
        self._log = logger or (lambda msg: None)
        self._server = None
        self._writers: set = set()

    async def start(self) -> "ChaosProxy":
        self._server = await asyncio.start_server(self._handle, self.listen_host, self.listen_port)
        self.listen_port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for w in list(self._writers):
            try:
                w.close()
            except Exception:
                pass
        await self._server.wait_closed()

    # -- fault selection -------------------------------------------------
    def _pick(self) -> str:
        f = self.faults
        r = self._rng.random()
        acc = 0.0
        for name, p in (("reset", f.reset_rate), ("blackhole", f.blackhole_rate),
                        ("error", f.error_rate), ("corrupt", f.corrupt_rate)):
            acc += p
            if r < acc:
                return name
        return "pass"

    def _delay(self) -> float:
        f = self.faults
        ms = f.latency_ms + (self._rng.uniform(-f.jitter_ms, f.jitter_ms) if f.jitter_ms else 0)
        return max(0.0, ms) / 1000.0

    @staticmethod
    def _garble(chunk: bytes, header_done: bool):
        def flip(data: bytes) -> bytes:
            b = bytearray(data)
            for i in range(0, len(b), 7):
                b[i] ^= 0x5A
            return bytes(b)

        if not header_done:
            idx = chunk.find(b"\r\n\r\n")
            if idx == -1:
                return chunk, False
            return chunk[: idx + 4] + flip(chunk[idx + 4:]), True
        return flip(chunk), True

    # -- connection handling ---------------------------------------------
    async def _handle(self, reader, writer) -> None:
        self._writers.add(writer)
        self.stats["connections"] += 1
        n = self.stats["connections"]
        fault = self._pick()
        self.stats[fault] += 1
        started = time.monotonic()
        delay = self._delay()
        try:
            if delay:
                await asyncio.sleep(delay)
            if fault == "reset":
                writer.transport.abort()
                return
            if fault == "blackhole":
                while await reader.read(65536):
                    pass
                return
            if fault == "error":
                await self._drain_request(reader)
                writer.write(ERROR_503)
                await writer.drain()
                return
            await self._forward(reader, writer, corrupt=(fault == "corrupt"))
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            self.stats["io_errors"] += 1
        finally:
            self._writers.discard(writer)
            if not writer.is_closing():
                writer.close()
            self._log(
                f"{datetime.now().strftime('%H:%M:%S')} conn#{n} fault={fault} "
                f"delay={int(delay * 1000)}ms took={int((time.monotonic() - started) * 1000)}ms"
            )

    @staticmethod
    async def _drain_request(reader) -> None:
        """Read one full HTTP request so closing doesn't RST the client's 503."""
        buf = b""
        try:
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
                if not chunk:
                    return
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            m = re.search(rb"content-length:\s*(\d+)", head, re.I)
            need = (min(int(m.group(1)), 1_000_000) - len(rest)) if m else 0
            while need > 0:
                chunk = await asyncio.wait_for(reader.read(min(need, 65536)), timeout=5)
                if not chunk:
                    return
                need -= len(chunk)
        except asyncio.TimeoutError:
            return

    async def _forward(self, creader, cwriter, corrupt: bool) -> None:
        try:
            ureader, uwriter = await asyncio.wait_for(
                asyncio.open_connection(self.upstream_host, self.upstream_port), timeout=5
            )
        except (OSError, asyncio.TimeoutError):
            self.stats["upstream_unreachable"] += 1
            cwriter.write(BAD_GATEWAY)
            await cwriter.drain()
            return
        self._writers.add(uwriter)

        async def pump(src, dst, garble: bool) -> None:
            header_done = False
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    if garble:
                        chunk, header_done = self._garble(chunk, header_done)
                    dst.write(chunk)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                try:
                    dst.write_eof()
                except Exception:
                    pass

        up = asyncio.create_task(pump(creader, uwriter, False))
        down = asyncio.create_task(pump(ureader, cwriter, corrupt))
        try:
            await down
        finally:
            up.cancel()
            await asyncio.gather(up, return_exceptions=True)
            self._writers.discard(uwriter)
            uwriter.close()

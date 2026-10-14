"""One application-level voice operation at a time; SDK owns wire recovery.

This module coordinates connection state. The adapter in bot.py performs
the actual Discord operations.
"""
import asyncio
from dataclasses import dataclass
import logging
from typing import Awaitable, Callable, Protocol

from recovery import Diagnosis, classify_failure, next_retry_delay

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snapshot:
    ready: bool
    session_exists: bool = False
    connected: bool = False
    channel_id: int | None = None
    muted_deafened: bool = False


class VoiceAdapter(Protocol):
    def snapshot(self) -> Snapshot: ...
    async def join(self) -> None: ...
    async def move_back(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def ensure_silent(self) -> None: ...
    async def diagnose(self) -> Diagnosis: ...


class VoiceController:
    def __init__(self, adapter: VoiceAdapter, channel_id: int, *,
                 check_interval: float = 5.0, recovery_grace: float = 90.0,
                 diagnostic_interval: float = 60.0,
                 retry_delay: Callable[[int, str], float] = next_retry_delay):
        self.adapter = adapter
        self.channel_id = channel_id
        self.desired_connected = True  # Deliberately resets on every process start.
        self.shutdown_requested = False
        self.connection_state = "starting"
        self.request_version = 0
        self.retry_attempt = 0
        self._check_interval = check_interval
        self._recovery_grace = recovery_grace
        self._diagnostic_interval = diagnostic_interval
        self._retry_delay = retry_delay
        self._wake = asyncio.Event()
        self._operation: asyncio.Task | None = None
        self._operation_name = ""
        self._operation_version = 0
        self._retry_at = 0.0
        self._unhealthy_since: float | None = None
        self._healthy_since: float | None = None
        self._next_diagnostic = 0.0

    def notify(self) -> None:
        """Wake the existing loop. An event never creates a second loop."""
        self._wake.set()

    def request_join(self) -> None:
        if self.shutdown_requested:
            return
        if not self.desired_connected:
            self.desired_connected = True
            self.request_version += 1
            self._retry_at = 0.0
            self.retry_attempt = 0
        # Repeated /join does not defeat backoff or create duplicate connects.
        self.notify()

    def request_leave(self) -> None:
        self.desired_connected = False  # Set BEFORE cancellation/disconnection.
        self.request_version += 1
        self._retry_at = 0.0
        self._unhealthy_since = None
        if (self._operation and not self._operation.done() and self._operation_name != "leave"
                and self._operation.cancelling() == 0):
            self._operation.cancel()
        self.notify()

    def request_shutdown(self) -> None:
        self.shutdown_requested = True
        self.request_leave()

    def _state(self, value: str) -> None:
        if self.connection_state != value:
            self.connection_state = value
            log.info("voice_state=%s", value)

    def _start(self, name: str, call: Callable[[], Awaitable[None]]) -> None:
        assert self._operation is None
        self._operation_name = name
        self._operation_version = self.request_version
        self._operation = asyncio.create_task(call(), name=f"voice-{name}")
        self._operation.add_done_callback(lambda _: self.notify())

    async def _reap(self, now: float) -> None:
        task = self._operation
        if task is None or not task.done():
            return
        self._operation = None
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            # An obsolete join failure must not delay a newly requested leave.
            if self._operation_version != self.request_version:
                return
            diagnosis = classify_failure(error)
            self.retry_attempt += 1
            delay = self._retry_delay(self.retry_attempt, diagnosis.category)
            self._retry_at = now + delay
            self._state("blocked" if diagnosis.category == "configuration" else "retrying")
            log.warning("operation=%s cause=%s retry_in=%.1fs detail=%s",
                        self._operation_name, diagnosis.category, delay, diagnosis.detail)

    async def _replace_session(self) -> None:
        """Only used for a confirmed removal or an exhausted recovery attempt."""
        version = self.request_version
        await self.adapter.disconnect()
        if (self.desired_connected and not self.shutdown_requested
                and version == self.request_version and self.adapter.snapshot().ready):
            await self.adapter.join()

    async def _diagnose_stalled_session(self) -> None:
        version = self.request_version
        diagnosis = await self.adapter.diagnose()
        log.warning("stalled_voice cause=%s detail=%s", diagnosis.category, diagnosis.detail)
        # Recheck AFTER the network await: the SDK may have recovered meanwhile.
        snap = self.adapter.snapshot()
        if (version != self.request_version or not self.desired_connected
                or self.shutdown_requested or not snap.ready
                or snap.connected or diagnosis.preserve_session):
            return
        log.warning("Voice recovery exceeded grace period; trying a fresh session.")
        await self._replace_session()

    async def _step(self) -> None:
        now = asyncio.get_running_loop().time()
        await self._reap(now)
        if self._operation is not None:
            return  # Cancellation cleanup must finish before the next operation.
        snap = self.adapter.snapshot()
        if not self.desired_connected:
            self._healthy_since = None
            self._state("paused")
            if now >= self._retry_at and (snap.session_exists or (snap.ready and snap.channel_id is not None)):
                self._start("leave", self.adapter.disconnect)
            return
        if not snap.ready:
            self._state("waiting_for_gateway")
            # A gateway outage does not mean an existing voice session is dead.
            return
        healthy = (snap.session_exists and snap.connected
                   and snap.channel_id == self.channel_id and snap.muted_deafened)
        if healthy:
            self._state("connected")
            self._unhealthy_since = None
            self._next_diagnostic = 0.0
            if self._healthy_since is None:
                self._healthy_since = now
            # Brief joins followed by removals do not reset a failure storm.
            if now - self._healthy_since >= 30:
                self.retry_attempt = 0
                self._retry_at = 0.0
            return
        self._healthy_since = None
        if now < self._retry_at:
            return
        if not snap.session_exists:
            self._state("connecting")
            self._start("join", self.adapter.join)
        elif snap.channel_id is None:
            self._state("returning")
            self._start("return_after_removal", self._replace_session)
        elif snap.channel_id != self.channel_id:
            self._state("returning")
            self._start("return_after_move", self.adapter.move_back)
        elif not snap.connected:
            self._state("recovering")
            if self._unhealthy_since is None:
                self._unhealthy_since = now
            if now - self._unhealthy_since >= self._recovery_grace and now >= self._next_diagnostic:
                self._next_diagnostic = now + self._diagnostic_interval
                self._start("diagnose_recovery", self._diagnose_stalled_session)
        else:
            self._start("restore_mute_deafen", self.adapter.ensure_silent)

    async def run(self) -> None:
        try:
            while not self.shutdown_requested:
                self._wake.clear()
                await self._step()
                delay = self._check_interval
                if self._retry_at > asyncio.get_running_loop().time():
                    delay = min(delay, self._retry_at - asyncio.get_running_loop().time())
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=max(0.001, delay))
                except TimeoutError:
                    pass
        finally:
            self.shutdown_requested = True
            self.desired_connected = False
            if self._operation is not None:
                self._operation.cancel()
                await asyncio.gather(self._operation, return_exceptions=True)
                self._operation = None
            try:
                await asyncio.wait_for(self.adapter.disconnect(), timeout=10)
            except Exception as error:
                log.warning("Shutdown voice cleanup: %s", type(error).__name__)
            self._state("stopped")

"""Bounded single-flight admission for one private Rumik server process."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import isfinite
from time import monotonic
from typing import Any


class AdmissionError(RuntimeError):
    """Base error for an admission decision that did not reach the transport."""


class AdmissionOverloadedError(AdmissionError):
    """The bounded waiting queue has no remaining capacity."""


class AdmissionClosedError(AdmissionError):
    """The gate is closing or closed and cannot admit a new request."""


class AdmissionQuarantinedError(AdmissionError):
    """An ambiguous transport outcome requires server verification before reuse."""


class AdmissionTimeoutError(AdmissionQuarantinedError):
    """The transport exceeded its admission timeout and may still be running remotely."""


class AdmissionTransportError(AdmissionQuarantinedError):
    """The transport raised after a request may have reached the private server."""


@dataclass(frozen=True)
class AdmissionResult:
    """A definitive transport response and its admission timing.

    Parameters:
        value: Transport response, including definitive non-success HTTP statuses.
        queue_wait_seconds: Time spent waiting behind an active request.
        request_seconds: Time from transport start until its definitive response.
    """

    value: Any
    queue_wait_seconds: float
    request_seconds: float


Transport = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class _Request:
    """One local caller waiting for the exclusive transport slot.

    Parameters:
        payload: Isolated request data passed to the transport.
        future: Result channel for the caller that submitted the payload.
        enqueued_at: Monotonic time when the caller joined the bounded queue.
        started: Whether the worker has committed the request to the transport.
    """

    payload: dict[str, Any]
    future: asyncio.Future[AdmissionResult]
    enqueued_at: float
    started: bool = False


class SingleFlightAdmission:
    """Serialize private-server work with a bounded local waiting queue.

    A caller cancelled while waiting is removed before its payload reaches the
    transport. Cancellation after transport start only cancels that caller's wait:
    the worker drains the shielded request until it responds or times out. A timeout
    cancels the local transport task, but still quarantines this gate because the
    remote process may have completed the request. Transport exceptions also
    quarantine the gate. Recreate it after the server has been verified or restarted.

    Args:
        transport: Async private-server request function.
        queue_capacity: Maximum waiting callers, excluding the one active request.
        timeout_seconds: Positive finite limit for a definitive transport response.
    """

    def __init__(
        self,
        transport: Transport,
        queue_capacity: int = 2,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Initialize an idle gate without starting a background task.

        Args:
            transport: Async private-server request function.
            queue_capacity: Maximum waiting callers, excluding the active request.
            timeout_seconds: Positive finite request timeout in seconds.
        """
        if (
            not isinstance(queue_capacity, int)
            or isinstance(queue_capacity, bool)
            or queue_capacity < 0
        ):
            raise ValueError("queue_capacity must be a non-negative integer")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        self._transport = transport
        self._queue_capacity = queue_capacity
        self._timeout_seconds = timeout_seconds
        self._queue: deque[_Request] = deque()
        self._worker_task: asyncio.Task[None] | None = None
        self._active: _Request | None = None
        self._active_transport_task: asyncio.Task[Any] | None = None
        self._unsettled_transport_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._quarantine: AdmissionQuarantinedError | None = None

    @property
    def quarantined(self) -> bool:
        """Return whether an ambiguous outcome permanently blocked new admissions."""
        return self._quarantine is not None

    @property
    def waiting_count(self) -> int:
        """Return the number of queued requests not yet claimed by the worker."""
        return len(self._queue)

    async def submit(self, payload: dict[str, Any]) -> AdmissionResult:
        """Wait for exclusive admission and return one definitive transport result.

        Args:
            payload: JSON-compatible private-server request fields.

        Returns:
            Definitive transport response and local timing measurements.

        Raises:
            AdmissionOverloadedError: If all waiting slots are occupied.
            AdmissionQuarantinedError: If an earlier request had an ambiguous outcome.
            AdmissionClosedError: If close has begun.
            asyncio.CancelledError: If this caller cancels while waiting or active.
        """
        self._ensure_open()
        projected_waiting = len(self._queue) + (1 if self._active is not None else 0)
        if projected_waiting > self._queue_capacity:
            raise AdmissionOverloadedError("Rumik admission queue is full")
        loop = asyncio.get_running_loop()
        request = _Request(dict(payload), loop.create_future(), monotonic())
        self._queue.append(request)
        self._ensure_worker()
        try:
            return await asyncio.shield(request.future)
        except asyncio.CancelledError:
            if not request.started:
                try:
                    self._queue.remove(request)
                except ValueError:
                    pass
            if not request.future.done():
                # A cancelled active caller no longer observes this future. The
                # worker still owns the transport task and drains it before reuse.
                request.future.cancel()
            raise

    async def close(self) -> None:
        """Reject waiting callers and wait for any started transport work to settle.

        ``close`` does not cancel an active request before its configured timeout.
        A timed-out request is cancelled locally, while its ambiguous remote outcome
        permanently quarantines the gate. ``close`` waits for the worker's bounded
        local-cancellation attempt.
        """
        if not self._closed:
            self._closed = True
            self._reject_waiting(AdmissionClosedError("Rumik admission is closed"))
        worker = self._worker_task
        if worker is not None and worker is not asyncio.current_task():
            await asyncio.shield(worker)

    async def __aenter__(self) -> SingleFlightAdmission:
        """Enter an admission scope that closes on exit."""
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        """Close the gate after a scoped admission use."""
        await self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise AdmissionClosedError("Rumik admission is closed")
        if self._quarantine is not None:
            raise AdmissionQuarantinedError(str(self._quarantine))

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._run(), name="rumik-single-flight-admission"
            )

    async def _run(self) -> None:
        try:
            while self._queue:
                if self._quarantine is not None or self._closed:
                    return
                request = self._queue.popleft()
                if request.future.cancelled():
                    continue
                request.started = True
                self._active = request
                await self._execute(request)
                self._active = None
                if self._quarantine is not None:
                    return
        finally:
            self._active = None
            self._active_transport_task = None
            if self._quarantine is not None:
                self._reject_waiting(self._quarantine)
            elif self._closed:
                self._reject_waiting(AdmissionClosedError("Rumik admission is closed"))
            self._worker_task = None

    async def _execute(self, request: _Request) -> None:
        started_at = monotonic()
        transport_task = asyncio.create_task(
            self._transport(request.payload), name="rumik-transport"
        )
        self._active_transport_task = transport_task
        try:
            value = await asyncio.wait_for(asyncio.shield(transport_task), self._timeout_seconds)
        except TimeoutError:
            timeout = AdmissionTimeoutError("Rumik transport timed out; admission is quarantined")
            self._quarantine_gate(timeout)
            if not request.future.done():
                request.future.set_exception(timeout)
            await self._cancel_transport(transport_task)
        except asyncio.CancelledError:
            failure = AdmissionTransportError("Rumik transport cancelled; admission is quarantined")
            self._quarantine_gate(failure)
            if not request.future.done():
                request.future.set_exception(failure)
            if not transport_task.done():
                await self._drain_transport(transport_task)
        except Exception:
            failure = AdmissionTransportError("Rumik transport failed; admission is quarantined")
            self._quarantine_gate(failure)
            if not request.future.done():
                request.future.set_exception(failure)
        else:
            if not request.future.done():
                request.future.set_result(
                    AdmissionResult(
                        value=value,
                        queue_wait_seconds=started_at - request.enqueued_at,
                        request_seconds=monotonic() - started_at,
                    )
                )
        finally:
            self._active_transport_task = None

    async def _drain_transport(self, task: asyncio.Task[Any]) -> None:
        """Await an active caller's transport without allowing its result to reopen admission."""
        try:
            await asyncio.shield(task)
        except BaseException:
            pass

    async def _cancel_transport(self, task: asyncio.Task[Any]) -> None:
        """Cancel a timed-out local transport task and wait boundedly for it to settle."""
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=min(self._timeout_seconds, 1.0))
        except TimeoutError:
            self._unsettled_transport_tasks.add(task)
            task.add_done_callback(self._retire_transport)
        except BaseException:
            pass

    def _retire_transport(self, task: asyncio.Task[Any]) -> None:
        """Discard a quarantined result and observe any late local exception."""
        self._unsettled_transport_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _quarantine_gate(self, error: AdmissionQuarantinedError) -> None:
        self._quarantine = error
        self._reject_waiting(error)

    def _reject_waiting(self, error: AdmissionError) -> None:
        while self._queue:
            request = self._queue.popleft()
            if not request.future.done():
                request.future.set_exception(type(error)(str(error)))

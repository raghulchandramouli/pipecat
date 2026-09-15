"""Concurrency and ambiguous-outcome coverage for Rumik single-flight admission."""

from __future__ import annotations

import asyncio

import pytest

from demo.rumik.admission import (
    AdmissionClosedError,
    AdmissionOverloadedError,
    AdmissionQuarantinedError,
    AdmissionResult,
    AdmissionTimeoutError,
    SingleFlightAdmission,
)


@pytest.mark.asyncio
async def test_late_failure_after_bounded_cancel_is_observed_without_reopening_gate():
    """An uncooperative local request stays tracked until its late result is discarded."""
    release = asyncio.Event()

    async def transport(_payload):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        raise OSError("late local failure")

    gate = SingleFlightAdmission(transport, timeout_seconds=0.01)
    try:
        with pytest.raises(AdmissionQuarantinedError):
            await gate.submit({})
        await asyncio.wait_for(gate.close(), timeout=1)
        assert gate.quarantined
        assert len(gate._unsettled_transport_tasks) == 1
    finally:
        release.set()
        await _yield()
    assert gate._unsettled_transport_tasks == set()


async def _yield() -> None:
    """Let newly created admission tasks reach their next await point."""
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_single_active_transport_and_bounded_waiting_queue() -> None:
    """Only one payload reaches the private transport while two callers wait locally."""
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum_active = 0
    calls: list[int] = []

    async def transport(payload):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        calls.append(payload["id"])
        if payload["id"] == 1:
            started.set()
            await release.wait()
        active -= 1
        return {"status": 200, "id": payload["id"]}

    gate = SingleFlightAdmission(transport, queue_capacity=2)
    try:
        first = asyncio.create_task(gate.submit({"id": 1}))
        await asyncio.wait_for(started.wait(), timeout=1)
        second = asyncio.create_task(gate.submit({"id": 2}))
        third = asyncio.create_task(gate.submit({"id": 3}))
        await _yield()
        assert gate.waiting_count == 2
        with pytest.raises(AdmissionOverloadedError):
            await gate.submit({"id": 4})

        release.set()
        results = await asyncio.gather(first, second, third)
        assert all(isinstance(result, AdmissionResult) for result in results)
        assert [result.value["id"] for result in results] == [1, 2, 3]
        assert calls == [1, 2, 3]
        assert maximum_active == 1
        assert results[1].queue_wait_seconds >= 0
        assert results[0].request_seconds >= 0
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_zero_waiting_capacity_allows_idle_work_then_rejects_overlap() -> None:
    """A zero-capacity gate still admits one idle request and admits another after it ends."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def transport(payload):
        calls.append(payload["id"])
        if payload["id"] == 1:
            started.set()
            await release.wait()
        return payload

    gate = SingleFlightAdmission(transport, queue_capacity=0)
    try:
        first = asyncio.create_task(gate.submit({"id": 1}))
        await asyncio.wait_for(started.wait(), timeout=1)
        with pytest.raises(AdmissionOverloadedError):
            await gate.submit({"id": 2})
        release.set()
        await first
        assert (await gate.submit({"id": 3})).value == {"id": 3}
        assert calls == [1, 3]
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_queued_cancellation_removes_capacity_without_starting_remote_work() -> None:
    """A cancelled waiting caller does not consume queue space or reach the transport."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def transport(payload):
        calls.append(payload["id"])
        if payload["id"] == 1:
            started.set()
            await release.wait()
        return payload

    gate = SingleFlightAdmission(transport, queue_capacity=1)
    try:
        first = asyncio.create_task(gate.submit({"id": 1}))
        await asyncio.wait_for(started.wait(), timeout=1)
        queued = asyncio.create_task(gate.submit({"id": 2}))
        await _yield()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert gate.waiting_count == 0

        replacement = asyncio.create_task(gate.submit({"id": 3}))
        release.set()
        await asyncio.gather(first, replacement)
        assert calls == [1, 3]
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_active_cancellation_keeps_transport_exclusive_until_it_drains() -> None:
    """Cancelling the caller does not permit a second remote request before the first settles."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []
    active = 0
    maximum_active = 0

    async def transport(payload):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        calls.append(payload["id"])
        if payload["id"] == 1:
            started.set()
            await release.wait()
        active -= 1
        return payload

    gate = SingleFlightAdmission(transport)
    try:
        active_caller = asyncio.create_task(gate.submit({"id": 1}))
        await asyncio.wait_for(started.wait(), timeout=1)
        active_caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active_caller

        waiting = asyncio.create_task(gate.submit({"id": 2}))
        await _yield()
        assert calls == [1]
        release.set()
        assert (await waiting).value == {"id": 2}
        assert calls == [1, 2]
        assert maximum_active == 1
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_active_cancellation_discards_unobserved_later_transport_error() -> None:
    """A cancelled active caller does not leave its later failure on an unobserved future."""
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def transport(_payload):
        started.set()
        await release.wait()
        finished.set()
        raise OSError("connection dropped")

    gate = SingleFlightAdmission(transport)
    try:
        caller = asyncio.create_task(gate.submit({"id": 1}))
        await asyncio.wait_for(started.wait(), timeout=1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)
        await _yield()
        assert gate.quarantined is True
        with pytest.raises(AdmissionQuarantinedError):
            await gate.submit({"id": 2})
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_timeout_cancels_local_work_and_quarantines_pending_and_future_admission() -> None:
    """A timeout cancels local work while rejecting all use after its ambiguous outcome."""
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def transport(payload):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return payload

    gate = SingleFlightAdmission(transport, queue_capacity=1, timeout_seconds=0.01)
    try:
        first = asyncio.create_task(gate.submit({"id": 1}))
        await asyncio.wait_for(started.wait(), timeout=1)
        waiting = asyncio.create_task(gate.submit({"id": 2}))
        with pytest.raises(AdmissionTimeoutError):
            await first
        with pytest.raises(AdmissionQuarantinedError):
            await waiting
        assert gate.quarantined is True
        with pytest.raises(AdmissionQuarantinedError):
            await gate.submit({"id": 3})

        await asyncio.wait_for(cancelled.wait(), timeout=1)
        await asyncio.wait_for(gate.close(), timeout=1)
    finally:
        release.set()
        await gate.close()


@pytest.mark.asyncio
async def test_transport_error_quarantines_but_definitive_status_response_does_not() -> None:
    """Only raised transport failures are ambiguous; returned HTTP status objects remain safe."""

    async def failing_transport(_payload):
        raise OSError("connection dropped")

    failed = SingleFlightAdmission(failing_transport)
    try:
        with pytest.raises(AdmissionQuarantinedError):
            await failed.submit({"id": 1})
        assert failed.quarantined is True
        with pytest.raises(AdmissionQuarantinedError):
            await failed.submit({"id": 2})
    finally:
        await failed.close()

    statuses = iter(({"status": 503}, {"status": 200}))

    async def definitive_transport(_payload):
        return next(statuses)

    safe = SingleFlightAdmission(definitive_transport)
    try:
        assert (await safe.submit({"id": 1})).value == {"status": 503}
        assert (await safe.submit({"id": 2})).value == {"status": 200}
        assert safe.quarantined is False
    finally:
        await safe.close()


@pytest.mark.asyncio
async def test_close_rejects_new_work() -> None:
    """Closing an idle gate makes later submission fail explicitly."""
    gate = SingleFlightAdmission(lambda _payload: asyncio.sleep(0))
    await gate.close()
    with pytest.raises(AdmissionClosedError):
        await gate.submit({})

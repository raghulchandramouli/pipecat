"""Adapter-to-ledger regression coverage for selective transcript repair."""

from __future__ import annotations

import base64
import json

import pytest
from websockets.protocol import State

from demo.interview.browser_stt import BrowserSarvamSTTService
from demo.interview.ledger import TranscriptLedger
from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection


class _Websocket:
    """Open websocket fixture that records only the adapter's wire messages."""

    def __init__(self) -> None:
        self.state = State.OPEN
        self.sent: list[dict[str, object]] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))


async def _send_pcm(service: BrowserSarvamSTTService) -> None:
    async for _ in service.run_stt(b"\0" * 1600):
        pass


async def _open(service: BrowserSarvamSTTService, candidate_turn_id: int) -> int:
    service.set_candidate_turn(candidate_turn_id)
    await service.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert service._active_boundary_id is not None
    return service._active_boundary_id


async def _close(service: BrowserSarvamSTTService) -> None:
    await service.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


@pytest.mark.asyncio
async def test_adapter_repair_drops_a_retired_final_and_waits_for_fresh_substantive_speech(
    monkeypatch,
) -> None:
    """The actual timestamp callbacks preserve a missing gap until fresh speech resolves it."""
    service = BrowserSarvamSTTService(api_key="test-key", connection_generation=7)
    service._websocket = _Websocket()
    service._sample_rate = 16_000
    ledger = TranscriptLedger(connection_generation=7, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=5, question_id="q1")
    clock = 0.0

    @service.event_handler("on_manual_boundary")
    def boundary(_service, event):
        nonlocal clock
        clock += 1.0
        ledger.record_boundary(event, now=clock)

    @service.event_handler("on_utterance_bound")
    def binding(_service, event):
        ledger.bind_boundary(event.boundary_id, event.segment_id)

    @service.event_handler("on_segment_final")
    def final(_service, event):
        ledger.record_final(event)

    @service.event_handler("on_correlation_failure")
    def failure(_service, event):
        ledger.correlation_failure(event)

    first_boundary = await _open(service, candidate_turn_id=5)
    await _send_pcm(service)
    await _close(service)
    assert ledger.expire(20)
    assert ledger.repairable_boundary_ids == (first_boundary,)

    assert service.can_repair_boundary(first_boundary)
    assert service.retire_boundary(first_boundary)
    assert ledger.begin_repair(first_boundary)
    # Retry is only an obligation until a subsequent, separately bound speech
    # interval completes. It cannot release the earlier answer by itself.
    assert not ledger.repair_replacement_ready
    assert not ledger.readiness

    replacement_boundary = await _open(service, candidate_turn_id=5)
    await _send_pcm(service)
    await _close(service)

    # This is delayed packet A. Its old interval is known and retired, so it
    # cannot bind the new interval or turn timestamp trust into a fresh failure.
    await service._handle_final_transcript(
        {"utterance_idx": 11, "start_s": 0.0, "end_s": 0.05, "text": "late A"}
    )
    assert service.retired_final_count == 1
    assert ledger.repair_replacement_records == ()
    assert not ledger.readiness

    await service._handle_final_transcript(
        {"utterance_idx": 12, "start_s": 0.05, "end_s": 0.10, "text": "replacement"}
    )
    assert ledger.repair_replacement_ready
    assert not ledger.resolve_repair_replacement(replacement_boundary, substantive=False)
    assert not ledger.readiness
    assert ledger.resolve_repair_replacement(replacement_boundary, substantive=True)
    assert ledger.readiness
    assert ledger.snapshot.text == "replacement"


@pytest.mark.asyncio
async def test_repair_acknowledgment_does_not_consume_a_later_substantive_window() -> None:
    """An early acknowledgment cannot permanently occupy the repair replacement slot."""
    service = BrowserSarvamSTTService(api_key="test-key", connection_generation=7)
    service._websocket = _Websocket()
    service._sample_rate = 16_000
    ledger = TranscriptLedger(connection_generation=7, transcript_final_timeout=10.0)
    ledger.begin_candidate_turn(candidate_turn_id=5, question_id="q1")

    @service.event_handler("on_manual_boundary")
    def boundary(_service, event):
        ledger.record_boundary(event, now=float(event.boundary_id + 1))

    @service.event_handler("on_utterance_bound")
    def binding(_service, event):
        ledger.bind_boundary(event.boundary_id, event.segment_id)

    @service.event_handler("on_segment_final")
    def final(_service, event):
        ledger.record_final(event)

    first_boundary = await _open(service, candidate_turn_id=5)
    await _send_pcm(service)
    await _close(service)
    assert ledger.expire(20)
    assert service.retire_boundary(first_boundary)
    assert ledger.begin_repair(first_boundary)

    replacement_boundary = await _open(service, candidate_turn_id=5)
    await _send_pcm(service)
    await _close(service)
    await service._handle_final_transcript(
        {"utterance_idx": 12, "start_s": 0.05, "end_s": 0.10, "text": "okay"}
    )

    assert ledger.repair_replacement_ready
    assert not ledger.resolve_repair_replacement(replacement_boundary, substantive=False)
    assert not ledger.readiness

    later_boundary = await _open(service, candidate_turn_id=5)
    await _send_pcm(service)
    await _close(service)
    await service._handle_final_transcript(
        {
            "utterance_idx": 13,
            "start_s": 0.10,
            "end_s": 0.15,
            "text": "I resolved the customer problem.",
        }
    )

    assert ledger.repair_replacement_ready
    assert ledger.resolve_repair_replacement(later_boundary, substantive=True)
    assert ledger.readiness


@pytest.mark.asyncio
async def test_timestamp_failure_is_sticky_and_disables_selective_repair() -> None:
    """Malformed correlation cannot be cleared later to make an interval repairable."""
    service = BrowserSarvamSTTService(api_key="test-key", connection_generation=7)
    service._websocket = _Websocket()
    service._sample_rate = 16_000
    boundary = await _open(service, candidate_turn_id=5)
    await _send_pcm(service)
    await _close(service)

    await service._handle_final_transcript(
        {"utterance_idx": 12, "start_s": None, "end_s": 0.05, "text": "unsafe"}
    )

    assert not service.timestamp_correlation_trusted
    assert not service.can_repair_boundary(boundary)
    assert not service.retire_boundary(boundary)

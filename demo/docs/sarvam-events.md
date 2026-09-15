# Sarvam segment events

Loop 02 adds an application-side extension of the checkout's
[`SarvamRealtimeSTTService`](../../src/pipecat/services/sarvam/stt.py).
Implementation lives in `demo/interview/sarvam_events.py`; the framework is unchanged.
This is a service-event integration seam, not the interview transcript gate or a
working voice bot. Loop 03 owns ordering, deduplication, finalization deadlines,
and dispatch readiness.

## Provider correlation

The [official realtime guide](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/realtime-streaming)
was consulted on 12 September 2026. It describes client-sent manual `speech_start`
and `speech_end`, optional final timestamps, and partial/final transcript events.
It does not establish a client-supplied boundary ID or a numeric correspondence
between local speech boundaries and provider utterance IDs. The local source and
its regression fixtures carry `utterance_idx` in transcript event payloads.
No live provider session was run for loop 02.

Keep two identities separate:

- A local boundary ID records a manual speech interval and its candidate-turn owner.
- `SegmentId(connection_generation, utterance_idx)` identifies a provider utterance.

Set the candidate-turn owner before sending its VAD start frame. Ownership is
captured at that start and must not change when the active turn changes. Bind a
provider utterance explicitly to the captured local boundary only when the caller
has evidence for that match. Do not infer the match from final arrival order,
a local counter, or whichever candidate turn is active when a final arrives.
Provider duplicates remain observable; the ledger is responsible for deduplication.

Unbound finals remain distinguishable from bound `SegmentFinal` outcomes. Empty
finals are terminal observations even though the base service emits no text frame.
Malformed IDs, conflicting bindings, failed boundary sends, and retention overflow
must reach the recovery/gating layer as diagnostics, not silently become accepted
transcripts. Retrospective binding can resolve a retained final without waiting
inside `process_frame`.

A live correlator still needs to validate provider evidence for matching an ID to
a local boundary, including empty/no-partial cases. If that evidence is absent or
ambiguous, loop 03 must hold dispatch and recover; this extension does not supply
an undocumented server guarantee.

## Application API

Construct `InterviewSarvamRealtimeSTTService(api_key=..., connection_generation=N)`.
It defaults to manual endpointing and rejects a different endpointing mode.
`set_candidate_turn(candidate_turn_id)` arms the next VAD start; call it for every
new local speech segment, even when several segments belong to the same answer.

Register fast handlers with `service.event_handler(event_name)`:

| Event | Payload / purpose |
| --- | --- |
| `on_manual_boundary` | `SarvamManualBoundary`: local ID, captured owner, start/end, and send success. |
| `on_final_observed` | `SarvamFinalObservation`: each raw final, including duplicate/empty/invalid-ID cases. |
| `on_unbound_final` | A final without usable local ownership; requires correlation or recovery. |
| `on_segment_final` | `SegmentFinal`: a final with explicit connection/utterance/candidate ownership. |
| `on_correlation_failure` | `SarvamCorrelationFailure`: refusal or loss of correlation information. |
| `on_connection_event` | `SarvamConnectionEvent`: failed connect/receive, receive end, provider error, or intentional shutdown. |

After observing the real provider ID and establishing its local boundary match,
call `await service.bind_provider_utterance(boundary_id=..., utterance_idx=...)`.
The method derives the candidate owner from the stored boundary, refuses conflicting
rebinding, and replays retained finals. It also works inside an `on_unbound_final`
handler, before the base transcript handler runs. `max_unbound_finals` defaults to
32; overflow emits a diagnostic and the raw observation remains visible, but the
excess result is not retained for later replay. The gate must treat that diagnostic
as a recovery condition.

## Ordering and connection ownership

The service instance owns one immutable connection generation. Replace the service
with a new generation on reconnect, retaining the old event generations so the
controller can reject them. Candidate ownership never depends on bot response
generation; interruption cannot relabel valid delayed STT results.

Final observation and bound-result handlers run before delegation to the base
final handler. Keep synchronous handlers fast: record state or enqueue work, then
return. Missing finals and semantic decisions belong in managed background tasks.
The base handler remains responsible for usage metrics and normal transcript
frames; the extension does not strip or deduplicate those frames.

The base manual-boundary path still sends buffered audio before `speech_end`.
The extension observes boundary-send outcomes without inserting a wait for STT
results. Connection failures/exhaustion and intentional shutdown are separate
lifecycle outcomes. A receive-loop exit still leaves the base service unusable
unless it was intentionally disconnected.

## Setup and validation

From the repository root, use the existing demo environment or create it as shown
in [the demo README](../README.md). Prepare and install a copy of this checkout so
package-build artifacts and dependencies remain under `demo/`:

```bash
python3 demo/scripts/prepare_pipecat.py
uv pip install --python demo/.venv/bin/python 'demo/.build/pipecat[sarvam]' -r demo/requirements-sarvam.txt
demo/.venv/bin/python -m pytest -c demo/pytest.ini demo/tests tests/test_sarvam_stt.py
demo/.venv/bin/ruff check demo
demo/.venv/bin/ruff format --check demo
```

`demo/pytest.ini` imports framework modules from the current repository `src/`.
The installed snapshot provides distribution metadata and the declared core/Sarvam
dependencies; it uses the source package's fallback development version outside
a Git checkout. Tests mock provider I/O while exercising actual framework methods.
Model weights and live credentials are unnecessary for these checks.

## Transcript gate events

`on_speech_start_requested(connection_generation)` runs before a manual boundary
send; the gate uses it to invalidate pending responses and arm the candidate owner.
`candidate_turn_for_next_boundary` exposes the armed owner without consuming it.
`on_utterance_bound(SarvamUtteranceBinding)` carries `boundary_id`, `segment_id`, and
`candidate_turn_id`, and fires before retained final replay. Repeating an identical
binding does not emit another binding event. See [transcript gate](transcript-gate.md)
for the coordinator that consumes these events and removes raw transcript frames.

# Transcript ledger and Gemini admission

The application gate admits inference only when speech is quiet, every local
boundary closed successfully, and every closed boundary has an explicitly bound
terminal final. The ledger joins text in local boundary order, ignores identical
duplicates, and treats empty finals as terminal without inventing answer text.
Conflicting, failed, unbound, or timed-out results block inference and require
recovery. An old connection cannot complete a new connection's segment.

## Components and placement

Use the same `LLMContext` for `TranscriptLedgerProcessor`,
`InterviewUserAggregator`, and the assistant aggregator. Register one
`InterviewGoogleLLMService` with that coordinator. The intended pipeline is:

```text
transport input → InterviewSarvamRealtimeSTTService
  → TranscriptLedgerProcessor → InterviewUserAggregator
  → InterviewGoogleLLMService → reply guard/TTS/output → assistant aggregator
```

The reply guard, TTS, controller, and browser wiring belong to later loops. This
module is an integration building block, not a runnable interview bot.

Before audio starts, call `ledger.begin_candidate_turn(candidate_turn_id=...,
question_id=...)` and `coordinator.attach_sarvam(stt)`. The pre-send speech-start
event invalidates old responses and arms the candidate owner before Sarvam's
boundary send can wait on the network. Successful boundary bindings emit
`on_utterance_bound` before retained final replay, so the ledger knows their owner.

Provider correlation remains explicit: call
`await stt.bind_provider_utterance(boundary_id=..., utterance_idx=...)` only with verified
association evidence. Neither numerical provider order nor FIFO arrival establishes
that association. An unbound final remains blocked and has a deadline; no automatic
live correlator has been demonstrated. See [Sarvam events](sarvam-events.md).

The ledger is synchronous. Event callbacks mutate it and call `coordinator.changed()`;
other controller mutations must also call `changed()`. The coordinator uses managed
tasks for deadlines and provider calls. Audio processing never waits for a final
transcript or model response. End/cancel closes admission before aggregator teardown
can flush pending work. Use the default runner task manager so provider child
tasks inherit the response context; supplying a fixed custom task context is not
supported by this integration.

## Dispatch interception

| Framework path | Application interception |
| --- | --- |
| Raw Sarvam final transcripts | Coordinator and user aggregator suppress raw finals; only a fresh ledger snapshot reaches turn detection. |
| User stop, deferred aggregation, inference trigger, watchdog stop | User `_push_aggregation` requests admission without appending pending text to canonical history. |
| Speculative/eager inference | `_run_speculative_inference` discards provisional context and requests a canonical snapshot. |
| User `LLMRunFrame`, append/update/transform with `run_llm`, idle handlers, queued strategy contexts | User `push_frame` intercepts every `LLMContextFrame` in either direction. |
| Assistant run, semantic retry, tool-result or image context pushed upstream | LLM mixin intercepts context at the actual service entrance in either direction. |
| Scheduled work and Google stream retries | Current response token is checked again before provider invocation and each SDK stream request. |
| Direct one-shot inference and context summarization | Disabled in this application service because they bypass transcript admission. |

At admission, the coordinator deep-copies canonical history and tools and adds one
user message for the complete pending transcript. It does not trust a context held
by an earlier trigger. A provider request does not accept or score the answer, and
does not append it to canonical history. Loop 05 owns acceptance and progression.

Speech resumption invalidates pending response tokens and cancels managed provider
work. Valid delayed transcript finals remain attached to the candidate turn. Output
from a stale provider generation is dropped even if it arrives after cancellation.
This protects future output; already emitted speech needs the later interruption
and audio-output guard.

`on_dispatch` exposes the admitted snapshot and its metadata: response token,
dispatch ID, candidate-turn ID, and transcript revision. `on_transcript_recovery`
exposes a recoverable error code, also emitted as a nonfatal pipeline error. An
explicit controller recovery reset is required before incomplete transcript state
can be discarded. Reconnection requires a strictly newer generation and matching
service/coordinator wiring.

## Validation and setup

Install the local source snapshot and Sarvam dependencies as described in
[Sarvam setup](sarvam-events.md), then install the gate's Google dependencies:

```bash
uv pip install --python demo/.venv/bin/python -r demo/requirements-gate.txt
demo/.venv/bin/python -m pytest -c demo/pytest.ini demo/tests
demo/.venv/bin/ruff check demo
demo/.venv/bin/ruff format --check demo
```

Ledger tests exercise ordering, deduplication, terminal outcomes, deadlines, stale
connections, and recovery. Gate tests exercise actual framework aggregator paths
and managed cancellation. Google tests run the real service through a mocked SDK
request boundary. Sarvam tests drive actual boundary and parsed-message handlers.
These tests make no live provider calls and establish no audio latency result.

Loop 04 adds pause/thinking grace and validates completion markers and reply kinds.
Readiness here establishes transcript integrity; it does not decide that the
candidate has finished answering or authorize speech or grading.

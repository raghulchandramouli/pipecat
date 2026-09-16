# Turn policy and reply authorization

This is the loop 04 turn-policy contract for the interview demo. The
implementation is composed by [`turn_coordinator.py`](../interview/turn_coordinator.py),
[`turn_policy.py`](../interview/turn_policy.py), and
[`reply_guard.py`](../interview/reply_guard.py) around the transcript gate. It
does not claim live audio behavior or provider latency. The related readiness
boundary is [`transcript-gate.md`](transcript-gate.md), and Sarvam event ownership
is [`sarvam-events.md`](sarvam-events.md).

## Unified completion policy

The coordinator admits a candidate response only when all three conditions hold:

1. Local VAD has ended speech and the 2.5-second candidate pause floor has elapsed.
2. Every closed segment has a terminal final outcome, including an explicit empty
   outcome; missing or failed finals enter recovery.
3. Gemini's completion judgment is requested only after the first two conditions,
   then returns a valid complete or incomplete decision.

An early STT final does not bypass the pause floor. A 0.5–2.0 second hesitation
keeps the pending answer open. Semantic judgment follows transcript readiness; it
is not a second pre-Gemini gate.

Initial timing values come from the design and are starting defaults:

| Timer | Value | Meaning |
| --- | ---: | --- |
| Candidate pause | 2.5 s | Minimum quiet after VAD stop before a completion probe. |
| Incomplete short retry | 8 s | First retry/check-in interval after an incomplete judgment. |
| Incomplete long retry | 30 s | Longer retry interval for a deliberate thinking pause. |
| Quiet watchdog | 45 s | Notification fallback after a quiet boundary, never answer acceptance. |
| Provider watchdog | 45 s | Starts at dispatch admission and ends at guard terminal; timeout cancels and recovers. |
| User idle notification | 15 s | Event-triggered opportunity to offer help. |
| Explicit thinking grace | 30 s | Grace anchored to the candidate's “give me a moment” request. |

The 10-second transcript-final timeout remains a foundation default and needs later
tuning. These values are neither measured performance nor provider guarantees.

Thinking mode owns its 30-second deadline. In ordinary quiet time, idle notification
is 15 seconds and the quiet watchdog is 45 seconds. An admitted request has
a separate 45-second bound; expiry invalidates and cancels it, emits a nonfatal
diagnostic, then schedules a guarded check-in after 8 seconds. While thinking is active,
semantic retries, idle notifications, and watchdog responses are suppressed or rescheduled. Speech
resumption cancels the pending prompt and invalidates the response token while
retaining valid delayed finals for the same candidate turn. Deadline expiry resumes
normal policy and may produce a check-in; it cannot silently accept a missing final.

## Completion and reply contract

The reply guard parses one complete model response and requires an exact completion
marker before authorizing speech. Missing, malformed, oversized, or stale streams
produce no TTS and no interview-state mutation. Partial marker text is buffered and
cannot escape as speech. The controller supplies a response authorization with the
current response token, dispatch ID, candidate turn, transcript revision, and
controller-owned reply kind.

`FOLLOW_UP` with a valid complete marker may make an answer eligible for later
controller acceptance. `CHECK_IN` is allowed to speak when policy permits, but never accepts,
grades, or advances its pending answer even if its marker says complete. The reply
guard validates generation and marker before forwarding text to TTS; the controller
still owns acceptance and question progression.

## Coordinator and aggregator composition

Construct the components with one shared context, ledger, and monotonic
clock. `create_user_aggregator()` installs one VAD start strategy and an external
stop strategy; the built-in watchdog is disabled (`inf`) and built-in idle handling
is disabled (`0`) because the coordinator owns those timers. `request_thinking(requested_at=...)` anchors explicit thinking grace. Call
`begin_waiting()` after a question finishes to arm idle notifications even if no
candidate speech arrives. It rejects calls during speech; it neither fabricates a
transcript nor permits empty-ledger model calls. Voice-intent recognition is a
later controller responsibility.

`create_reply_guard()` must sit before TTS, output transport, and assistant
aggregation. `InterviewGoogleLLMService` is configured with strict replies: it
adds the completion instructions, avoids the built-in early completion broadcast,
blocks model tools, and rejects direct `run_inference`. The application guard owns
full-response marker validation before any speakable text is forwarded.

The gate must cover every route that can cause Gemini work. Method names below are
current framework observations and application wrappers:

| Route | Required application behavior |
| --- | --- |
| `push_aggregation()` / stop and deferred inference | The coordinator requests a fresh ledger snapshot; pending text is not appended to canonical history before admission. |
| `push_context_frame()` | `InterviewUserAggregator` gates normal run, append, update, and transform context frames when `run_llm` is requested. |
| Direct speculative `push_frame()` | `InterviewUserAggregator` suppresses or replaces speculative context with a complete ledger snapshot; speculation cannot bypass readiness. |
| User strategy `_on_push_frame` | Strategy requests are queued and the coordinator admits them after readiness. |
| Incoming context frames | Apply the same gate in both directions, including assistant/tool/image context that could trigger a run. |
| Semantic timeout | A validated incomplete result schedules an 8/30-second check-in. Its instruction is added only to the next copied provider context. Built-in append/run retry timers do not drive the interview. |
| Watchdog stop | Request recovery or a policy check-in through the gate; never accept a truncated answer. |
| Idle event | Treat the user-idle event as a request for a controller-authorized check-in, not automatic completion. |
| End/stop/cancel teardown | Close admission first, cancel managed timer/provider tasks, then allow aggregator teardown to flush safely. |
| Summary or direct `run_inference` | `InterviewGoogleLLMService.run_inference` is disabled because it bypasses transcript admission. |

`InterviewTurnCoordinator` exposes dispatch metadata (response token, dispatch ID,
candidate-turn ID, and transcript revision) and recovery errors. Every provider
invocation and retry rechecks authorization. `process_frame` remains responsive;
deadlines and inference use managed tasks.

## Timing behavior and validation

Deterministic tests must show repeated hesitation produces no spoken reply, early
finals do not bypass 2.5 seconds, and thinking grace prevents competing prompts.
They must show malformed, unmarked, stale, and interrupted responses produce no
speech or progression; check-ins preserve pending segments; and missing finals
produce recovery. The coordinator emits `on_reply_decision` for validated replies
and `on_reply_rejected` for fail-closed rejection or watchdog timeout. It does not
accept or score answers; later controller work owns those mutations. No live
provider, browser, audio, or latency claim is made by this document.

## Composition example

```python
from demo.interview.google_llm import InterviewGoogleLLMService
from demo.interview.ledger import TranscriptLedger
from demo.interview.turn_coordinator import InterviewTurnCoordinator
from pipecat.processors.aggregators.llm_context import LLMContext

context = LLMContext()
ledger = TranscriptLedger(connection_generation=1, transcript_final_timeout=10)
ledger.begin_candidate_turn(candidate_turn_id=1, question_id="q1")
coordinator = InterviewTurnCoordinator(ledger=ledger, context=context)
user = coordinator.create_user_aggregator()  # Supply local vad_analyzer when wiring audio.
llm = InterviewGoogleLLMService(coordinator=coordinator, api_key=backend_google_key)
guard = coordinator.create_reply_guard()
coordinator.attach_sarvam(stt)  # Matching generation; explicit provider-ID binding required.
# Pipeline: input -> stt -> coordinator -> user -> llm -> guard -> TTS/output -> assistant.
```

`stt` and `backend_google_key` are backend-provided objects/configuration. Browser,
TTS, controller startup, and live provider setup remain later-loop integration.
Use `demo/requirements-gate.txt`; no additional dependencies are required here.

`on_policy_action` reports due idle/watchdog/retry/probe actions; it is a notification,
not permission to accept an answer. `ReplyDecision.completion.is_valid` exposes
parser validity. Accepted syntax is `●` plus whitespace and nonempty speech, `◐`
(short incomplete), or `○` (long incomplete). Other/mixed/trailing markers fail
validation. Completed and rejected dispatch IDs are retired against replay.

Tests under `demo/tests/` cover pure policy clocks, guard chunk/replay/error races,
managed coordinator integration, and actual Google streaming with its SDK mocked.
Run `demo/.venv/bin/python -m pytest -c demo/pytest.ini demo/tests -q`. The next loop
owns voice-intent recognition, acceptance/scoring, and interview progression.

`on_reply_decision` runs before approved speech is released. A controller consuming
an eligible answer must preserve the admitted ledger revision and response
identity through that release. Clearing the ledger or advancing its candidate turn
inside that callback invalidates the reply; defer that transition until output
release or add an explicit release acknowledgment when implementing loop 05.

Use [interaction-acceptance.md](interaction-acceptance.md) to exercise hesitation,
resumed speech, and check-in suppression. Keep server finalization, dispatch,
guard authorization, and actual playback timestamps distinct when measuring delay.

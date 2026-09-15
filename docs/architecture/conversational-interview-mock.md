# Conversational interview assistant: mock design

**Status:** Design draft; no interview bot or Rumik adapter is implemented by this document.
**Prepared:** 12 September 2026.
**Code baseline:** This Pipecat checkout at `4bae10d06`.
**Audience:** Product and engineering teams building a voice interview prototype.

## 1. Product outcome

The candidate speaks naturally, pauses to think, and continues without pressing a
“done speaking” button. The interviewer waits for a complete answer, asks one
relevant follow-up, and stops talking when the candidate interrupts.

The proposed application uses **Sarvam for speech recognition**, **Gemini 3.8 Flash
for interview reasoning**, and **Rumik for spoken replies**, coordinated by Pipecat.
End-to-end here describes the complete audio-in to audio-out application.

The mock assumes a browser-based practice interview, initially in Indian English,
with configurable interview role, difficulty, duration, and question rubric.
Additional languages and code-switching require their own audio acceptance tests.
If this becomes a hiring assessment, define the assessment policy separately;
the prototype produces evidence-based feedback for human review.

Browser microphone permission and audio-playback permission may require an initial
user gesture. After that setup, turn-taking is automatic. The candidate can say
“repeat the question,” “give me a moment,” “skip this question,” or “end the interview.”

## 2. Stack and verification status

| Layer | Selected component | Status and role |
| --- | --- | --- |
| Browser audio | WebRTC transport with microphone input and speaker output | Proposed transport; enable echo cancellation and test with speakers and headsets. |
| Orchestration | Pipecat `PipelineWorker` and `WorkerRunner` | Present in this checkout; one interview session owns its pipeline and context. |
| Speech-to-text (STT) | `SarvamRealtimeSTTService`, `saaras:v3-realtime` | Present in this checkout; partial transcripts support live captions and final segments feed interview context. |
| Speech activity | `SileroVADAnalyzer` | Present; voice activity detection (VAD) detects physical speech and silence locally. |
| Turn policy | Pause grace plus `FilterIncompleteUserTurnStrategies` | Existing building blocks; strict final-transcript coordination still needs application work. |
| Language model (LLM) | `GoogleLLMService`, explicitly `gemini-3.8-flash` | Model ID verified in Google documentation; configure thinking explicitly as described below. |
| Text-to-speech (TTS) | Provisional mapping: `rumik-ai/rumik-oss-1` | Likely intended by “Rumik-3B”; owner confirmation is pending. A custom Pipecat adapter is required. |
| Interview control | Question/rubric state, verbal controls, turn ledger | Proposed application module. |

Google lists `gemini-3.8-flash` as a text-output model with streaming-compatible
text generation, function calling, and structured output support. Its model page
does not list audio generation or Live API support. Use the normal Pipecat Google
text-LLM service between STT and TTS.
[Google model reference](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash),
[local Google integration](../../src/pipecat/services/google/llm.py).

The likely Rumik checkpoint is a 3B multilingual TTS model producing 24 kHz audio.
Its published weights use CC BY-NC 4.0 with an acceptable-use addendum. The model
card says commercial products and commercial self-hosting require separate
permission. Record the intended use and applicable permissions before a commercial
rollout. The complete stack includes hosted Sarvam and Google services.
[Rumik model card and license](https://huggingface.co/rumik-ai/rumik-oss-1#license).

## 3. Candidate experience

```text
Practice interview · Backend engineering · Question 2 of 6

Interviewer: “Tell me about a production incident you helped resolve.”

Status: Listening
Live captions: “We first checked the database ...”

Speak naturally. You can pause, ask for time, or interrupt the interviewer.
```

The status changes between **Listening**, **Giving you time**, **Thinking**,
**Speaking**, and **Reconnecting**. Captions are provisional until STT finalizes
them. A pause does not advance the question counter. No per-answer recording,
submit, or stop-speaking control appears in the main conversation flow.

| Moment | Candidate behavior | Required response |
| --- | --- | --- |
| Opening | Grants microphone access | Introduce the AI interviewer and explain the voice controls. |
| Answering | Speaks for 90 seconds | Keep listening and accumulate segments into one answer. |
| Hesitation | Pauses for 1.5 seconds, then continues | Remain silent; preserve the same answer. |
| Thinking | Says “give me a moment” | Acknowledge briefly and enter a longer waiting state. |
| Completion | Finishes a thought and stays quiet | Verify transcript completeness, then allow a response. |
| Interruption | Speaks while the interviewer talks | Stop outgoing speech and listen to the candidate. |
| Closing | Says “end the interview” | End questioning, give a short closing, and release the audio session. |

## 4. Architecture

```text
Candidate microphone
        |
        v
Browser WebRTC -> Pipecat transport.input()
        |
        v
SarvamRealtimeSTTService
  audio -> interim captions + final transcript segments
        |
        v
User context aggregation + local VAD + interview turn coordination
  pause grace / transcript readiness / semantic completion
        |
        v
GoogleLLMService: gemini-3.8-flash
  completion judgment, next question, clarification, or feedback
        |
        v
Proposed reply guard: validate reply kind, completion signal, and generation
        |
        v
Proposed Rumik TTS adapter -> private Rumik inference service
        |
        v
transport.output() -> Browser speaker
        |
        v
Assistant context aggregation

Candidate speech-start -> interruption -> cancel pending reply and queued audio
Interview controller <-> accepted turns, question state, rubric, session lifecycle
```

Use the order demonstrated by the local
[Sarvam realtime example](../../examples/voice/voice-sarvam-realtime.py).
Keep the assistant aggregator after output so conversation state follows delivered
speech as closely as the transport permits. A Rumik adapter without word timestamps
must document the remaining uncertainty about exactly which words were heard
when playback was interrupted.

The proposed turn coordinator is an application responsibility spanning STT event
tracking and LLM dispatch. The diagram does not imply that Pipecat already ships
an `InterviewTurnCoordinator` class.

## 5. Automatic end-of-answer detection

### Separate speech segments from interview answers

VAD answers “is there speech right now?” STT answers “what words were spoken?”
The interview turn policy answers “should the interviewer respond now?”

Use `SarvamRealtimeSTTService(endpointing="manual")` with local VAD. In this API,
**manual means the application sends speech boundaries automatically**. Pipecat
sends `speech_start` and `speech_end` from VAD events; it flushes the last buffered
audio before `speech_end`. The candidate does not signal these boundaries.

A candidate may produce several finalized Sarvam utterances during one answer.
Accumulate those segments; do not equate a provider final transcript with permission
to ask the next question.
[Local Sarvam implementation](../../src/pipecat/services/sarvam/stt.py),
[manual-boundary regression tests](../../tests/test_sarvam_stt.py).

### Proposed initial interview policy

These values are prototype starting points, not measured performance or universal
defaults.

| Setting or rule | Initial choice | Purpose |
| --- | --- | --- |
| Local VAD start/stop confirmation | `0.2` seconds each, current defaults | Detect activity promptly; keep the longer interview pause policy separate. |
| Candidate pause grace | `user_speech_timeout=2.5` seconds | Preserve ordinary hesitation before requesting completion judgment. |
| Transcript waiting | `wait_for_transcript=True` | Retain Pipecat's existing STT wait, supplemented by the strict gate below. |
| Semantic completion | `FilterIncompleteUserTurnStrategies` around the timeout detector | Let Gemini suppress a reply when the candidate's thought is incomplete. |
| Incomplete-turn check-ins | Short: 8 seconds; long: 30 seconds | Configure the filter's retry timers instead of retaining its 5/10-second defaults. |
| Turn-stop watchdog | `user_turn_stop_timeout=45.0` seconds | Place the fallback beyond the proposed thinking interval; it still cannot accept an answer. |
| Idle notification | `user_idle_timeout=15.0` seconds | Give the application an opportunity to check whether the candidate needs help. |
| Explicit request for thinking time | Proposed 30-second grace, then a gentle check-in | Avoid repeated prompts while the candidate considers an answer. |

The timeout strategy retains its pause floor even when a final transcript arrives
quickly. Speech resumption cancels a pending completion. The semantic filter may
call Gemini during a pause, but an incomplete verdict suppresses the spoken reply
and keeps the answer open. Completion-control markers must never reach TTS.
[Timeout strategy](../../src/pipecat/turns/user_stop/speech_timeout_user_turn_stop_strategy.py),
[completion filter](../../src/pipecat/turns/user_turn_strategies.py),
[filter example](../../examples/turn-management/turn-management-filter-incomplete-turns.py).

Thinking mode must coordinate all timers. While its explicit deadline is active,
suppress or reschedule semantic retries, idle prompts, and watchdog-triggered
responses. The configured long retry reduces conflicts but does not establish this
policy by itself; its clock starts at the incomplete verdict, not the candidate's
request. Resume normal timing when speech resumes or the grace period expires.

Do not put a timeout strategy and Smart Turn beside each other and assume both
must agree: stop strategies can independently trigger completion. Smart Turn's
`stop_secs` is a maximum-silence fallback, not a minimum grace period. If semantic
audio detection is added later, combine its decision with the pause policy
explicitly and re-run the hesitation tests.
[Smart Turn strategy](../../src/pipecat/turns/user_stop/turn_analyzer_user_turn_stop_strategy.py),
[Smart Turn parameters](../../src/pipecat/audio/turn/smart_turn/base_smart_turn.py).

### Strict transcript and generation gate: proposed work

The existing `wait_for_transcript=True` setting can proceed after an STT deadline
with earlier transcript text. It does not prove that every segment of the current
answer has finalized. The controller watchdog is another possible completion path.

Add application coordination with these invariants:

1. Identify segments by connection generation and provider `utterance_idx`.
   Track open speech boundaries and outstanding final results.
2. Deduplicate and order final segments before they enter user context. Display
   interim text as captions; never save it as a completed answer.
3. Hold **every** user-context dispatch to Gemini, including semantic completion
   probes, until all closed segments have final outcomes and the candidate is quiet.
4. Accept STT finals by connection generation and candidate-turn/segment ownership,
   independently of bot response generation. If speech resumes while a Gemini or
   TTS request is waiting or running, invalidate that response generation and reject
   its output. Retain valid delayed STT finals for the pending candidate answer.
5. Observe empty final events at the STT event boundary too. This service does not
   emit a text frame for an empty final; treating it as an outstanding text frame
   forever would deadlock the interview.
6. If finalization times out or the connection fails, enter a recoverable transcript
   error state. Ask the candidate to repeat the missing part after recovery. Do not
   advance the rubric or generate feedback from a silently truncated answer.

Deduplicate and order transcripts upstream of the user aggregator, and intercept
or replace its normal context-dispatch path before Gemini. The aggregator normally
accepts each final transcript and can emit context immediately on a deferred
completion trigger; a guard after the aggregator or LLM cannot supply both gates.
Keep readiness in controller-owned state and use tracked asynchronous tasks for
deadlines and dispatch. Never await missing finals inside `process_frame`: audio,
speech-resumption events, and interruptions must continue flowing.

This requires a service-event extension and aggregation/dispatch integration; it
is not achieved by the configuration fragment alone. Commit an interview answer
only on a validated answer-completion decision with its full transcript. A Pipecat
turn-stop event or the filter's **complete** marker alone is insufficient: built-in timeout prompts
also request that marker for a gentle check-in. Preserve the pending interview
answer across those conversational boundaries. Watchdog expiry and idle events
may cause a check-in, never an automatic question advance.
[Transcript aggregation](../../src/pipecat/processors/aggregators/llm_response_universal.py),
[turn controller](../../src/pipecat/turns/user_turn_controller.py).

Add a proposed reply guard before TTS and interview-state changes. The built-in
semantic filter forwards unmarked text when a response ends, so it is not a strict
output-validation boundary. The guard needs an explicit validity signal from the
completion parser and a controller-owned reply kind, such as check-in, follow-up,
or closing. Suppress missing/malformed decisions and stale generations; a timeout
check-in may speak without accepting or grading the answer. This is additional
integration work, not behavior the configuration fragment supplies.
[Completion parsing and timeout prompts](../../src/pipecat/turns/user_turn_completion_mixin.py).

### Example timing

```text
0.0s  Candidate: “We checked the database ...”
1.2s  Silence begins.
1.4s  Local VAD confirms the stop and closes an STT segment.
1.8s  Sarvam final arrives; save the segment, remain silent.
2.7s  Candidate continues: “... and then found a connection leak.”
      Cancel the pending turn-end decision; keep the same answer.
6.0s  Candidate finishes.
6.2s  VAD closes the next segment and starts its pause timer.
      Wait for final outcomes and the configured pause grace.
      Gemini judges the thought complete, then generates a follow-up.
```

Timings illustrate the desired behavior. The 2.5-second candidate-pause timer
runs after the VAD-stop event, so the ordinary minimum here is about 2.7 seconds
of physical silence. Only the separate STT safety timer subtracts the VAD interval.
Measure end-to-end timing from recorded audio.

## 6. Barge-in and cancellation

Keep incoming audio active while the interviewer speaks. On a confirmed candidate
speech-start, broadcast an interruption, stop reading the old Gemini response,
discard pending TTS requests where possible, and clear queued playback.

The Rumik adapter must attach a response-generation identifier to work it starts.
If an old HTTP request finishes after interruption, discard its audio. Cancelling
the client request does not establish that the remote model stopped computing.
Keep the inference worker responsive and bound its queue so abandoned synthesis
cannot delay the next question indefinitely.

Use browser echo cancellation and calibrate speech detection against background
noise and the interviewer's own voice. Measure audible stop time at the client;
clearing a server queue cannot recall audio already played or buffered elsewhere.
[Pipecat interruption handling](../../src/pipecat/processors/frame_processor.py),
[output transport](../../src/pipecat/transports/base_output.py).

## 7. Rumik speech adapter and hosting

No Rumik service was found in this checkout. The proposed adapter extends Pipecat's
`TTSService` and calls a separately hosted inference process.

The author's example server exposes `POST /v1/audio/speech` and returns WAV audio.
The prototype assumes one complete WAV response per short sentence, then feeds
decoded audio into Pipecat **after that sentence is ready**. Incremental generation
and request serialization are unverified for the deployment revision; inspect a
pinned server revision before implementation. The design does not depend on a
server-side synthesis lock or streaming support.
[Rumik inference example](https://huggingface.co/rumik-ai/rumik-oss-1#inference),
[server source to verify](https://huggingface.co/rumik-ai/rumik-oss-1/blob/main/server.py).

| Proposed adapter obligation | Required behavior |
| --- | --- |
| Input | Speak Gemini's approved reply text; remove control markers and unsupported markup. |
| Request | Use the documented `input` and `speaker` fields; keep the inference URL server-side. |
| Audio | Decode the WAV container into mono PCM; preserve or explicitly resample its 24 kHz output. |
| Pipecat frames | Emit correctly scoped `TTSStartedFrame`, `TTSAudioRawFrame`, and `TTSStoppedFrame` using the current `context_id` contract. |
| Scheduling | Run inference in a separate process/service; never put GPU work or a blocking HTTP call inside `process_frame`. |
| Cancellation | Cancel queued work, reject late audio, and avoid replaying interrupted sentences. |
| Errors | Surface a nonfatal pipeline error and a visible recovery state; never synthesize an empty answer silently. |
| Metrics | Record request wait, synthesis time, generated audio duration, and first audible output. |

The reference inference example uses NVIDIA CUDA. No minimum VRAM or acceptable
production latency was verified for this design. Benchmark the selected GPU and
sentence lengths before sizing infrastructure. The author discourages utterances
longer than 35 seconds; short interview prompts also reduce waiting time.
[Rumik inference and limitations](https://huggingface.co/rumik-ai/rumik-oss-1),
[Pipecat TTS base](../../src/pipecat/services/tts_service.py).

Have the adapter admit one active synthesis request per reference-server process.
Expand concurrency only after measuring queues and memory. A GPU deployment is a
proposed implementation step; no model weights or serving dependencies were installed for
this document. Download model artifacts under the repository's `models/` directory
when that implementation step is undertaken; record the selected model revision.

## 8. Gemini and interview control

Set `model="gemini-3.8-flash"` and `thinking_level="low"` explicitly. Google lists
`low`, `medium`, and `high`; `minimal` is unsupported. This checkout's automatic
Flash-level fallback knows about 3.7 but otherwise selects `minimal`, so relying
on it for 3.8 can produce an invalid request. This design uses an explicit setting
without changing framework code.
[Google model reference](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash),
[local thinking defaults](../../src/pipecat/services/google/llm.py).

Keep interview progression in an application controller. Give Gemini the current
question, accepted candidate answer, relevant rubric, and previous accepted turns.
Separate the text to speak from internal assessment data. Emit exactly one prompt
at a time and ground follow-ups in what the candidate actually said.

Proposed controller state:

| Field | Purpose |
| --- | --- |
| `session_id`, `connection_generation` | Isolate interviews and reject events from obsolete connections. |
| `question_id`, `question_index`, `interview_phase` | Track introduction, questioning, follow-up, and closing. |
| `candidate_turn_id`, pending and accepted segment IDs | Associate speech segments with one answer independently of bot response generation. |
| `response_generation` | Invalidate stale Gemini and Rumik output after interruption. |
| `rubric_evidence` | Store feedback with supporting transcript excerpts and uncertainty. |
| `waiting_reason`, deadlines | Distinguish thinking time, missing transcript, and connection recovery. |

These names define a proposed internal contract, not an existing Pipecat schema.
Never score accent, hesitation, or a transcription failure as lack of competence.
If the request is practice rather than assessment, produce coaching feedback and
concrete suggestions rather than a hiring recommendation.

## 9. Configuration fragment for existing building blocks

This fragment illustrates APIs present in the checkout. It is **not a runnable
interview application**: transport, strict transcript gate, Rumik adapter, interview
controller, credentials, and runtime setup still need implementation. Syntax is
checked; provider calls have not been executed.

```python
import os

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.sarvam.stt import SarvamRealtimeSTTService
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_completion_mixin import UserTurnCompletionConfig
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies

stt = SarvamRealtimeSTTService(
    api_key=os.environ["SARVAM_API_KEY"],
    endpointing="manual",
    sample_rate=16000,
    settings=SarvamRealtimeSTTService.Settings(
        language_code="en-IN",
        stream_type="balanced",
        mode="transcribe",
    ),
)

llm = GoogleLLMService(
    api_key=os.environ["GOOGLE_API_KEY"],
    settings=GoogleLLMService.Settings(
        model="gemini-3.8-flash",
        thinking=GoogleLLMService.ThinkingConfig(thinking_level="low"),
        system_instruction=(
            "You are a practice interviewer. Ask one question at a time. "
            "Allow thinking pauses. Ground follow-ups in the candidate's answer. "
            "Keep spoken replies brief and respect requests to stop or repeat."
        ),
    ),
)

context = LLMContext()
user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
    context,
    user_params=LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(),
        user_turn_strategies=FilterIncompleteUserTurnStrategies(
            config=UserTurnCompletionConfig(
                incomplete_short_timeout=8.0,
                incomplete_long_timeout=30.0,
            ),
            stop=[
                SpeechTimeoutUserTurnStopStrategy(
                    user_speech_timeout=2.5,
                    wait_for_transcript=True,
                )
            ],
        ),
        user_turn_stop_timeout=45.0,
        user_idle_timeout=15.0,
    ),
)
```

Keep `realtime_service_mode` at its default for this STT-to-text-LLM pipeline.
That flag is for an audio-consuming realtime LLM and changes transcript-wait
behavior; Sarvam's realtime product name is not a reason to enable it.

The idle and thinking-time events need controller handlers and coordinated timer
management. Setting a timeout alone does not implement the spoken check-in,
strict finalization gate, or reply guard.

## 10. Reliability, privacy, and recovery

| Condition | Proposed product behavior |
| --- | --- |
| STT disconnects or a final is missing | Show recovery state, preserve accepted answers, and request repetition of the missing part after recovery. Replace an unusable STT service or its session pipeline. |
| Microphone input stops | Distinguish missing audio from intentional silence; show microphone guidance instead of advancing the question. |
| Gemini times out or rejects configuration | Keep the current question; emit a controlled retry/recovery message without inventing feedback. |
| Rumik is slow or unavailable | Show speech-generation status; offer text continuation. Do not silently switch the selected voice provider. |
| Candidate reconnects | Create a new connection generation, restore accepted state, and repeat the current question if its delivery is uncertain. |
| Candidate asks to stop | Cancel pending work, release microphone and transport resources, and stop further questions. |

`SarvamRealtimeSTTService` disables automatic reconnection and becomes unusable
after its receive loop exits. For an STT connection failure, create a replacement
service or rebuild the session pipeline, bind it to a new connection generation,
and restore accepted interview state. Waiting for the failed instance to reconnect
is not a recovery mechanism.
[Sarvam connection lifecycle](../../src/pipecat/services/sarvam/stt.py).

Keep Sarvam and Google credentials on the backend. Make raw-audio recording
opt-in; define transcript retention and deletion for the prototype. Keep candidate
content out of routine logs. Explain that audio reaches Sarvam and transcript
context reaches Google; self-hosting Rumik alone does not keep the whole pipeline
on the local machine.

## 11. Acceptance tests and measurement

Use Pipecat's audio-mode behavioral eval transport and real provider integrations
once the module exists. Deterministic fixtures should include actual silence,
overlapping speech, and delayed transcript delivery; text-only tests cannot prove
automatic turn-taking. Existing scenarios provide useful starting points:
[interruption](../../scripts/release-evals/scenarios/scripted/interruption_audio.yaml),
[incomplete turns](../../scripts/release-evals/scenarios/scripted/filter_incomplete_turns.yaml),
[multi-turn](../../scripts/release-evals/scenarios/scripted/multi_turn.yaml).

| Scenario | Pass condition |
| --- | --- |
| No finish-speaking control | A complete multi-question interview works after initial browser permission. |
| Hesitation | No interviewer audio during repeated 0.5–2.0-second pauses within an answer. |
| Long answer | A 90–180-second answer remains one accepted interview answer. |
| Incomplete thought | A pause after “the reason was…” produces no substantive follow-up until continuation or a gentle check-in. |
| Delayed final | Gemini dispatch waits for all closed-segment outcomes; a missing final produces recovery, not assessment. |
| Duplicate/out-of-order final | Context contains each accepted segment once, in the correct order. |
| Empty final | The coordinator resolves the segment without deadlock or a fabricated answer. |
| Resume during generation | The old reply is invalidated; the candidate's continuation and valid delayed STT finals remain in the pending answer. |
| Barge-in during playback | No late-generation audio enters playback after invalidation; already queued client audio stops within the measured audible-stop target. |
| Invalid completion response | Missing/malformed completion signals never reach TTS or advance interview state. |
| Check-in while thinking | A timed check-in preserves the pending answer; a complete marker on that check-in does not accept or grade it. |
| Echo and noise | Speaker playback, fans, and keyboard sounds do not repeatedly seize the candidate's turn. |
| Voice controls | Repeat, skip, thinking-time, and end requests change state correctly without buttons. |
| Provider failure/reconnect | Replace failed STT instances; accepted answers survive, and old-connection events cannot advance the interview. |
| Language coverage | Selected language and code-switch combinations meet a separately defined transcription and pronunciation rubric. |

Proposed initial performance targets are **at most 500 ms** from audible candidate
speech-start to audible bot-stop at p95, and **at most 5 seconds** from a clearly
finished answer to first audible reply at p95 under the prototype's single-session
load. The second target includes VAD confirmation and the deliberate 2.5-second
pause grace. These are
acceptance targets, not measured claims or provider guarantees; Rumik synthesis
may be the limiting stage.

Record VAD timing, STT final delay, gate wait, Gemini first token, TTS queue and
synthesis duration, client first-audio time, interrupted output, and premature
response rate. Track semantic probes separately because they add LLM work. Do not
estimate full conversational latency from one provider's first-token metric.

## 12. Implementation handoff

1. Confirm the Rumik checkpoint and intended license/use; select a benchmark host.
2. Assemble Sarvam realtime, the explicit Gemini configuration, local VAD, and
   user/assistant aggregators using the existing examples.
3. Implement the transcript/dispatch gate, reply guard, coordinated thinking timers,
   and interview state controller. Verify hesitation, check-ins, malformed markers,
   empty finals, delayed finals, and multi-segment answers first.
4. Verify the pinned Rumik server's response and concurrency behavior, then implement
   and benchmark the TTS adapter, including WAV decoding,
   bounded queues, interruption handling, and rejection of late audio.
5. Add browser audio/status, verbal controls, transcript handling, and recovery.
6. Run the audio acceptance suite under quiet, noisy, interrupted, and reconnecting
   conditions before presenting the module as a working interview system.

**Open decisions:** confirm whether “Rumik-3B” means `rumik-ai/rumik-oss-1`; confirm
practice versus hiring use, first supported languages, and deployment hardware.
None prevents reviewing this mock design, but they affect implementation and release.

## Evidence and documentation scope

This document combines an architecture explanation with a clearly marked proposal
and configuration reference. It does not claim a working tutorial or shipped API.
Local code takes precedence when online integration examples describe different
Pipecat revisions, especially for Sarvam endpointing and settings placement.

Useful local references:

- [Sarvam realtime voice example](../../examples/voice/voice-sarvam-realtime.py)
- [Google voice example](../../examples/voice/voice-google-gemini.py)
- [Sarvam STT regression tests](../../tests/test_sarvam_stt.py)
- [Turn strategy regression tests](../../tests/test_user_turn_stop_strategy.py)
- [Turn controller regression tests](../../tests/test_user_turn_controller.py)
- [Behavioral evaluation workflow](../../scripts/release-evals/README.md)

Provider references were checked on 12 September 2026. No live interview, model
inference, latency benchmark, or commercial deployment was performed for this draft.

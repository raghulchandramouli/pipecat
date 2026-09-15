# Interview foundation contracts

This document defines the application boundary for the conversational interview
prototype. It is a contract for later implementation, not a claim that an
interview bot exists. The source design is [`../../docs/architecture/conversational-interview-mock.md`](../../docs/architecture/conversational-interview-mock.md),
read at baseline `4bae10d06018c9ee68b8d1a4f4fff7ea94d57988`.

## Scope and assumptions

The first product assumption is a browser-based practice interview in Indian
English (`en-IN`). It is not an assessment or hiring recommendation. Feedback must
be evidence-based coaching, and must never score accent, hesitation, or a provider
transcription failure as competence. More languages, code-switching, hiring use,
and retention policy require explicit product decisions and separate acceptance
evidence.

The proposed stack is browser WebRTC audio, Pipecat orchestration, Sarvam realtime
STT with manual application speech boundaries, local Silero VAD, Gemini text LLM,
and hosted Sarvam Bulbul v3 streaming TTS. Provider APIs, model availability,
licenses, serving behavior, and latency remain integration-session work.

## Decision register

| Decision | Current position | Owner / evidence needed |
| --- | --- | --- |
| Active TTS | Sarvam bulbul:v3, shubh, en-IN, 24-kHz PCM streaming. | [Live setup and evidence](sarvam-tts.md); owner replacement on 2026-09-15. |
| Historical Rumik checkpoint | Inspected `rumik-ai/rumik-oss-1` at `0ed3c98684e14350c910129c0efa179841c41ad2`; selected by the owner for local Mac execution. | [Pinned serving contract](rumik-serving.md); live local evidence recorded in loop 06. |
| Rumik use | Practice prototype is assumed. Commercial self-hosting and commercial product use need permission review. | Product and legal decision before release. |
| Inference host | Local Apple M5 Pro, 48 GiB unified memory, MPS float32. | Loop 06 live benchmark passed; short sentences take 23–35 seconds. |
| Practice versus assessment | Practice coaching is in scope; hiring assessment policy is out of scope. | Product decision before any assessment use. |
| Languages | `en-IN` is the prototype assumption. | Define language and code-switch acceptance tests before expansion. |
| Browser transport | WebRTC with microphone/speaker permissions, echo cancellation, and headset/speaker testing. | Confirm transport wiring and browser matrix in loop 08/10. |
| Retention | Raw audio is opt-in; transcript retention and deletion are undefined. | Product/privacy decision in loop 09. |
| Provider contracts | No live provider verification is claimed by foundation work. | Recheck official current contracts in each integration session. |

## Typed contract concepts

Configuration must validate required values and reject invalid deadlines without
printing credentials. It covers role, difficulty, interview duration, question
rubric, provider settings, pause grace, transcript deadlines, retry/check-in
deadlines, watchdog and idle limits, and the explicit Gemini model
`gemini-3.8-flash` with low thinking. Configuration is application-owned and must
keep secrets server-side. Pydantic structured validation data can retain raw
inputs: log only `str(error)`, or use `.errors(include_input=False,
include_context=False)` / `.json(include_input=False, include_context=False)` when
structured output is required. The 10-second `transcript_final_timeout` is an
unmeasured foundation default to tune during transcript-gate integration.

Every finalized speech segment has a provider identity composed of:

```text
SegmentId = (connection_generation, utterance_idx)
```

The session scopes the segment ledger. `candidate_turn_id` is separate ownership
metadata that associates a terminal event with the candidate answer currently being
assembled; it is not part of `SegmentId`. A new connection increments
`connection_generation` and therefore creates new segment identities. Within one
connection, segment ownership survives bot interruption: canceling a Gemini or
TTS response does not discard valid delayed STT finals belonging to the current
candidate turn. The landed `SegmentFinal` contract represents a terminal result
with text, empty, or failed outcome. Open/closed lifecycle tracking, interim text,
ordering, and deduplication remain future ledger obligations.

The future controller must enforce the following acceptance and reply rules;
loop 01 supplies data contracts only. The application ledger distinguishes `pending` and `accepted` answers. An answer
becomes accepted only after all closed segments have final outcomes, the candidate
has remained quiet for the configured pause grace, and a validated completion
decision says the thought is complete. Missing finals enter recoverable transcript
error; they never silently produce a truncated answer or advance the rubric.

A reasoning request contains the candidate turn, current question, finalized
transcript, and response generation that owns the attempt. Its reasoning reply
contains the response generation, reply kind (`check-in`, `follow-up`, `closing`,
or equivalent application-owned value), spoken text, and completion validity.
Gemini generates text; the application owns the response-generation identifier and
reply guard. Invalid, malformed, or stale replies are suppressed before TTS and
cannot mutate interview state. A check-in may speak while the answer remains
pending; a complete marker on a check-in never accepts or grades that answer.

The landed session phase enum covers `CONNECTING`, `LISTENING`, `THINKING`,
`GENERATING`, `SPEAKING`, `RECOVERING`, `CLOSING`, and `ENDED`. The eventual
controller may expose product labels such as introduction, transcript recovery,
or reconnecting while mapping them to these phases. State stores session and
connection generation, question identity/index, pending and accepted segment IDs,
pending/accepted answer text, response generation, rubric evidence, waiting
reason, and active deadlines.

## Event flow and ownership

This is the integration contract for later loops; the foundation does not run this pipeline.

```text
browser input
  -> transport input
  -> VAD speech boundaries + Sarvam interim/final events
  -> segment ledger (order, deduplicate, resolve empty finals)
  -> candidate-turn gate (quiet + all finals + pause readiness)
  -> Gemini completion judgment and response contract
  -> reply/generation guard
  -> Sarvam Bulbul v3 TTS
  -> transport output
  -> assistant context / delivered-speech state
```

The transport owns browser I/O and audible queue clearing. Sarvam owns provider
transcription events; the application owns segment identity, ordering, finalization
readiness, and recovery. VAD owns physical speech activity, while the application
owns the interview pause policy. Gemini generates response text; the application owns response-generation IDs,
reply kind, completion validity, and question progression. Rumik owns speech
synthesis; the application owns generation tagging, bounded scheduling, stale
audio rejection, and user-visible recovery. Pipecat processors carry frames and
must remain responsive: deadlines, provider calls, and GPU work run in tracked
background tasks rather than blocking `process_frame`.

Candidate speech-start invalidates the active response generation, broadcasts an
interruption, and prevents late Gemini/Rumik output from entering playback. A new
connection increments `connection_generation`; events from an old connection can
never advance the interview.

## Planned application paths

All interview-specific implementation belongs under `demo/`:

| Path | Responsibility | Status |
| --- | --- | --- |
| `demo/__init__.py` | Importable package boundary | Landed foundation |
| `demo/__main__.py` | `python -m demo` offline configuration smoke entry point | Landed foundation |
| `demo/interview/config.py` | Validated role, rubric, provider, and deadline settings | Landed foundation |
| `demo/interview/contracts.py` | Segment, answer, response, and session state models | Landed foundation |
| `demo/interview/providers.py` | Provider interface seams and deterministic fakes | Landed foundation |
| `demo/interview/sarvam_events.py` | Sarvam boundary, final, and connection events with explicit binding | Implemented loop 02 |
| `demo/interview/ledger.py` | Future ordering, deduplication, and strict finalization gate | Future loop 03 |
| `demo/interview/turn_policy.py` | Future pause, thinking-time, and reply guard | Future loop 04 |
| `demo/interview/controller.py` | Future questions, controls, rubric, and coaching | Future loop 05 |
| `demo/interview/rumik.py` | Guarded Rumik TTS and stale-audio rejection | Implemented loop 07 |
| `demo/tests/test_contracts.py` | Focused deterministic contract tests | Landed foundation |
| `demo/evals/fixtures/` | Future delayed, duplicate, empty-final, interruption, and reconnect fixtures | Future loops 02–05 |
| `demo/evals/scenarios/` | Future audio behavioral scenarios | Future loop 10 |

Tests import `demo.interview` from the repository root via `demo/pytest.ini`; no
package installation or provider SDK is required. Dependencies are pinned in
`demo/requirements-dev.txt`. Downloaded model weights and caches belong in root `models/` per the owner’s
2026-09-14 instruction. Inspected source references remain in `demo/models/`.
Application implementation remains under `demo/`.

## Design acceptance mapping

| Design acceptance row | Required evidence | Owning loop |
| --- | --- | --- |
| No finish-speaking control | Browser/audio scenario completes multiple questions without a submit control. | 08, 10 |
| Hesitation | 0.5–2.0 second pauses do not trigger interviewer audio. | 04, 10 |
| Long answer | 90–180 seconds remains one accepted answer. | 03, 05, 10 |
| Incomplete thought | No substantive follow-up until continuation or check-in. | 04, 05, 10 |
| Delayed final | Dispatch waits for all closed-segment outcomes; missing final recovers. | 02, 03, 09, 10 |
| Duplicate/out-of-order final | Each accepted segment appears once and in order. | 02, 03, 10 |
| Empty final | Segment resolves without deadlock or fabricated answer. | 02, 03, 10 |
| Resume during generation | Old reply invalidates while continuation and delayed finals remain attached to the turn. | 03, 04, 10 |
| Barge-in during playback | Stale audio is rejected and audible stop is measured. | 04, 07, 08, 10 |
| Invalid completion response | Malformed signals cannot reach TTS or advance state. | 04, 05, 10 |
| Check-in while thinking | Check-in preserves pending answer and does not grade it. | 04, 05, 10 |
| Echo and noise | Speaker, fan, and keyboard conditions do not seize the turn repeatedly. | 08, 10 |
| Voice controls | Repeat, skip, thinking-time, and end requests transition correctly. | 05, 08, 10 |
| Provider failure/reconnect | Replacement STT session preserves accepted state and rejects old events. | 02, 09, 10 |
| Language coverage | Selected languages/code-switch cases meet a defined transcription/pronunciation rubric. | 05, 10 |

The design's initial targets are acceptance targets only: p95 audible bot stop within
500 ms of candidate speech-start and p95 first audible reply within 5 seconds of a
clearly finished answer under single-session prototype load. No target is measured
by this foundation loop.

## Verification boundary

Foundation tests may use deterministic fake provider events and should exercise
validation, identity, ownership, and state invariants. They do not establish
Sarvam, Gemini, Rumik, GPU, browser, latency, license, or commercial-use facts.
Integration sessions must reverify current official provider documentation before
using live credentials; audio acceptance requires the real audio-mode eval flow.

## Sarvam event boundary

[Loop 02 event contracts](sarvam-events.md) define the application service subclass.
Manual local boundary IDs and provider utterance IDs remain separate; explicit
binding preserves the captured candidate owner, including delayed and empty finals.
Loop 03 still owns transcript ordering and readiness before Gemini dispatch.

## Transcript admission implementation

The pending transcript ledger and provider admission contract are documented in
[transcript gate](transcript-gate.md). Canonical history receives no pending answer
commit from aggregation. Inference uses a copied, complete ledger snapshot, with
response invalidation independent of candidate ownership. Answer acceptance and
question progression remain controller responsibilities in loop 05.

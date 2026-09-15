# Interview controller

This document defines loop 05’s application contract for question progression,
voice controls, and coaching. It builds on the [transcript gate](transcript-gate.md)
and [turn policy](turn-policy.md). Provider, browser, TTS, and live audio behavior
remain unverified.

`InterviewSession` composes the turn coordinator, transcript ledger, reply guard,
provider boundary, and an `InterviewController` state machine. The controller owns
interview state transitions. It starts with an introduction,
asks one configured question at a time, permits at most one relevant follow-up for
an accepted answer, and closes when the configured duration or an explicit end
request is reached. Answer-driven progression requires a full transcript to pass
the reply guard and the controller to commit the accepted answer; skip and end
are explicit control transitions. Invalid
model output, provider errors, and timeouts preserve the current question.

## State and controls

The session composes state from `demo/interview/contracts.py`, the transcript
ledger, the turn policy, and the guarded Google service. Its state
includes the current question and index, interview phase, pending versus accepted
answer, candidate turn, response token, elapsed duration, and rubric evidence.
Response tokens and candidate ownership remain independent: interrupting output
does not discard delayed finals for the pending candidate answer.

Voice controls are exact application intents recognized from candidate speech:

| Intent | State effect |
| --- | --- |
| Repeat the question | Replays the current question or follow-up through the reply guard; it does not create evidence or advance state. |
| Skip this question | Closes the current question without grading its pending answer and moves to the next configured question. |
| Give me a moment | Calls the turn policy’s explicit thinking request and preserves pending segments. |
| End the interview | Stops new questioning, cancels pending work, and routes one closing reply through the guard. |

Controls are control-only turns. Their transcript is excluded from rubric evidence,
accepted-answer text, and coaching context. A control received while output is
pending first invalidates that output, then applies the intent. Voice-intent
recognition and its ambiguity policy are controller work; this contract does not
claim a speech recognizer is implemented.

## Replies and coaching

All spoken text passes through the existing reply guard after the controller has
issued an authorization. The guard validates the complete marker, response token,
dispatch, transcript revision, reply kind, and current ownership before forwarding
frames. “Frame forwarded” means the application authorized output; it is not an
audible playback acknowledgement.

Internal coaching data is separate from spoken text. Structured evidence references
prior accepted answers plus the current finalized staged answer, and records
uncertainty; it is never sent directly to TTS. The current staged answer remains
available after its matching guard authorization until the guarded output is
released. Only then does the controller commit the answer and evidence. Accent,
hesitation, and transcription failures are not evidence of competence. Evidence
competencies must match the active question's rubric. Empty or
pending answers cannot be scored.

Deterministic prompts for introduction, repeat, thinking acknowledgment, skip, and
closing pass through the reply guard. Timed `CHECK_IN` requests use Gemini's
completion decision. A complete decision releases a fixed neutral invitation;
model-authored check-in text and internal data are not spoken. Check-ins preserve
the pending answer and cannot accept, grade, or advance it. An empty ledger does
not authorize a Gemini response. With no candidate speech, the idle deadline emits
a notification only; it does not produce a spoken check-in.

## Duration and shutdown

The controller owns the interview duration deadline. At expiry it prevents further
question dispatch, invalidates response tokens, cancels managed provider work, and
routes at most one closing response through the guard. A state transition occurs
after the matching guarded frame is forwarded to the downstream release boundary;
that is an application release signal, not confirmation that audio was heard. End and cancel follow the
same admission-first teardown ordering described in the [turn policy](turn-policy.md):
close dispatch, cancel timers and provider tasks, then release pipeline resources.

## Gemini configuration

The application uses the text model `gemini-3.8-flash` with `thinking_level="low"`
explicitly. The current Google model reference was verified on 12 September 2026:
[Gemini 3.8 Flash model documentation](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash).
The service keeps realtime audio mode at its default and does not let model tools
mutate interview state before reply validation.

## Acceptance boundary

Deterministic controller tests must prove one-question-at-a-time progression,
repeat/skip/thinking/end transitions while output is pending, duration closure,
control exclusion from evidence, grounded coaching, and preservation of state after
invalid replies. These checks do not establish browser playback, TTS quality,
provider latency, or live model behavior.

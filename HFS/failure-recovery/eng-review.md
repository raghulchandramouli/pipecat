# Engineering review: failure recovery and Sarvam experiment

Date: 2026-09-16. Mode: FULL_REVIEW, after CEO and DX amendments. Independent reviewer: Codex. Claude unavailable; cross-model consensus is N/A. This reviews a proposed implementation, not a completed HF adapter. Only this report and its test plan are authored by this reviewer.

## Scope challenge and reuse

The objective needs a diagnostic sequence and a conditional experiment, not a second interview implementation. Two new provider handlers plus their tests trigger the complexity check, but their separate STT and TTS protocols justify the split. Auto-decision: retain the bounded experiment and stage one handler at a time; do not add a generic compatibility gateway unless the native seam fails its feasibility gate. This follows explicit interfaces and reuse, without reducing failure coverage.

| Sub-problem | Existing code examined | Action |
|---|---|---|
| Provider TTS and cancellation | `demo/interview/sarvam_tts.py`, `test_sarvam_tts.py` | Reuse wire settings and failure fixtures, not Pipecat frame types in HF |
| STT interval ownership | `browser_stt.py`, `test_browser_stt.py`, `test_interaction_repair.py` | Preserve semantics; translate to HF identities in an isolated adapter |
| Empty finals and repair | `test_ledger.py`, actual STT callback integration in `test_interaction_repair.py` | Use as contract examples; never treat generic recognition as interview parity |
| Reply admission | `test_reply_guard.py` | Keep fail-closed admission in the demo; migration remains separate |
| Provider evidence | `probe_sarvam_realtime.py`, existing smoke/probe commands in DX report | Extend input/output handling instead of duplicating probes |
| HF audio conversion | pinned `TTS/openai_compatible_handler.py:247,634,704` | Reuse stateful FIR conversion and block assembly [Layer 1] |
| HF lifecycle | pinned `STT/openai_compatible_handler.py`, `pipeline/messages.py`, TTS handler | Reuse session, revision and cancellation ownership [Layer 1] |

`demo/TODOS.md` already tracks restoration, broader audio acceptance, follow-up timing and accepted-answer edits. Restoration blocks a migration claim, not the minimal voice experiment. Distribution as a package or public service is not required for this local source checkout; its README and pinned installation are the delivery mechanism.

## 1. Architecture

```text
Existing demo                                       Proposed isolated experiment
browser mic -> Pipecat VAD                          same recording/device conditions
  -> Sarvam manual STT -> transcript ledger           -> HF VAD -> Sarvam STT adapter
  -> controller -> Gemini -> reply guard              -> matched LLM configuration
  -> Sarvam PCM24k -> browser playback                -> Sarvam TTS adapter
                                                        -> stateful PCM24k-to-16k
Provider-only probes --------------------------------> HF output -> audible capture
          \                                           /
           +---- allowlisted per-run evidence --------+
                          |
               eligibility / comparability gate
                          |
           keep demo / experimental baseline / no result
```

No runtime import should cross from the HF environment into `demo.interview`. Pinned upstream classes supply the lifecycle, while sibling adapters translate Sarvam requests and events. The only shared comparison input is a recording plus declared configuration; credentials remain server-side and output manifests remain allowlisted. Loopback binding, ignored caches/results and explicit process-local environments avoid introducing a public service or altering demo dependencies.

**E1 [P1] (confidence 9/10): specify the complete audio contract.** `plan.md:92` says “explicitly resample once”; `sarvam_tts.py:125` requests `"speech_sample_rate": 24000` and line 166 emits a 24k mono frame. Pinned HF `_decode_pcm_stream` decodes `dtype="<i2"`, and `_resample_to_blocks` targets 16k with final block padding. Adopt raw little-endian signed PCM16 mono at 24k on the provider edge; retain a trailing byte across chunks; reject an odd total, empty response, wrong content type or container signature. Reuse one stateful resampler per response, emit HF int16 blocks at its configured block size, flush only on successful completion, and discard pending samples on cancellation. Report meaningful audio samples separately from final zero padding; compare duration within one output sample before padding, and at most one block of padding. Do not resample per network chunk or reinterpret 24k bytes as 16k.

**E2 [P1] (confidence 9/10): define empty, late and conflicting finals.** `plan.md:92` requires identity preservation but does not enumerate final outcomes. `browser_stt.py:175-195` matches successfully sent intervals and rejects identity conflicts; `test_browser_stt.py:190` proves whitespace final is terminal without a `TranscriptionFrame`. Proposed HF STT must map each request or manual interval to session generation, turn ID and revision before sending audio. A blank final resolves that owned operation once and produces no LLM input; partials never advance state. Duplicate identical finals are idempotent; conflicting, ambiguous or unbound finals produce a typed sanitized failure and block admission. Cancelled, superseded or previous-session finals are discarded, including a completion paused immediately before publication. Reconnect creates a new generation; it cannot repair trust by assigning an old final to the next turn.

**E3 [P1] (confidence 9/10): require cancellation to release transport and preserve terminal ownership.** `plan.md:53` asks for “cancellation followed by a successful next turn.” HF TTS `process` checks `cancel_generation`, `response_key`, `turn_id` and `turn_revision`, while its HTTP operation can cancel a stalled read. Preserve all four identifiers, cancel before dispatch and before each publication, close active HTTP/WebSocket work on interrupt/end, discard buffered PCM, and retain upstream keyed terminal cleanup behavior. A cancelled generation must not publish audio, transcript, success or failure into a newer response. Verify actual socket closure and a fresh successful response; merely filtering emitted audio is insufficient. Implement bounded teardown and fail the trial if a worker/connection survives it.

## 2. Code quality

The current demo already separates provider transport from reply admission, but Sarvam inherits the Rumik admission class and `_Request`. That is a reuse seam inside the demo, not a portable HF handler. Copying that inheritance into HF would import Pipecat lifecycle assumptions and unnecessary dependencies. Keep Sarvam payload construction small, explicit and independently tested; preserve the pinned upstream implementation rather than copying its entire decoder or queue machinery.

**E4 [P2] (confidence 9/10): evidence must survive failures before network setup.** `plan.md:107` requires every failure to return nonzero with an artifact path; `probe_sarvam_realtime.py:27` opens the fixed WAV before connecting and writes its report only after the connection block. Extend the existing probes with validated input, collision-resistant run directory creation and an outer failure-finalization path covering configuration, WAV reading, initialization, provider work and interruption. Create the manifest before attempting providers, update atomically, and if the output path cannot be written, report that explicitly to stderr with nonzero exit. Keep a small shared helper only for genuinely repeated manifest behavior; do not create a new diagnostic framework. Never include arbitrary exception text or HTTP response bodies in the allowlisted report.

No existing source diagram is changed by this documentation-only plan. Add the audio conversion and identity contract to the experiment README; a short ownership comment at the adapter publication boundary is useful, while duplicating this review as inline comments is not. `test-plan.md` names the behavior needed to protect the helper and provider translation instead of prescribing private function structure.

## 3. Tests

Pytest is authoritative in AGENTS.md; `demo/pytest.ini` sets import paths and async fixture scope. The parent run reports 24 passing tests across Sarvam TTS, browser STT and interaction repair, and 13 parsed interaction scenarios. Those results validate selected existing behavior only: no live provider call, physical microphone run, HF adapter or audible p95 was measured here.

The [test plan](test-plan.md) maps every proposed boundary and user flow to existing coverage or required tests, including error, empty and stale results. Existing HF tests cover chunk-invariant anti-aliasing, cancellation before headers, stale terminal cleanup and session reuse; they are source evidence and reusable fixtures, not proof that Sarvam adaptation inherits correctness. All proposed adapter paths remain implementation gaps until the isolated test suite runs against them.

**E5 [P1] (confidence 9/10): adapter qualification requires fault injection.** `plan.md:118` names “protocol, cancellation, format, isolation checks” without an executable branch matrix. Require the detailed matrix below before any winner or migration claim: corrupt/empty PCM, response ownership, empty STT, late finals, queue overflow, auth/timeout, cancelled socket and healthy next request. Tests must use the new Sarvam handler and actual pipeline publication seams, with loopback socket tests where mocks would conceal release failures. This is a new integration requirement, not a demonstrated regression in existing demo code.

No prompt changes are proposed. If diagnosis changes Gemini prompts, reply validation, pause rules or control behavior, rerun all applicable `demo/evals/interaction/` scenarios, existing reply-guard/controller tests and the live browser behavior checks against the recorded baseline. Parsing the YAML is not an eval pass. Language quality and naturalness still need human ratings after the deterministic suite passes.

## 4. Performance

There is no database or N+1 query path in this experiment. The risks are provider latency, queue accumulation, polling/cancellation latency and audio buffering. Existing Sarvam streaming caps output at 16 MiB; HF STT bounds pending final requests at eight, but those limits are reference behavior, not an acceptable substitute for measuring a new WebSocket adapter.

**E6 [P2] (confidence 8/10): bound work and expose overload.** `plan.md:53` requires a bounded feasibility spike, while HF STT defines `_MAX_PENDING_FINAL_REQUESTS = 8` and TTS reads streamed output. Adopt explicit total deadlines and pending-work limits in adapter configuration; reject overflow with a typed current-turn failure, never silently evict an admitted final. Bound accumulated provider bytes and input duration, cancel obsolete work promptly, and verify memory settles after repeated interrupt/retry cycles. Keep only model caches across runs; cache neither live synthesis nor transcripts in the latency sample set. Mark any warmup provider calls separately because upstream setup performs real warmups.

The <=5s answer-end-to-audible-reply target includes endpointing and playback, while provider time-to-first-byte excludes both. The <=500ms interrupt target must be measured at the output device, not at cancel dispatch. Report unsuccessful turns separately, retain the full denominator, and show cold/warm distributions plus sample count; 30 observations are an exploratory p95 estimate. A shared monotonic recording timeline or synchronized capture is required for cross-process timing; wall-clock subtraction between unrelated timestamps is not enough.

## Failure modes and critical gaps

| Boundary | Realistic failure | Existing handling/test evidence | Required handling and visible result | Gate |
|---|---|---|---|---|
| Probe setup | missing key, malformed WAV, unwritable directory | STT probe can fail before report | configuration artifact or explicit stderr, nonzero | before repeated trials |
| Provider auth | invalid/expired entitlement | demo TTS HTTP error test | no auth retries, sanitized stage/action | before integration |
| STT ownership | late final binds new speech | demo interval/repair tests | generation fence and typed correlation failure | critical adapter qualification |
| Empty STT | silent pending turn or empty LLM request | demo terminal-empty test | resolve once, no model call, observable empty outcome | critical adapter qualification |
| TTS format | rate/endianness mismatch, odd EOF | demo invalid PCM; HF decoder tests | reject, fail response, no false success | critical adapter qualification |
| Cancellation | stalled read outlives turn | demo and upstream socket/lifecycle tests | close transport, no stale publication, next turn works | critical adapter qualification |
| Backpressure | slow endpoint retains many utterances | HF queue-limit tests | bounded queue, typed overflow, healthy recovery | before extended trial |
| Evidence | interrupted trial disappears | fixed-path probe limitation | unique manifest, terminal outcome, raw count | before comparison |
| Playback | audio emitted but speaker silent | existing frontend playback readiness/retry tests | device check plus audible capture | before latency claim |
| Comparison | unmatched prompt wins unfairly | CEO comparison gate | mark confounded or NO QUALIFIED RESULT | before winner claim |
| Rollback | orphan process holds port | not established for future experiment | owned-process teardown, demo health and next session | before accepting experiment |

Four critical implementation boundaries are explicitly gated above: STT ownership, empty-final completion, PCM integrity and cancellation. After incorporating this report there are zero unplanned silent-failure gaps, but none of those future adapter gates is passed yet. Do not report the proposed implementation as validated or ready to deploy.

## Decisions and bounded implementation tasks

| ID | Decision | Class / principle | Rejected |
|---|---|---|---|
| E1 | Reuse stateful HF resampler with exact PCM contract | mechanical / DRY | per-chunk conversion or rate relabeling |
| E2 | Terminal empty outcome plus immutable generation ownership | mechanical / complete | next-response arrival-order binding |
| E3 | Cancel sockets and buffers, preserve terminal identity | mechanical / complete | output filtering alone |
| E4 | Outer manifest lifecycle and unique output directories | mechanical / explicit | happy-path-only artifacts |
| E5 | Fault-inject actual adapter publication boundaries | mechanical / complete | relying on upstream test names |
| E6 | Bounded queues/deadlines and audible measurement | mechanical / pragmatic | unlimited backlog or proxy latency |

| Task | Priority | Files / responsibility | Human / agent effort | Dependency |
|---|---|---|---|---|
| ENG-1 | P1 | existing probe scripts, focused probe tests: failure-safe evidence and input validation | 0.5–1 day / 1–2 hours | approved plan |
| ENG-2 | P1 | isolated adapters and contract tests: TTS payload, PCM and cancellation | 0.5–1 day / 2–4 hours | pinned HF baseline |
| ENG-3 | P1 | isolated STT adapter and lifecycle tests: manual boundaries, empty/late finals | 1–2 days / 3–6 hours | raw Sarvam STT pass; feasibility timebox still applies |
| ENG-4 | P1 | experiment tests and run report: combined round trip, socket abort, next turn, isolation | 0.5 day / 1–2 hours | ENG-2 and ENG-3 |
| ENG-5 | P2 | comparison evidence and device acceptance: matched recordings, audible latency, human ratings | 1–3 days scheduling / 1–2 hours analysis plus trials | ENG-4; human/device availability |

These are task estimates, not a promise to fit every task into the first spike. If the one-day feasibility budget expires, record what passed and stop at the documented blocker. Probe hardening and isolated environment preparation may run in separate worktrees; TTS and STT may follow independently after agreeing the identity contract. Combined lifecycle tests run after both and serially against owned ports. Changes to shared demo controller/ledger files require one owner.

## NOT in scope

- Migrating interview state, coaching or restoration into HF: separate parity design after the voice experiment qualifies.
- Publishing a package/service or deploying a second app: local source installation suffices for this diagnostic.
- Changing follow-up counts or pause policy to improve a benchmark: already deferred in `demo/TODOS.md`; it changes the workload.
- Refactoring the Rumik/Sarvam inheritance hierarchy: not necessary for the isolated adapter or observed failure diagnosis.

## Completion summary

| Item | Result |
|---|---|
| Architecture | 3 contract findings, resolved as proposed requirements E1–E3 |
| Code quality | 1 evidence-lifecycle finding, resolved in E4 |
| Tests | 1 integration coverage finding, full branch artifact required by E5 |
| Performance | 1 bounded-work finding, resolved in E6 |
| Unresolved engineering choices | 0 new taste choices; parent checkpoint/adapter preference gate remains |
| Critical gaps | 0 unplanned after amendment; 4 explicit unpassed implementation gates |
| Outside voices | This Codex review only; parent records separate CLI review; Claude unavailable |
| Validation | Parent reports 24 selected existing tests passed and 13 scenarios parsed; no live/HF/audio claims |
| Status | DONE_WITH_CONCERNS: plan requirements complete; implementation and device evidence pending |

Suppressed findings: no claim of an observed race or memory leak in the future adapter is made. Such code does not exist yet. The bounded-work requirements are preventive contracts grounded in the inspected upstream queues and transport lifecycle.

Durable learning: pinned HF TTS already includes an anti-aliased chunk-invariant 24k-to-16k resampler and keyed lifecycle tests; adapting only the request payload without preserving that lifecycle is insufficient.

## Final integration amendments

The parent engineering CLI identified four concerns: accepted-state versus audible-delivery ownership, cancellation after client buffering, streaming resampler continuity/reset, and a synchronized audible measurement oracle. E1/E3 and the test artifact cover conversion and transport cancellation; the following additions complete the user-facing boundary. This is a separate Codex review, not cross-model consensus.

Proposed delivery policy: preserve a valid accepted answer when synthesis or playback fails, mark the current question delivery unconfirmed, and retry the same authorized question ID without another progression transition. Do not roll back accepted evidence solely because TTS failed. Tests must cover failure before any speech, partial audible question, buffered client audio, repeated retry and stale playback acknowledgments. Old playback epochs cannot resume audio or confirm a newer question. This policy is a future implementation requirement, not a statement about current behavior.

The executable setup specification now uses `UV_PROJECT_ENVIRONMENT` pointing to the absolute isolated `.venv`. Because the inspected upstream has no `uv.lock`, its first sync is unfrozen; archive the generated lock and resolved environment before measurements. The supported local Mac baseline uses actual upstream selectors but different models, so it validates installation only and cannot establish an orchestration winner. The demo TTS smoke passes through the demo adapter/guard; if that layer is implicated, add a minimal direct Sarvam HTTP control before attributing failure to the provider.

Evidence is point-in-time. Other work is actively changing `/demo`; the parent captured test-time source evidence but did not enforce before/after tree stability. The 24 passes remain observations for that run, not proof of the final current tree. Recheck hashes and rerun affected tests before implementation decisions or final acceptance.

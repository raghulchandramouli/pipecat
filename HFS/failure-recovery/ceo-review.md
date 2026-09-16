# CEO review: demo failure recovery

Date: 2026-09-16. Mode: SELECTIVE EXPANSION. Reviewer: independent Codex agent. Claude voice unavailable; this report is not cross-model consensus. Reviewed the draft plan, current Sarvam transport and probe code, ledger/session ownership paths, test inventory, interaction baseline, and demo TODOs. This is a plan review, not a claim that tests or provider calls ran during review.

## 0A. Premise challenge

1. **The current demo is failing in a known way: partially supported.** The historical synthetic browser run passed while its caption-to-reply event deltas ranged from 11.2 to 19.9 seconds. Physical microphone and audible p95 were explicitly unmeasured. Establish an exact current reproduction before assigning fault to Pipecat, Sarvam, Gemini, microphone routing, or turn policy.
2. **A separate HF pipeline can isolate orchestration: conditionally valid.** An experiment provides useful evidence only when provider settings, input waveform, timing rules, and output measurement match. A different local speech model or prompt can establish that HF runs, but cannot establish why this demo fails.
3. **Sarvam keys are sufficient to run HF with Sarvam: unproven.** The draft correctly requires contract inspection and adapters. Credential validity, provider protocol compatibility, local hardware support, and interruption handling are four separate gates.
4. **A faster chatbot is a usable interview replacement: false unless parity is demonstrated.** Existing boundary ownership, accepted-answer provenance, question progression, repair, and stale-playback rejection are substantial product behavior. Preserve the user's separate-folder fallback direction while labeling its first result a voice experiment.
5. **Thirty trials establish production p95: insufficient as a broad reliability claim.** Thirty per condition is a useful exploratory minimum, but report sample count, failures, warm/cold status, and raw distribution. A small successful trial cannot establish rare failure rates or universal device support.

No request to override the user's stated direction is needed. The plan already treats HF as conditional and isolated. The checkpoint duration and adapter style remain taste decisions for the final gate.

## 0B. What already exists / leverage map

| Sub-problem | Existing implementation | Planned reuse |
|---|---|---|
| Prove a Sarvam key can synthesize | `demo/scripts/smoke_sarvam_tts.py` | Keep its language, speaker, timeout, output-dir CLI and source hash |
| Prove realtime manual boundaries | `demo/scripts/probe_sarvam_realtime.py` | Extend output isolation and input validation instead of a second probe |
| Correlate speech/finals | `demo/interview/browser_stt.py` | Preserve 16 kHz PCM and sent-audio interval semantics |
| Bound audio and cancel old output | `demo/interview/sarvam_tts.py` | Reuse behavior/tests as adapter requirements, not imports across environments |
| Preserve accepted answer ownership | `ledger.py`, `session.py`, `controller.py`, `reply_guard.py` | Existing demo remains the parity oracle |
| Check language/control behavior | `demo/evals/interaction/`, `check_interaction_acceptance.py` | Extend existing cases when diagnosis changes behavior |
| Restore disconnected sessions | `demo/TODOS.md`, loop 09 | Existing deferred work; do not claim already solved |
| Verify physical audio | `demo/TODOS.md`, loop 10 | Reuse device matrix and consented trials |

## 0C. Dream state and delta

```text
CURRENT                         THIS PLAN                         12-MONTH IDEAL
synthetic pass + uncertain  ->  reproducible layer evidence   ->   reliable multilingual interview
physical experience            focused repair or isolated HF      supported devices + restoration
one complex demo               matched comparison, no migration   provider swaps behind proven contracts
```

The valuable 10x improvement is shortening time from “it failed” to “this boundary failed with this evidence.” A second framework alone does not provide that improvement. This plan leaves physical acceptance, supported device coverage, and restored accepted state as explicit work rather than burying them behind a successful audio round-trip.

## 0C-bis. Alternatives

| Approach | Human / agent estimate | Benefit | Cost and risk | Decision |
|---|---|---|---|---|
| Diagnose demo, then conditional HF experiment | 1–3 days / 3–8 active hours plus trials | Preserves interview guarantees and produces causal evidence | Adapter effort and human trials remain uncertain | Recommend |
| Immediately build HF Sarvam experiment in parallel | 1–3 days / 4–10 active hours plus trials | Earlier independent audio baseline | Two changing systems can obscure diagnosis; doubles environment work | Viable taste alternative |
| Replace demo with HF immediately | Several days to weeks / unknown until contracts inspected | One eventual runtime | Must rebuild interview guarantees before equivalent product exists | Reject for current scope |

Estimates are planning ranges, not delivery promises; downloads, provider availability, hardware, and participant scheduling are outside agent execution time.

## 0D–0F. Scope and temporal interrogation

SELECTIVE EXPANSION is appropriate: keep diagnosis plus isolated comparison and accept only evidence quality improvements inside that scope. Add unique run directories, a bounded experiment admission manifest, explicit failed-trial accounting, and a same-provider comparison label. Do not expand into a provider abstraction framework or production migration.

| Window | Work and exit evidence |
|---|---|
| Hour 1 | Record dirty revision, symptom, device path, key presence without values, one bounded provider reproduction |
| Hours 2–3 | Trace first failed boundary; repair one causal issue with focused and neighboring checks |
| Hour 4 | Apply the proposed two-attempt/four-hour checkpoint; preserve evidence and decide experiment entry |
| Hours 5–6 | Inspect pinned HF contracts and establish supported-backend baseline before Sarvam substitutions |
| Hour 6+ | Add one Sarvam stage at a time; run matched trials only once output and cancellation work |

The clock bounds investigation, not correctness. If a direct provider probe fails, HF is not a rational credential repair; fix the provider/configuration failure first. The checkpoint must record attempted hypotheses and outcomes so two superficial retries do not satisfy it.

## 1. Architecture

The draft's isolation is sound because the existing demo and experiment share measurement inputs, not runtime state. `InterviewSarvamTTSService` currently inherits admission machinery from `RumikTTSService`; importing it wholesale into an HF environment would erase the independence of the comparison. Keep adapters small and preserve behavior through tests rather than cross-environment inheritance.

```text
EXISTING: browser -> WebRTC -> browser_stt -> ledger/session/controller
                                        -> Gemini -> reply_guard -> Sarvam TTS -> playback
NEW:      same recorded corpus -> pinned HF -> isolated Sarvam handlers -> experiment playback
          both runs ---------> allowlisted run manifest + matched evidence report
```

At 10x concurrent experimental work, API quota and duplicate device ownership are the first risks; at 100x this ceases to be a local experiment and needs a separate load/cost design. Admit one comparison session at a time initially. The API providers and local audio device remain single points of failure; switching framework does not remove them.

```text
planned -> provider-ready -> HF-baseline-ready -> one-adapter-ready -> both-ready -> compared
   |             |                  |                   |                 |
   +-------------+------------------+-------------------+-----------------> failed evidence
compared -> retain demo OR retain experimental baseline
compared -X-> automatic interview migration (separate parity decision required)
```

## 2. Error and rescue map

Current TTS code catches `httpx.HTTPError`, `TimeoutError`, and `ValueError`, handles non-200 HTTP status explicitly, rejects invalid audio, and closes obsolete streams. The STT probe can raise directly on absent environment keys, missing WAVs, invalid JSON, websocket failures, or timeouts; it writes evidence only after successful completion. The plan should require failure manifests in a `finally` path, with sanitized reasons and partial trial status, so a failed run is not mistaken for a missing run.

| Codepath | Failure / exception | Existing rescue | Required action and visible result |
|---|---|---|---|
| STT credential loading | Missing key / `KeyError` | No probe-specific rescue | Preflight presence; exit “SARVAM_API_KEY missing” without value |
| TTS credential loading | Blank key / `ValueError` | CLI catches | Preserve actionable configuration error |
| STT WAV loading | Missing/invalid file / `FileNotFoundError`, `wave.Error` | No probe-specific rescue | Validate path, channels, width, rate; label input failure |
| Provider handshake | Auth rejection / websocket handshake exception | No probe-specific rescue | No retries; record provider/status and fix key/account |
| HTTP TTS | HTTP 429/5xx | Error frame; no automatic retry | Bounded retries only when no stale/partial output can replay |
| Provider transport | `httpx.HTTPError`, websocket close, `TimeoutError` | TTS catches; STT propagates | Record deadline and stage; cancel work and emit failed trial |
| Provider event parsing | `JSONDecodeError`, invalid payload | STT propagates | Reject malformed event, close session, retain sanitized diagnostic |
| Audio decoding | Empty, odd-byte, container payload / `ValueError` | TTS catches | Fail stage; never count silence as completed speech |
| LLM reply | Empty, refusal, invalid completion marker | Reply guard rejects unauthorized output | Count rejected response separately; bounded authorized retry only |
| Output artifacts | `OSError`, existing run collision | Not fully specified | Exclusive unique run path; fail loudly rather than overwrite |
| Experiment cancellation | Task cancellation, late chunks | Existing demo epoch rejection | Contract tests for adapter and output queue; discard stale output |

## 3. Security and threat model

The experiment introduces downloaded code, provider credentials, and audio artifacts, but does not need a new public service. Keep new listeners on loopback and pass keys from the process environment; neither command arguments nor browser payloads should contain them. A pinned revision improves reproducibility but is not a dependency audit, so inspect setup hooks and avoid importing an unrestricted host environment into the experiment.

| Threat | Likelihood / impact | Mitigation |
|---|---|---|
| Keys in reports or command history | Medium / high | Key-presence status only; allowlist error fields; no headers/env dumps |
| Interview speech or transcripts committed | Medium / high | Synthetic inputs first; ignored run storage; deliberate retention/deletion policy for consented trials |
| Upstream dependency executes with unnecessary secrets | Medium / high | Inspect pinned setup, separate environment, minimum required exported credentials |
| Shared local service exposes experiment controls | Low / medium | Loopback binding, no forwarding/public deployment |
| Model output drives shell or filesystem actions | Low / high | Treat generated text solely as speech content; no new tools |

The current realtime probe allowlists `text`, which is still potentially personal information. “Allowlisted” is not equivalent to “non-sensitive”; retain real speech only under the trial's explicit retention rule.

## 4. Data flow and interaction edges

```text
recorded PCM -> validate format -> STT -> validated text -> LLM -> TTS PCM -> measured playback
missing     -> fail preflight
empty       -> explicit silence case, no answer acceptance
invalid     -> fail stage, preserve failed-trial manifest
timeout     -> cancel request, discard stale generation, count failure
duplicate   -> retain event identity; never advance question twice
partial     -> mark incomplete playback; never score as full success
```

The probe hardcodes 24 kHz mono input conversion and reads a fixed Hinglish smoke WAV, so arbitrary recordings require validation before reuse. Device replay must maintain realtime pacing; sending a recording as fast as possible changes endpointing behavior and makes latency incomparable. Double launch, stop during generation, output device change, and retry while an old stream is stalled need explicit per-run statuses and cleanup checks.

## 5. Code quality

Reuse the existing smoke CLI rather than build a universal diagnostics framework. `smoke_sarvam_tts.py` already supports `--output-dir` and guards source mutation during a run; the STT probe's fixed filenames and mixed fixture/setup responsibilities are the immediate quality gap. Add the smallest explicit input/output CLI needed by repeated comparisons and document which functions are demo-backed versus raw provider probes.

The HF adapter should not duplicate the interview controller or ledger. Two small native handlers are preferable if the pinned contracts cover required streaming and cancellation; a loopback bridge is reasonable only when it reduces protocol work without hiding buffer ownership. No new adapter implementation was supplied, so method branching and naming must be reviewed when its contracts are concrete.

## 6. Test review

Existing tests cover PCM streaming and HTTP recovery, stalled-stream interruption, malformed PCM, sent-audio interval ownership, pre-roll, reverse finals, empty finals, conflicting IDs, ambiguous timestamps, playback epochs, and source provenance. Those tests are valuable specifications, but passing them cannot prove a physical speaker stopped or a participant found the voice natural. Keep deterministic adapter tests separate from live provider and physical-device acceptance.

| New flow/codepath | Happy-path check | Failure/edge check | Coverage decision |
|---|---|---|---|
| Unique run and manifest | Two runs produce distinct complete reports | Collision, disk failure, interrupted run | Add focused filesystem tests |
| Probe input CLI | Valid mono waveform and output path | Missing, stereo, unsupported width/rate, empty | Add validation tests |
| HF setup | Pinned supported backend starts | Unsupported hardware/dependency or missing key | Document clean-environment smoke and actionable failure |
| Sarvam STT adapter | Correct text and timing metadata | Empty/reordered final, disconnect, duplicate, cancellation | Add contract unit/integration tests |
| Sarvam TTS adapter | Correct PCM format and complete stream | 401, 429, partial stream, odd chunks, stale output | Add contract unit/integration tests |
| Adapter combination | Same fixture produces audible reply | Barge-in during stalled stream, next turn progresses | Run realtime system check |
| Comparative report | Matched fields and failed-trial count | Mismatched prompt/model, no audible measurement | Reject parity label for mismatched evidence |
| Demo repair | Existing targeted regression passes | Neighboring interruption/recovery/provenance fails | Run affected suite before broader smoke |

The Friday-night confidence check is interrupting a stalled output stream, then successfully completing the next turn without old audio or question advancement. Hostile QA should deliver duplicate and late STT finals after cancellation. The chaos check disconnects the provider between final recognition and playback and verifies the run remains failed/incomplete, with no automatic accepted-state migration. Any prompt or language-policy change requires the existing English, Tanglish, and Hinglish interaction scenarios, not only a text snapshot.

## 7. Performance

Measure the three likely slow regions separately: answer endpointing plus STT finalization, Gemini completion/validation, and TTS/network/playback buffering. Their p99 values are unknown; the historical caption deltas cannot supply them. The fixed 2.5-second pause floor in existing TODO context consumes a material share of the proposed five-second audible budget, but changing it is a separate product experiment because premature responses damage answer capture.

Existing TTS caps response bytes at 16 MiB and has a 30-second request deadline. New adapters need finite queue/audio limits and cancellation deadlines too; a timeout that leaves buffered playback draining is not an interruption guarantee. No database or query path is introduced, so N+1/index analysis found no applicable new risk. Do not cache measured provider outputs in comparison trials; report model warmup and cold starts separately.

## 8. Observability and debuggability

Use a run ID, source revision/hash, fixture hash, provider/model/voice/language settings, device route, monotonic stage timestamps, status, and sanitized error category. Tie all stage events to a logical turn ID so multiple partial/final captions do not inflate the sample count. The current TTS metrics explicitly exclude client playback, and the baseline labels server-speaking events as insufficient proof of audibility; preserve those distinctions in the report schema.

For this local experiment an actionable console failure and retained JSON summary are enough; an alerting service or dashboard would add unrelated infrastructure. Each failed row should identify the first failed boundary and next diagnostic command. An absent measurement must stay `NOT MEASURED`, never zero, and failed attempts must remain in the denominator.

## 9. Rollout and rollback

```text
capture source -> create isolated checkout/env -> provider gate -> HF baseline
 -> TTS only -> STT only -> both -> matched trials -> explicit retain/migrate decision

experiment fails -> stop experiment processes -> release ports/audio -> restart existing demo
                 -> existing demo smoke -> retain failed-run evidence
```

No database migration or user traffic rollout is introduced. Keep environments and model caches separate, use distinct ports, and stop one physical-audio process before starting the other. The first five minutes verify startup, key presence, correct PCM shape, and clean shutdown; the first hour verifies an interrupted turn and subsequent healthy turn. Rollback is operationally small but must verify port/audio release, not merely terminate a parent shell.

## 10. Long-term trajectory

The six-month regret would be maintaining two incomplete interview products because a fast HF audio demo was mistaken for equivalent behavior. Give the experiment an owner and disposition at the comparison checkpoint: retained measurement baseline, archived experiment, or separately proposed migration. Reversibility is 5/5 while no application imports, lock changes, or accepted-state transfer are introduced; it drops sharply once product state is shared.

The useful platform asset is the corpus and evidence contract, which can compare future providers without a rewrite. Phase two is physical acceptance and accepted-state restoration already tracked in TODOs, not further framework shopping. A future engineer should be able to read one setup/run/report document and distinguish experimental claims from supported-product claims.

## Failure modes registry

| Codepath | Failure | Rescued? | Test? | User sees | Logged? |
|---|---|---|---|---|---|
| Existing TTS | Stalled stream | Yes, cancellation | Existing | Stop then next turn | Error/metrics |
| Existing STT ownership | Ambiguous or late final | Reject/recovery | Existing | Recovery state | Correlation event |
| STT probe | Exception before report write | No complete report | Gap | CLI traceback | Partial stdout only |
| Comparison | Failed runs omitted | Planned rule needed | New report test | Misleading success rate otherwise | New manifest |
| HF adapter | Old PCM after stop | Contract unverified | Required new test | Stale speech | Required generation trace |
| Physical output | Server says speaking but no sound | Not inferable from server | Required device check | Silence | Must record audible evidence |
| Migration | Lost accepted state | Explicitly deferred | Existing TODO | Unsupported migration blocked | Decision record |

Critical gap before accepting an experimental result: cancellation and failed-run accounting must be demonstrated. No silent production failure is newly introduced by this documentation-only plan; unresolved gaps are implementation admission criteria.

## Decision audit

| ID | Decision | Class | Principle | Rejected alternative |
|---|---|---|---|---|
| CEO-1 | Preserve conditional isolated HF experiment | Mechanical | Explicit, pragmatic | Immediate replacement |
| CEO-2 | Require stage-level matching and failed-run denominator | Mechanical | Completeness | Compare selected successes |
| CEO-3 | Add unique run paths and failure manifest | Mechanical | Completeness, DRY | Duplicate probe framework |
| CEO-4 | Prefer two-attempt/four-hour checkpoint | Taste | Bias to action | Unlimited diagnosis or immediate fork |
| CEO-5 | Keep restoration and broad device support in existing TODOs | Mechanical | Boil lakes | Claim voice baseline is full parity |
| CEO-6 | Prefer native handlers subject to contract inspection | Taste | Explicit | Default compatibility bridge |

## NOT in scope

- Replacing the interview state machine or accepting automatic HF migration: requires product parity and separate decision.
- New provider abstraction, public deployment, multi-user scaling, or dashboard service: not needed to diagnose this local demo.
- Changing follow-up counts or pause floors: existing evidence-led experiments, not a framework comparison control.
- Completing accepted-state restoration within this experiment: existing P1 work with separate ownership and failure-injection requirements.

## Implementation tasks

- [ ] **CEO-T1, P1, human 2–4h / agent 30–90m:** Extend the existing realtime probe with validated input, unique output directory, and sanitized failure manifest. Files: `demo/scripts/probe_sarvam_realtime.py`, focused probe tests. Verify missing input/key, malformed audio, timeout and two-run isolation.
- [ ] **CEO-T2, P1, human 2–4h / agent 30–90m:** Define matched-run evidence fields and explicit failed/unmeasured statuses. Files: experiment README/report schema once created. Verify turn identity and mismatch rejection with fixture reports.
- [ ] **CEO-T3, P1, human 1–3 days / agent 3–8h plus trials:** Implement adapters only after pinned HF baseline and contract verification. Files: isolated experiment handlers and tests. Verify stage-by-stage success, cancellation, and clean teardown before combined trials.

## Completion summary

| Dimension | Result |
|---|---|
| Premises | Five evaluated; no unconditional framework-blame or replacement claim accepted |
| Architecture | Isolated environments and shared measurement inputs; no runtime product-state coupling |
| Error/rescue | Ten planned failure categories; STT failure artifact handling is an observed gap |
| Security | Keys, speech retention, dependencies and local listeners evaluated |
| Data flow | Missing, empty, invalid, stale, duplicate and partial paths specified |
| Code quality | Reuse probes; avoid a general diagnostics framework |
| Tests | Existing specifications mapped; adapter and measurement gaps enumerated |
| Performance | Stage timing required; physical p95 remains unknown |
| Observability | Run/turn identity and failure denominator required |
| Rollout | Component gates and process/audio cleanup rollback defined |
| Trajectory | Reversible baseline experiment; separate migration admission |
| Design | No new UI scope; existing states are acceptance surfaces |
| Scope proposals | Two in-scope evidence improvements; broad migration/scaling deferred |
| Taste decisions | Checkpoint duration and native-handler preference |
| Review limitation | Codex independent review only; Claude unavailable; no tests/live probes run |
| Status | DONE_WITH_CONCERNS: plan viable with evidence/cancellation additions; engineering review still required |

Durable learning: realtime probe artifacts are written only on success, whereas the TTS smoke already supports an output directory and source hash. Repeated failure investigations need a failure manifest before their reports can support comparative claims.

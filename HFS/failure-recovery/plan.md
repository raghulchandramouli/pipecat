<!-- /autoplan restore point: /Users/raghul/.gstack/projects/raghulchandramouli-pipecat/main-autoplan-restore-20260916-demo-failures.md -->
# Demo failure recovery and isolated Sarvam comparison

Status: draft for autoplan review. Date: 2026-09-16. Baseline: main at 6b942c0eb plus existing uncommitted interaction work.

## Intended outcome

Identify why a voice interview fails, repair the failing layer, and keep a separate Hugging Face speech-to-speech experiment available when the demo orchestration remains the problem. Preserve the current demo and its pending changes. This plan does not authorize replacing its interview state machine with a generic chatbot.

## Proposed sequence

1. Capture revision plus dirty-source hashes and reproduce the exact failure. Separate offline tests, synthetic provider runs, real microphone checks, and human ratings. Existing interaction baseline reports final-caption-to-reply deltas of 11.2–19.9 seconds, but several captions may refer to one answer; these are not independent end-of-answer latency samples.
2. Validate credentials and provider components separately using existing Sarvam TTS and realtime STT probes. Keep credentials in the server environment; save only allowlisted evidence. Confirm Gemini separately from Sarvam. No retries for authentication failures; bound timeout/rate-limit retries.
3. Trace input audio -> VAD -> STT final and boundary ownership -> transcript gate -> Gemini -> reply validation -> TTS PCM -> actual playback. Repair the first failed boundary and rerun its regression plus neighboring cancellation/recovery checks.
4. If a same-provider direct probe passes but the full demo still fails after two reproducible focused repair attempts or a four-hour engineering investigation, run an isolated comparison. This is an investigation checkpoint, not proof that Pipecat is defective.
5. Prepare `demo/experiments/hf-speech-to-speech/` with a pinned upstream checkout in `upstream/`, its own virtual environment and model cache, adapters in a sibling directory, and ignored runtime output. Do not install into demo/.venv or change the root dependency lock. Inspect the pinned HF handler contracts first. Native Sarvam support is not listed in the current upstream supported-components table. Sarvam auth and wire formats require verified translation, not merely changing an OpenAI base URL.
6. Establish raw Sarvam STT and TTS success; then a minimal HF supported-backend baseline, then Sarvam TTS only, STT only, and finally both. Match Gemini model/prompt/settings where feasible; otherwise label the comparison confounded. Recorded utterance STT can provide a first audio round-trip, but does not demonstrate realtime endpointing or interruption parity.
7. Compare matched English, Tanglish, and Hinglish recordings and device conditions. Keep prompts, voices, provider settings, pause rules and session state explicit. Target p95 answer-end to audible reply <=5s and speech-start to audible stop <=500ms, at least 30 samples per language/condition. Require zero accidental question advances and human naturalness/control >=4/5 (owner plus two speakers per offered language). These are acceptance targets, not current measured performance.
8. Keep /demo if fixed; use HF as an experimental voice baseline if it performs better; propose migration separately only after question progression, evidence grounding, repair, controls, cancellation, privacy and restoration contracts pass parity tests. Rollback is stopping the isolated processes and restarting the existing demo.

## Evidence and reuse

- `demo/browser/results/interaction/baseline.json`: synthetic browser/provider pass; physical mic NOT RUN, audible p95 NOT MEASURED.
- `demo/browser/results/browser-smoke.json`, `browser-interruption.json`: historical successful synthetic browser runs.
- `demo/scripts/smoke_sarvam_tts.py`: provider-backed TTS smoke and WAV evidence.
- `demo/scripts/probe_sarvam_realtime.py`: manual boundaries and timestamp probes; current fixed output filenames need unique run directories before repeated comparison runs.
- `demo/interview/browser_stt.py`: 16kHz input, pre-roll, timestamp correlation.
- `demo/interview/sarvam_tts.py`: bounded HTTP PCM streaming and playback ownership.
- `demo/interview/ledger.py`, `session.py`, `controller.py`, `reply_guard.py`: interview guarantees.
- `demo/scripts/check_interaction_acceptance.py`, `demo/evals/interaction/`, `demo/tests/`: deterministic and behavioral checks.
- `demo/TODOS.md`: accepted-state restoration and broader audio acceptance already tracked.

## Scope

UI scope: no new screens, components or layouts. Existing browser states are acceptance surfaces only. Developer-facing scope: yes, experiment setup, adapter contracts, commands and evidence reports. Preserve /demo application behavior until a diagnosed fix is separately implemented.

## Sources

- https://github.com/huggingface/speech-to-speech (supported components, source install, realtime protocol; checked 2026-09-16).
- https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/realtime-streaming
- https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/streaming-api/http-stream

## Decisions pending review

Recommended experiment checkpoint: two focused repair attempts or four engineering hours. Recommended HF adapter style: isolated native handlers when the pinned contracts support cancellation; a loopback compatibility bridge is an alternative if it preserves streaming and avoids patching upstream.

## CEO review amendments

The full [CEO review](ceo-review.md) is part of this plan. Diagnosis and focused repairs are implementation tasks proposed by this plan; this planning run changes documentation only. At the investigation checkpoint, write a localized failure hypothesis and name which HF stage tests it. A browser-only failure routes to device/transport diagnosis; a provider outage routes to provider recovery, not framework replacement. If evidence remains inconclusive, the HF run is exploratory and cannot attribute fault.

Compare equivalent minimal voice pipelines before comparing interview applications. A mismatch in model, prompt, validation policy, endpointing, language, voice, input waveform or playback route disqualifies an orchestration-winner claim. Such a result may be retained as a configuration experiment only. Count logical turns, failed attempts, and progression opportunities separately; retain all raw measurements and report cold/warm runs separately. Thirty samples are an exploratory minimum, not a production reliability guarantee.

Time-box the initial HF feasibility spike to one engineering day (human roughly 1–2 days; agent roughly 4–8 active hours, excluding downloads and human trials). Minimum useful result: one verified Sarvam input-to-output round trip, cancellation followed by a successful next turn, and a repeatable command with safe evidence. Stop with a documented blocker if the pinned contracts require a large upstream rewrite. Full multilingual acceptance follows only after this gate passes.

Existing realtime probe output is written only on success and uses fixed filenames. First add validated WAV inputs, unique output directories, and a sanitized manifest for success, failure, timeout and interruption. Reuse the TTS smoke's output-dir convention. Do not make repeated comparison runs overwrite existing evidence.

### Independent voices

Claude runtime is unavailable on this host. One independent Codex agent supplied the full CEO report; a separate Codex CLI review returned five concerns: checkpoint hypothesis, equivalent workload, matching rules, measurement denominator, and bounded experiment effort. The amendments above address all five. This is same-model independent review, not Claude/Codex consensus.

| CEO dimension | Claude | Codex reviewers | Cross-model consensus |
|---|---|---|---|
| Premises | unavailable | conditional on localized evidence | N/A |
| Right problem | unavailable | diagnosis before framework judgment | N/A |
| Scope | unavailable | isolated bounded experiment | N/A |
| Alternatives | unavailable | repair / experiment / later migration | N/A |
| Competitive risk | unavailable | duplicate integration effort | N/A |
| Six-month trajectory | unavailable | avoid two incomplete interview apps | N/A |

## Decision audit trail

| ID | Phase | Decision | Class | Principle | Rationale / rejected |
|---|---|---|---|---|---|
| 1 | CEO | Preserve demo; conditional isolated HF experiment | Mechanical | Explicit | Keeps interview state; reject immediate replacement |
| 2 | CEO | Match stages and count failed trials | Mechanical | Complete | Prevents false framework attribution |
| 3 | CEO | Unique probe artifacts and failure manifests | Mechanical | DRY | Extend existing probes rather than duplicate |
| 4 | CEO | Two attempts / four-hour diagnosis checkpoint | Taste | Action | Prevents indefinite debugging; timing remains adjustable |
| 5 | CEO | One-day initial HF feasibility spike | Taste | Pragmatic | Prevents unbounded second integration project |
| 6 | CEO | Native handlers subject to pinned contracts | Taste | Explicit | Bridge remains viable if it preserves cancellation |
| 7 | CEO | No UI redesign | Mechanical | Pragmatic | Existing browser is a test surface; no new screens requested |

## Phase 2: design applicability

No new view, form, modal, component or layout is proposed. Browser permission, listening, recovery and playback states are existing acceptance surfaces. Design phase omitted on that evidence; developer setup and report usability are reviewed in DX.

## Pinned HF source inspection

Inspected upstream revision `16d7f98ff712fb082d53497937f5456667c84680` in a temporary read-only research checkout. No Sarvam Python implementation or documentation matches were found. The native experiment must explicitly wire handler selection; it cannot assume an unimplemented `--stt sarvam` or `--tts sarvam` flag.

HF's OpenAI-compatible STT uses bearer authentication, multipart WAV uploads and `/audio/transcriptions`; the demo's Sarvam realtime path uses subscription-key authentication and manual WebSocket events. HF TTS uses `/audio/speech` and an OpenAI-shaped payload, while the demo sends Sarvam-specific fields to `/text-to-speech/stream`. These are protocol differences, not key substitution.

HF's TTS handler has cancel-generation and speculative-turn checks, cancellable HTTP reads, and source-rate decoding into a 16kHz pipeline. The demo outputs Sarvam 24kHz mono PCM16. A native handler must preserve identity/cancellation semantics and explicitly resample once; verify channel count, endianness, frame alignment, chunk continuity and output duration. Existing cancellation code is a useful integration seam, not proof a new Sarvam adapter works.


## Comparison decision rules

A configuration is eligible only after its own functional and cancellation gates pass. If neither passes, report `NO QUALIFIED RESULT` and preserve both failure sets. If both pass, prefer the existing demo unless the matched comparison improves the diagnosed outcome without breaking interview behavior. If only HF voice tests pass, retain it as a working voice experiment; the interview remains unaccepted until its separate parity gates pass. Do not choose a winner from mismatched or missing audible measurements.

The first device matrix is quiet/headset and quiet/laptop-speakers, each in English, Tanglish and Hinglish. Add noise, long answers and reconnect cases from loops 09–10 before broader supported-environment claims. Run each offered-language matrix over at least three fresh sessions; 30 logical response turns per language/device condition is the exploratory p95 minimum. Independently count at least one instance of every progression/control/repair scenario per session; zero accidental advances means zero across those enumerated opportunities, not zero observed in an unspecified trial.

## DX amendments

The full [DX review](dx-review.md) and its copy-paste setup specification are part of this plan. Use Python 3.12 for existing demo probes because they use `audioop`; the proposed HF environment uses upstream's documented Python 3.11 source setup. Record resolved package versions and native platform prerequisites before the first baseline. In the HF process unset inherited `PYTHONPATH`, use explicit environment-local executables, reserve loopback port 8765, and retain demo ports 7860/7861. Do not set a Sarvam key as a default OpenAI credential.

Reuse `demo/scripts/probe_browser_reply.py` for the Gemini completion/coaching/quote contract. It already writes a failure outcome from its provider try/finally and exits nonzero on invalid evidence, but has a fixed output path and initialization failures outside that region; add the same output-dir and early-failure handling contract as the realtime probe. This supersedes the CLI reviewer's suggestion that no reusable Gemini probe existed.

Every probe command must report stage, outcome, sanitized category, corrective action and artifact path. Missing keys must produce a configuration error before network use; invalid audio must name the required format; 401/403 must say authentication/entitlement rather than retrying; 429 must give a bounded retry instruction; timeout must distinguish STT, LLM and TTS. All failures return nonzero. Never echo keys, full request headers or provider bodies.

Proposed implementation layout:

```text
demo/experiments/hf-speech-to-speech/
  README.md                 commands, prerequisites, stage gates, known limits
  upstream/                 ignored checkout pinned to inspected revision
  .venv/                    ignored isolated environment
  .cache/                   ignored model and package caches
  adapters/                 proposed Sarvam handlers and explicit integration
  tests/                    protocol, cancellation, format, isolation checks
  results/<run-id>/          ignored recordings and sanitized run manifests
```

A clean checkout reaching CLI help is the first developer result, target under five minutes on a prepared host with dependencies cached. Cold download and audible round-trip times are unmeasured and must not inherit that target. The final experiment README must include a tested combined command only after adapters exist; the plan deliberately makes no claim that stock HF can already select Sarvam.

| ID | Phase | Decision | Class | Principle | Rationale / rejected |
|---|---|---|---|---|---|
| 8 | DX | Reuse existing Gemini probe | Mechanical | DRY | Avoid duplicate live validation code |
| 9 | DX | Pin interpreters, revision, caches and ports | Mechanical | Explicit | Prevent demo/HF environment contamination |
| 10 | DX | Structured actionable failure output | Mechanical | Complete | A JSON file alone does not help the next command |
| 11 | DX | Distinguish existing commands from future interfaces | Mechanical | Explicit | No fictional Sarvam CLI selectors |
| 12 | DX | No winner when neither qualifies | Mechanical | Complete | Failed or incomparable evidence cannot justify migration |

### DX independent voices

Independent Codex agent plus Codex CLI; Claude unavailable. CLI findings: clean setup, runnable sequence, actionable errors, operational isolation (four concerns). Amendments and the DX report address each; the report scores plan completeness, not measured usability. Cross-model consensus is N/A for getting started, naming, errors, docs, upgrades and environment.

## Executable setup specification for implementation

The following setup is proposed and has not been run. It uses actual upstream source-install commands and supported baseline selectors; it downloads dependencies/models and produces a local-model baseline, not Sarvam parity. Run from the Pipecat repository root on the target Apple Silicon host with Git and uv available. An existing destination is a stop-and-inspect condition; never overwrite a checkout.

```bash
mkdir -p demo/experiments/hf-speech-to-speech
cd demo/experiments/hf-speech-to-speech
printf 'upstream/\n.venv/\n.cache/\nresults/\n.env\n' > .gitignore
git clone https://github.com/huggingface/speech-to-speech.git upstream
git -C upstream checkout --detach 16d7f98ff712fb082d53497937f5456667c84680
unset PYTHONPATH
export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
export UV_CACHE_DIR="$PWD/.cache/uv"
export HF_HOME="$PWD/.cache/huggingface"
export XDG_CACHE_HOME="$PWD/.cache"
uv sync --project upstream --python 3.11
.venv/bin/speech-to-speech local --help
uv pip freeze --python .venv/bin/python > dependencies-resolved.txt
```

Upstream has no `uv.lock` at the inspected revision. Archive the newly resolved upstream lock with the experiment metadata; use it for subsequent reinstalls. Inspect native dependencies and available memory/disk before selecting models. The documented Mac preset downloads several GB and is a separate baseline cost; do not repeat downloads for each trial.

```bash
.venv/bin/speech-to-speech local --mac-optimal-settings --model_name mlx-community/Qwen3-4B-Instruct-2507-4bit --port 8765
```

Expected result: startup/model warmup, microphone client connection, then a spoken response. Stop with Ctrl+C and verify the process releases its port and audio device. Grant microphone access only for this interactive test. Use headphones first, and do not enable microphone blocking during playback when testing interruption. This command uses local speech/LLM models, needs no Sarvam credential, and is not an orchestration comparison against the hosted demo.

Sarvam integration adds explicit locally implemented handler selection after protocol tests; its exact combined command is an implementation deliverable. Required fields: Sarvam STT model/language/mode, TTS model/voice/language/rate, compatible LLM transport/model/thinking, input/output devices, bounded timeouts, run directory and credential environment names. Gemini transport compatibility with HF must be tested separately. Unsupported combinations fail before audio capture or provider calls.

To return to the existing demo after stopping the experiment, open a clean shell at the Pipecat root and run:

```bash
PYTHONPATH=src demo/.venv/bin/python -m demo.interview.server --host 127.0.0.1 --port 7860
```

Validate one new interview and stop it cleanly; do not kill unrelated processes on occupied ports. The existing Sarvam TTS smoke exercises the demo guard and adapter, so it proves that path rather than being fully independent of Pipecat. Use a minimal direct HTTP request/fixture transport for a truly provider-only TTS control if the adapter itself is implicated; retain the same provider settings and output validation. The realtime STT probe is already direct WebSocket I/O.

## Final engineering contracts

The full [engineering review](eng-review.md) and [branch-by-branch test plan](test-plan.md) are part of this plan. Four final CLI findings are incorporated below; they are required implementation gates, not claims that the current app already satisfies them.

**Acceptance and delivery are separate.** Guard authorization can commit valid answer/progression state before speech becomes audible. Preserve legitimately accepted answer evidence when TTS fails, mark the current question's delivery unconfirmed, and allow a retry/repeat of that same authorized question ID without another advancement. Retry must use the current playback generation and must not replay obsolete speech after a control or interruption. If the existing UI cannot expose that recovery, specify a small follow-on interaction change before implementing it; no UI redesign is part of this experiment. Test no-header rejection, partial PCM failure and subsequent repeat against controller/ledger state.

**Cancellation reaches the physical output.** Test cancellation before response headers, during PCM and after transport buffering. Close the owned provider request, stop producing old-generation samples, clear/retire downstream playback, and prove a subsequent valid turn succeeds. Distinguish request abort from dropping output: both are required. Record task/thread teardown deadlines and assert no leaked worker or old audio after playback reattaches.

**Resampler state belongs to a response.** Reuse upstream's existing streaming conversion seam. Keep carry bytes, filter memory and final flush tied to response identity; maintain them across chunks of one response, reset them on cancellation or a new response, and never emit the cancelled tail. Verify identical sample output within numeric tolerance for different partitions of the same PCM input, output duration within one output sample where the selected converter permits, known-frequency preservation and no tail contamination. If the converter's filter delay needs a larger tolerance, derive and document it from that implementation before acceptance. Never concatenate separate responses just to maintain filter continuity.

**Audible measurements use one recording clock.** For physical trials capture a candidate-reference channel and bot-output channel in one synchronized multichannel recording at a known sample rate, retaining channel routing and calibration. In a headphone condition use a measured output loopback; for speaker acoustics retain an acoustic output recording and identify echo separately. A controlled fixture supplies the candidate answer-end and interrupt onset; inspect/annotate waveform boundaries, including a documented detector threshold and manual confirmation. Compute reply latency from candidate end to first audible bot sample; compute interruption latency from candidate interrupt onset to the last old-response audible bot sample. Record raw sample indices so analysis is reproducible. Server/browser timestamps are diagnostic only unless their clock offsets and uncertainty are calibrated. If this setup is unavailable, mark audible metrics `NOT MEASURED`.

A never-started reply, timed-out turn or unsuccessful interruption is a failed trial, not a latency of zero and not an omitted sample. Report failure rate and conditional successful-trial p95 together; no overall acceptance if any required functional/cancellation case fails. Include sample counts, raw distribution and the measurement noise floor. The 30-sample floor is exploratory and does not establish production reliability.

| ID | Phase | Decision | Class | Principle | Rationale / rejected |
|---|---|---|---|---|---|
| 13 | Eng | Preserve accepted evidence; track unconfirmed delivery | Mechanical | Complete | Speech failure must not erase answer or double-advance |
| 14 | Eng | End-to-end cancellation plus next-turn proof | Mechanical | Complete | Dropping frames alone does not silence buffered output |
| 15 | Eng | Reuse response-scoped streaming resampling | Mechanical | DRY | Avoid per-chunk resets and cancelled filter tails |
| 16 | Eng | Synchronized capture for audible targets | Mechanical | Explicit | Server speaking events are not acoustic measurements |

**STT finals resolve owned operations.** Bind manual boundaries to session generation, turn ID and revision before sending. Empty finals terminate once without sending empty input to the LLM. Identical duplicates are idempotent; conflicting or unbound finals fail closed; cancelled or old-generation finals cannot enter a new turn. Keep terminal cleanup keyed to the response identity even when a response fails.

**Bound queues and preserve failed runs.** Set explicit request deadlines, input duration/output byte limits and pending-operation limits. Overflow produces a typed failure, never silent eviction. Create each run manifest before initialization and atomically finalize it for every terminal outcome; an unwritable destination returns nonzero with an explicit stderr error. Required cancellation tests prove resources settle after repeated cycles.

| ID | Phase | Decision | Class | Principle | Rationale / rejected |
|---|---|---|---|---|---|
| 17 | Eng | Empty/late finals retain immutable ownership | Mechanical | Complete | No phantom input or reassignment after cancellation |
| 18 | Eng | Bound pending work and signal overflow | Mechanical | Explicit | Avoid unbounded memory or silent loss |
| 19 | Eng | Atomic manifest lifecycle begins before setup | Mechanical | Complete | Initialization failures remain visible |

### Engineering independent voices

Independent Codex agent plus Codex CLI, Claude unavailable. CLI raised four concerns: delivery recovery, cancellation scope, resampler lifecycle, audible oracle. The contracts above and test plan address each. Cross-model consensus is N/A for architecture, test coverage, performance, security, error handling and deployment; independent same-model review is the available evidence.

## Cross-phase themes

- CEO, DX and Eng: reproducible run identity and failure accounting are prerequisites for meaningful comparisons.
- CEO and Eng: a fast voice loop is not interview parity; keep accepted-answer and delivery semantics explicit.
- DX and Eng: environment isolation, cancellation and rollback require executable checks, not merely separate directories.

## NOT in scope

Immediate interview migration, public hosting, a universal provider abstraction, and a UI redesign are outside this plan. Conditional migration and adapter maintenance are in [TODOS.md](TODOS.md). Existing accepted-state restoration and physical device coverage remain in [demo/TODOS.md](../../TODOS.md). No root dependency or framework-source changes are proposed for the isolated experiment.

## Implementation tasks, aggregated across phases

Estimates are active engineering time; provider access, large downloads and human scheduling are additional. Each gate can stop with evidence rather than requiring speculative implementation of later steps.

| Priority / ID | Action and owned files | Exit evidence | Human / agent estimate |
|---|---|---|---|
| P1 / T1 | Reproduce failure; snapshot source and logical-turn measurements. Existing smoke/eval harnesses and new results run directory. | Exact symptom, first failed stage, no secrets, failures retained | 2–4h / 30–90m plus live run |
| P1 / T2 | Extend existing STT, TTS and Gemini probes with consistent input/output/timeout/failure contracts; focused tests. | Successful and failing probes produce unique safe manifests and correct exit status | 1–2 days / 2–5h |
| P1 / T3 | Fix diagnosed demo boundary only; update relevant interview module and regression. | Focused tests plus interruption, ownership, language and browser cases pass | Diagnosis-dependent; initial checkpoint 4h |
| P1 / T4 | At the conditional checkpoint prepare isolated HF environment, pin source/dependencies, prove supported baseline. Proposed experiment README, ignore rules and dependency record. | Known backend runs and clean teardown; root/demo environment unchanged | 0.5–1 day / 1–3h plus downloads |
| P1 / T5 | Add Sarvam TTS then STT handlers with explicit selection, protocol validation, response identity and cancellation; adapter tests. | PCM/empty/late/error tests plus cancelled request followed by successful next turn | Feasibility spike 1–2 days / 4–8h; reassess on expiry |
| P1 / T6 | Run matched language/device comparison and separate interview parity checks. Run manifests and comparison report. | Qualified result or explicit no-result; failures counted; audible evidence marked honestly | 1–3 days / 2–4h analysis plus human trials |
| P2 / T7 | Link final tested runbook from demo README; second-developer walkthrough and rollback rehearsal. | Repeatable commands and measured setup friction | 1–2h / 30–60m plus walkthrough |

Order: T1 → T2 → T3 → conditional T4 → T5 → T6 → T7. T2 strengthens the evidence before repeated live comparison; an obvious configuration fix can be diagnosed in T1. Within T5, TTS-only and STT-only stages each pass before the combined run. No simultaneous use of the physical audio device for competing baselines.

## Validation performed for this planning run

- 24 focused tests passed: Sarvam TTS streaming/cancellation, browser STT ownership and selective transcript repair.
- All 13 interaction scenario artifacts parsed. This checker did not run the scenarios against providers.
- Upstream source inspected at `16d7f98ff712fb082d53497937f5456667c84680`; no packages or models installed into the demo.
- Mermaid source rendered to SVG, PNG and a 51-element editable Excalidraw scene; PNG visually inspected.
- Live credentials, provider calls, real microphone, acoustic echo, audible p95 and migration parity: NOT RUN in this planning session.
- [evidence.json](evidence.json) records source hashes. Concurrent demo edits were observed during review, and source stability across the test run was not enforced. The 24-test result is a point-in-time observation, not certification of the final working tree. Recapture source identity and rerun required checks before live comparisons.

## Approval choices

Recommended: retain the two-attempt/four-hour diagnosis checkpoint, one-day initial HF feasibility spike, and native handlers only when the pinned contracts remain small and cancellable. Each is a reversible planning choice; the user may override timing or request the compatibility bridge. No challenge to the requested separate-folder experiment is proposed.

## GSTACK REVIEW REPORT

Status: DONE_WITH_CONCERNS, reviewed plan awaiting user approval; implementation not started.
CEO: SELECTIVE EXPANSION completed, all ten sections and registries in ceo-review.md; five CLI concerns incorporated.
Design: no new UI scope; existing browser states used for acceptance only.
DX: eight dimensions, 5.75/10 initial → 8.25/10 amended-plan completeness; current live setup time unmeasured. Four CLI concerns addressed, with the Gemini probe discovery correcting one premise.
Eng: final architecture, code quality, test and performance review in eng-review.md; execution matrix in test-plan.md. Four CLI concerns incorporated into required contracts.
Voices: independent Codex agents plus Codex CLI; Claude unavailable. Cross-model confirmed counts: N/A, not zero disagreements or unanimous consensus.
Decisions: 19 total, 16 mechanical recommendations and 3 taste choices; no user challenges. Full audit above, supporting per-phase decisions in reports.
Deferred work: migration and upstream maintenance in local TODOS.md; restoration and broad audio acceptance retain existing demo TODOs.
No approval logs or implementation-ready runtime claims are recorded before the user's final decision.

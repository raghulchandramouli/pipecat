# Failure recovery execution

Status: active. User authorized implementation with agent-loop on 2026-09-16.
Supervisor: Astra. Five implementation workers: four Terra, one Luna. Reviewers are independent Astra agents retained from planning. Full scope remains the plan plus test-plan T1–T15; physical/human gates cannot be replaced with synthetic results.

| Work | Owner | Dependencies | Status | Evidence |
|---|---|---|---|---|
| Safe STT/TTS/Gemini probes | Terra probe_implementation | existing demo env | running | T1/T2 fixtures required |
| Native HF Sarvam TTS | Terra hf_tts | pinned HF environment | running | T3–T6 |
| Native HF Sarvam STT/lifecycle | Terra hf_stt | pinned HF environment | running | T7–T10 |
| Isolated launcher/bootstrap/runbook | Terra hf_launcher | adapter setup contracts | running | T11 |
| Honest comparison evidence | Luna comparison_evidence | none | review | initial 3 tests; supervisor audit pending |
| Demo error ownership/delivery recovery | Astra root | current diagnosis | running | T13/T15 |
| Runtime qualification and full checks | Astra root | integration above | queued | no live success claimed yet |
| Device/human acceptance | owner + participants | qualified runtime | queued | no ratings or physical measurements yet |

## Baseline observations

Both SARVAM_API_KEY and GOOGLE_API_KEY are present (values not printed). Existing independent servers use ports7860 and7871; leave them running. Current complete offline demo run:351passed,2failed; one socket-bind failure passes when rerun with loopback permission, and the other is an optional Rumik engine import requirement in a test using an injected engine. No failures hidden as passes.

HF upstream copied from inspected checkout to HFS/upstream, revision16d7f98ff712fb082d53497937f5456667c84680. Isolated dependency installation is running; no root lock or demo environment change.

Historical handshake-failure.json is superseded by current successful artifacts. Actual remaining delivery ownership concerns are recorded in current-diagnosis.md.

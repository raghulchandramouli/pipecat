# TODOS

## Interview practice

### Connection restoration and retention

**What:** Complete existing loop 09 accepted-state restoration across connection generations.

**Why:** Users should retain accepted practice progress after a dropped connection.

**Context:** Loop 11 offers selective repair only while interval ownership remains trustworthy; otherwise it explicitly starts a new session. Preserve that limitation until restoration passes failure-injection and teardown tests. Start at `.loop/09-recovery-and-observability.md` and reconcile historical Rumik wording with the current Sarvam implementation. This adds lifecycle, retention, and generation-ownership complexity; coordinate shared session/ledger files with loop 11.

**Effort:** L
**Priority:** P1
**Depends on:** Existing browser pipeline; stable loop 11 ownership contracts if implemented first.

### Broader audio acceptance

**What:** Complete existing loop 10 microphone, headset, speaker, echo/noise, and latency checks.

**Why:** Supported-environment claims need audible measurements under recorded conditions.

**Context:** Loop 11's language interaction cases contribute evidence but do not complete the broader device/recovery matrix. Start at `.loop/10-audio-acceptance-and-handoff.md` and `demo/docs/browser-interview.md`. The cost is participant/device scheduling and output capture, not another test abstraction. Reconcile stale provider prerequisites with current Sarvam selection.

**Effort:** L
**Priority:** P1
**Depends on:** Loop 09 for restoration scenarios; consented physical audio setup.

### Evidence-led follow-up count and timing experiments

**What:** Evaluate optional follow-ups, explicit spoken completion, and adaptive pause timing as separate experiments.

**Why:** Each may improve usefulness or response time if the matched baseline reveals those failures.

**Context:** Current behavior uses one follow-up and a 2.5-second pause floor. Concise spoken wording is a separate loop 11 choice. Changing counts or timing adds progression branches and premature-response risk. Start from loop 11 language scenarios and ratings; preserve hands-free defaults and obtain an explicit product decision before changing them.

**Effort:** M
**Priority:** P2
**Depends on:** Matched interaction baseline and implemented loop 11 acceptance evidence.

### Accepted-answer editing and revision

**What:** Define transcript editing with explicit accepted-answer and evidence revision semantics if trials establish demand.

**Why:** Some transcription mistakes may survive repeat/repair and affect practice feedback.

**Context:** Editing an accepted answer differs from repairing a pending missing final. It needs revision history, evidence invalidation, and controller/coaching reconciliation. Start from ledger and coaching contracts; defer until human trials show that existing recovery cannot address the need.

**Effort:** L
**Priority:** P3
**Depends on:** Human interaction findings and a separate evidence-revision design.

# Voice interview interface

The interview uses a restrained monochrome layout with a central audio visual, inspired by [Grok's voice experience](https://x.ai/grok). It runs in the existing HTML/CSS/JavaScript client. System fonts and local assets keep the interface available without third-party font or animation requests.

Setup exposes interview language, conversation style, difficulty, and duration. Simple Tamil is the default; other languages, including Tanglish, remain selectable. Input language detection is automatic and speech uses a relaxed pace. Microphone permission, recovery, restart, and occupation-led prompts retain their protocol controls.

## Visual rules

- Dark surfaces use `#101010` and `#191919`; light surfaces use `#f9f9f6` and `#f0f0ed`. The system color preference selects the theme.
- Interactive buttons are pill shaped, inputs use an 8px radius, and containing surfaces use 12–18px radii.
- The active prompt and voice visual lead the conversation. Captions scroll inside their own keyboard-accessible region so new words do not move the page away from the speaker.
- The orb and frequency bars respond to locally measured microphone and remote playback audio. Silence settles the visual; there is no autonomous speaking animation. This shows audio activity, not emotion or speech understanding.
- Playback interruption advances the playback epoch and disables remote audio visualization until playback resumes. The native output gate drops provider audio while the user speaks and waits for a fresh response start before allowing audio again. Ending a session disconnects the analysis nodes, closes their audio context, and cancels the animation frame. The visualizer does not own or stop transport tracks.
- Reduced-motion preferences disable audio animation. A hidden document pauses drawing. Text states communicate listening, speaking, waiting, and errors independently of motion.

The visualizer in `web/voice-visual.js` connects audio sources only to analyser nodes, never to a speaker destination. It does not record or send additional audio.

## Checks

`demo/tests/test_voice_visual.py` exercises real Web Audio input/output signals, silence, interrupted output, reduced motion, cleanup, and transcript scrolling. The browser frontend tests cover setup payloads, permission races, playback epochs, recovery, and restart. `demo.scripts.smoke_gemini_conversation` checks live provider audio and conversation controls.

## Occupation-based question paths

The interviewer aims for 12 substantive questions, including the opening occupation question, within the selected time limit. After learning what the person does, it plans connected questions within that domain. It asks one at a time, updates the remaining path from each answer, and skips details already supplied. Changing occupation or requesting a new topic rebuilds the path.

For product engineers, the path can cover the product, their contribution, intended users, customer research and alternatives, design decisions, and feedback. It does not assume software or that the engineer personally owns market research. For carpenters, the path can cover an actual item, customer requirements, measurements, material choices, construction decisions, quality checks, and customer feedback. Other occupations receive equivalent paths based on the person's own description.

Difficulty changes the depth of the question, not the complexity of the language. Simple focuses on concrete activities, Standard on reasons and outcomes, and Detailed on tradeoffs and evidence. Unfamiliar terms receive a brief explanation grounded in the person's work.

## Reply timing

Native turn detection confirms silence after 0.2 seconds, then gives the speaker another 1.8 seconds to continue. Resumed speech cancels the pending turn end. This produces an approximately two-second silence window before the provider begins its response; generation and network delivery add to that delay. Speaking pace remains relaxed and is independent of this turn-end timing.

## Bounded round and ending

The planned round balances profession and daily work, career or life choices, and problem-solving in the person's domain. The target is 12 questions, with a 10–15 range when time allows. Repeat and explanation controls do not consume a new question slot. The selected duration takes precedence, so long answers can leave fewer questions completed.

Gemini calls `finish_interview` to complete the round, honour a spoken request to stop, or end for explicitly described personal harmful wrongdoing. The server supplies each new question’s number and required theme through `next_interview_question`. It rejects ordinary completion before twelve substantive interviewer turns and starts closing after the answer following the fifteenth turn as a fallback. This count assumes the instructed one-question-per-turn conversation format; clarifying conversation can differ from a literal question count.

Behaviour-ending guidance distinguishes personal harmful wrongdoing from reporting an incident, preventing misconduct, rejecting it, or discussing a hypothetical. Ambiguous attribution calls for clarification. The note describes the interview decision without declaring a legal offence or issuing a hiring verdict.

The normal time limit reserves its last 12 seconds for a short spoken note and still closes the connection at the deadline. Other model-requested endings wait for the closing transcript and transport playback to finish, with a 12-second cleanup fallback. Every end event also supplies a visible closing note. Pressing End stops immediately and leaves a written note; it does not force extra playback after the user stops.

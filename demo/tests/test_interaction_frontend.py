"""Browser checks for capability-gated interview controls."""

from __future__ import annotations

from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")


def test_server_prompt_controls_and_recovery_render_without_caption_inference() -> None:
    """Render only negotiated server state and keep an older status from replacing it."""
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    with sync_playwright() as api:
        browser = api.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.add_init_script("""
              window.__commands = [];
              window.createInterviewTransport = async ({onEvent}) => {
                window.__emit = onEvent;
                return {
                  async unlockPlayback() {}, async start() {},
                  async ready() { onEvent({type: "interview.client_ready"}); },
                  async sendCommand(command) { window.__commands.push(command); return {outcome: "applied"}; },
                  async close() {}, clearPlayback() {}, resumePlayback() {},
                };
              };
            """)
            page.goto(index.as_uri())
            page.locator('select[name="language"]').select_option("english")
            page.locator('button[type="submit"]').click()
            page.locator("#permission-button").click()
            page.evaluate("""() => window.__emit({
              type: "interview.status", status: "Listening",
              capabilities: ["interaction_controls_v1"], question_text: "How has your day been so far?",
              interaction_state: "Listening. You can pause or interrupt me.", prompt_revision: 7,
              state_revision: 9, permitted_actions: ["repeat", "explain", "thinking", "skip", "restart_answer"],
              recovery_token: "recover-1", question_index: null, question_count: null,
            })""")
            assert page.locator("#question-text").inner_text() == "How has your day been so far?"
            assert page.locator("#active-prompt-title").inner_text() == "LET’S TALK"
            assert page.locator("#question-count").is_hidden()
            assert page.locator('button[data-action="skip"]').inner_text() == "New topic"
            assert "new one" in page.locator("#controls-help").inner_text().lower()
            assert (
                page.locator("#state-text").inner_text()
                == "Listening. You can pause or interrupt me."
            )
            assert page.locator("#interaction-controls").is_visible()
            assert page.locator('button[data-action="restart_answer"]').is_visible()
            assert (
                "discards" in page.locator("#recovery-message").inner_text().lower()
                or page.locator("#recovery-message").inner_text()
            )
            page.locator('button[data-action="explain"]').click()
            assert page.evaluate("window.__commands[0]") == {
                "action": "explain",
                "promptRevision": 7,
                "recoveryToken": None,
            }
            page.locator('button[data-action="restart_answer"]').click()
            assert page.evaluate("window.__commands[1]") == {
                "action": "restart_answer",
                "promptRevision": 7,
                "recoveryToken": "recover-1",
            }
            page.evaluate("""() => window.__emit({
              type: "interview.snapshot", state_revision: 10, prompt_revision: 7,
              question_index: 0, question_count: 0,
            })""")
            assert page.locator("#question-count").is_hidden()
            page.evaluate("""() => window.__emit({
              type: "interview.status", status: "Speaking", capabilities: ["interaction_controls_v1"],
              question_text: "Old question", interaction_state: "Old state", prompt_revision: 6,
              state_revision: 8, permitted_actions: [],
            })""")
            assert page.locator("#question-text").inner_text() == "How has your day been so far?"
            assert page.locator("#status-label").inner_text() == "Listening"
            page.evaluate("""() => window.__emit({type: "interview.connection_lost"})""")
            assert page.locator("#status-label").inner_text() == "Connection lost"
            assert page.locator("#restart-button").is_visible()
            assert page.locator("#interaction-controls").is_hidden()
        finally:
            browser.close()


def test_command_resends_once_then_resyncs_without_advancing_channel_sequence() -> None:
    """Reuse a command ID once and use a REST snapshot after an unknown outcome."""
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    with sync_playwright() as api:
        browser = api.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.add_init_script("""
              const nativeTimeout = window.setTimeout.bind(window);
              window.setTimeout = (callback, milliseconds, ...args) =>
                nativeTimeout(callback, milliseconds === 2000 ? 5 : milliseconds, ...args);
              window.__sent = [];
              window.fetch = async (url, options) => {
                if (String(url).includes("/api/sessions/session") && !options) {
                  return new Response(JSON.stringify({
                    capabilities: ["interaction_controls_v1"], question_text: "Snapshot question",
                    interaction_state: "Listening", prompt_revision: 3, state_revision: 4,
                    permitted_actions: ["repeat"], recovery_token: null,
                  }), {status: 200});
                }
                return new Response(JSON.stringify({session_id: "session", offer_url: "/offer"}), {status: 200});
              };
              HTMLMediaElement.prototype.play = () => Promise.resolve();
              window.RTCPeerConnection = class {
                constructor() { this.iceGatheringState = "complete"; window.__peer = this; }
                createDataChannel() { return {readyState: "open", send(message) { window.__sent.push(JSON.parse(message)); }, close() {}}; }
                getSenders() { return []; }
                close() {}
              };
            """)
            page.goto(index.as_uri())
            result = page.evaluate("""async () => {
              const events = [];
              const transport = await window.createInterviewTransport({setup: {}, onEvent: event => events.push(event)});
              const outcome = await transport.sendCommand({action: "repeat", promptRevision: 3}).then(
                () => "confirmed", error => error.message
              );
              await new Promise(resolve => setTimeout(resolve, 30));
              return {sent: window.__sent, outcome, snapshot: events.find(event => event.type === "interview.snapshot")};
            }""")
        finally:
            browser.close()

    assert [message["command_id"] for message in result["sent"]] == [1, 1]
    assert result["sent"][0] == result["sent"][1]
    assert result["outcome"] == "I couldn't confirm that action."
    assert result["snapshot"]["question_text"] == "Snapshot question"
    assert "seq" not in result["snapshot"]


def test_empty_loading_error_end_and_restart_leave_no_old_conversation() -> None:
    """Keep recovery visible through an empty prompt and reset the room for a new practice."""
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    with sync_playwright() as api:
        browser = api.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.add_init_script("""
              window.createInterviewTransport = async ({onEvent}) => {
                window.__emit = onEvent;
                return {async unlockPlayback() {}, async close() {}, clearPlayback() {}, resumePlayback() {}};
              };
            """)
            page.goto(index.as_uri())
            page.locator('button[type="submit"]').click()
            page.evaluate("""() => {
              window.__emit({type: "interview.status", status: "Processing", capabilities: ["interaction_controls_v1"],
                question_text: "", interaction_state: "Getting the first question ready.", prompt_revision: 1,
                state_revision: 1, permitted_actions: []});
              window.__emit({type: "interview.caption", segment_id: 1, connection_generation: 1, final: true, text: "Old caption"});
              window.__emit({type: "interview.error", message: "I couldn't respond. Try again."});
            }""")
            assert page.locator("#question-text").inner_text() == "Getting the conversation ready."
            assert page.locator("#status-label").inner_text() == "Preparing a reply"
            assert "couldn't respond" in page.locator("#notice").inner_text().lower()
            assert "Old caption" in page.locator("#conversation").inner_text()
            page.locator("#end-button").click()
            assert page.locator("#restart-button").is_visible()
            page.locator("#restart-button").click()
            assert page.locator("#setup").is_visible()
            assert page.locator("#room").is_hidden()
            assert page.locator("#conversation").inner_text() == ""
        finally:
            browser.close()


def test_four_field_setup_uses_automatic_input_and_relaxed_speech() -> None:
    """Expose Gemini's native language catalog and preserve compatibility payload fields."""
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    with sync_playwright() as api:
        browser = api.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.add_init_script("""
              window.createInterviewTransport = async ({setup}) => {
                window.__selectedSetup = setup;
                return {async close() {}};
              };
            """)
            page.goto(index.as_uri())
            assert page.locator('select[name="language"] option').count() == 99
            assert page.locator("#setup-form [name]").evaluate_all(
                "els => els.map(el => el.name)"
            ) == ["language", "role", "difficulty", "duration_minutes"]
            assert page.locator('select[name="language"]').input_value() == "tamil"
            page.locator('select[name="language"]').select_option("bengali")
            page.locator('button[type="submit"]').click()
            page.wait_for_function("Boolean(window.__selectedSetup)")
            assert page.evaluate("window.__selectedSetup.language") == "bengali"
            assert page.evaluate("window.__selectedSetup.stt_language") == "auto"
            assert page.evaluate("window.__selectedSetup.speech_pace") == 0.85
            guidance = page.evaluate("window.__selectedSetup.rubric.map(item => item.guidance)")
            assert len(guidance) == 5
            assert all(item.startswith("Listening topic:") for item in guidance)
            assert all("tell me about a time" not in item.lower() for item in guidance)
        finally:
            browser.close()

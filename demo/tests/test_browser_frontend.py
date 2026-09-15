"""Browser-only client behavior that is independent of live providers."""

from __future__ import annotations

from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")


def test_stale_playback_events_do_not_unmute_interrupted_audio() -> None:
    """Keep client playback muted until a newer speaking epoch arrives."""
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    init = """
      window.fetch = async () => new Response(JSON.stringify({session_id: "session", offer_url: "/offer"}), {status: 200});
      window.AudioContext = class { async resume() {} async close() {} };
      window.__playCalls = 0;
      HTMLMediaElement.prototype.play = function() { window.__playCalls += 1; return Promise.resolve(); };
      window.RTCPeerConnection = class {
        constructor() { window.__peer = this; }
        createDataChannel() { return {readyState: "open", close() {}, send() {}}; }
        getSenders() { return []; }
        close() {}
      };
    """
    with sync_playwright() as api:
        try:
            browser = api.chromium.launch(headless=True)
        except Exception as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Playwright Chromium is not installed")
            raise
        try:
            page = browser.new_page()
            page.add_init_script(init)
            page.goto(index.as_uri())
            result = page.evaluate(
                """async () => {
                  const transport = await window.createInterviewTransport({setup: {}, onEvent() {}});
                  await transport.unlockPlayback();
                  window.__peer.ontrack({streams: [new MediaStream()]});
                  await Promise.resolve();
                  transport.clearPlayback(1);
                  transport.clearPlayback(4);
                  transport.clearPlayback(2);
                  transport.resumePlayback(3);
                  const afterStaleSpeaking = window.__playCalls;
                  transport.resumePlayback(5);
                  return {afterStaleSpeaking, afterNewSpeaking: window.__playCalls};
                }"""
            )
        finally:
            browser.close()

    assert result == {"afterStaleSpeaking": 2, "afterNewSpeaking": 3}


def test_end_during_connection_does_not_restore_a_closed_interview() -> None:
    """Keep a user-ended room closed when its pending audio start resolves."""
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    init = """
      window.__startEntered = false;
      window.__closeCalls = 0;
      window.__trackStopped = false;
      window.__start = new Promise(resolve => { window.__resolveStart = resolve; });
      navigator.mediaDevices.getUserMedia = async () => ({
        getTracks() { return [{stop() { window.__trackStopped = true; }}]; },
      });
      window.createInterviewTransport = async ({onEvent}) => {
        window.__emit = onEvent;
        return {
          async unlockPlayback() {},
          async start() { window.__startEntered = true; await window.__start; },
          async ready() {},
          async close() { window.__closeCalls += 1; },
          clearPlayback() {},
          resumePlayback() {},
        };
      };
    """
    with sync_playwright() as api:
        try:
            browser = api.chromium.launch(headless=True)
        except Exception as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Playwright Chromium is not installed")
            raise
        try:
            page = browser.new_page()
            page.add_init_script(init)
            page.goto(index.as_uri())
            page.locator('button[type="submit"]').click()
            page.locator("#permission-button").wait_for(state="visible")
            page.wait_for_function("!document.querySelector('#permission-button').disabled")
            page.locator("#permission-button").click()
            page.wait_for_function("window.__startEntered")
            page.locator("#end-button").click()
            page.wait_for_function("document.querySelector('#end-button').disabled")
            result = page.evaluate(
                """async () => {
                  window.__resolveStart();
                  await new Promise(resolve => setTimeout(resolve, 20));
                  window.__emit({v: 1, seq: 1, type: "interview.status", status: "Speaking"});
                  return {
                    status: document.querySelector('#status-label').textContent,
                    permissionDisabled: document.querySelector('#permission-button').disabled,
                    endDisabled: document.querySelector('#end-button').disabled,
                    permissionVisible: !document.querySelector('#permission').hidden,
                    trackStopped: window.__trackStopped,
                    closeCalls: window.__closeCalls,
                  };
                }"""
            )
        finally:
            browser.close()

    assert result == {
        "status": "Ended",
        "permissionDisabled": True,
        "endDisabled": True,
        "permissionVisible": True,
        "trackStopped": True,
        "closeCalls": 1,
    }


def test_end_during_microphone_permission_stops_the_late_stream() -> None:
    """Release a microphone returned after End without beginning negotiation."""
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    init = """
      window.__permissionRequested = false;
      window.__trackStopped = false;
      window.__startCalls = 0;
      window.__permission = new Promise(resolve => { window.__resolvePermission = resolve; });
      navigator.mediaDevices.getUserMedia = async () => {
        window.__permissionRequested = true;
        return window.__permission;
      };
      window.createInterviewTransport = async () => ({
        async unlockPlayback() {},
        async start() { window.__startCalls += 1; },
        async ready() {},
        async close() {},
        clearPlayback() {},
        resumePlayback() {},
      });
    """
    with sync_playwright() as api:
        try:
            browser = api.chromium.launch(headless=True)
        except Exception as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Playwright Chromium is not installed")
            raise
        try:
            page = browser.new_page()
            page.add_init_script(init)
            page.goto(index.as_uri())
            page.locator('button[type="submit"]').click()
            page.wait_for_function("!document.querySelector('#permission-button').disabled")
            page.locator("#permission-button").click()
            page.wait_for_function("window.__permissionRequested")
            page.locator("#end-button").click()
            result = page.evaluate(
                """async () => {
                  window.__resolvePermission({
                    getTracks() { return [{stop() { window.__trackStopped = true; }}]; },
                  });
                  await new Promise(resolve => setTimeout(resolve, 20));
                  return {
                    status: document.querySelector('#status-label').textContent,
                    startCalls: window.__startCalls,
                    trackStopped: window.__trackStopped,
                  };
                }"""
            )
        finally:
            browser.close()

    assert result == {"status": "Ended", "startCalls": 0, "trackStopped": True}


def test_data_channel_timeout_cancels_its_polling_timer() -> None:
    """Reject an unopened channel once without leaving its retry timer active."""
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    init = """
      const nativeTimeout = window.setTimeout.bind(window);
      window.__waitTimerRuns = 0;
      window.setTimeout = (callback, milliseconds, ...args) => nativeTimeout(() => {
        window.__waitTimerRuns += 1;
        callback(...args);
      }, milliseconds === 20000 ? 1 : milliseconds);
      window.fetch = async () => new Response(JSON.stringify({session_id: "session", offer_url: "/offer"}), {status: 200});
      window.RTCPeerConnection = class {
        constructor() { this.iceGatheringState = "complete"; }
        createDataChannel() { return {readyState: "connecting", close() {}, send() {}}; }
        getSenders() { return []; }
        close() {}
      };
    """
    with sync_playwright() as api:
        try:
            browser = api.chromium.launch(headless=True)
        except Exception as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Playwright Chromium is not installed")
            raise
        try:
            page = browser.new_page()
            page.add_init_script(init)
            page.goto(index.as_uri())
            result = page.evaluate(
                """async () => {
                  const transport = await window.createInterviewTransport({setup: {}, onEvent() {}});
                  const message = await transport.ready().then(() => "opened", error => error.message);
                  await new Promise(resolve => setTimeout(resolve, 70));
                  return {message, waitTimerRuns: window.__waitTimerRuns};
                }"""
            )
        finally:
            browser.close()

    assert result == {"message": "Interview data channel timed out.", "waitTimerRuns": 2}


def test_permission_prompt_hides_after_media_setup() -> None:
    """Remove the initial permission prompt from the active conversation layout."""
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    with sync_playwright() as api:
        browser = api.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.add_init_script("""
                navigator.mediaDevices.getUserMedia = async () => ({getTracks: () => []});
                window.createInterviewTransport = async () => ({
                    unlockPlayback: async () => {}, start: async () => {},
                    ready: async () => {}, close: async () => {}
                });
            """)
            page.goto(index.as_uri())
            page.locator('button[type="submit"]').click()
            page.locator("#permission-button").click()
            page.locator("#permission").wait_for(state="hidden")
            assert page.locator("#status-label").inner_text() == "Listening"
        finally:
            browser.close()


def test_speaker_retry_preserves_the_current_interview() -> None:
    """Retry playback without reacquiring the microphone or replacing the session."""
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    with sync_playwright() as api:
        browser = api.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.add_init_script("""
              window.__created = 0; window.__unlocked = 0;
              navigator.mediaDevices.getUserMedia = async () => {
                throw new Error("Speaker retry must not request microphone access");
              };
              window.createInterviewTransport = async () => {
                window.__created++;
                return {async unlockPlayback() {window.__unlocked++;}, async close() {}};
              };
            """)
            page.goto(index.as_uri())
            page.locator('button[type="submit"]').click()
            page.wait_for_function("window.__created === 1")
            page.locator("#speaker-button").click()
            page.wait_for_function("window.__unlocked === 1")
            assert page.evaluate("window.__created") == 1
            assert "Speaker enabled" in page.locator("#notice").inner_text()
            page.locator("#end-button").click()
            assert page.locator("#speaker-button").is_disabled()
        finally:
            browser.close()


def test_interview_ready_waits_for_speaker_playback() -> None:
    """Do not request the greeting before the remote audio element can play."""
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    with sync_playwright() as api:
        browser = api.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.add_init_script("""
              window.fetch = async () => new Response(JSON.stringify({
                session_id: "session", offer_url: "/offer"
              }), {status: 200});
              window.__readySent = 0;
              HTMLMediaElement.prototype.play = function() {
                if (this.srcObject) return new Promise(resolve => {window.__allowAudio = resolve;});
                return Promise.resolve();
              };
              window.RTCPeerConnection = class {
                constructor() {window.__peer = this;}
                createDataChannel() {
                  return {readyState: "open", send() {window.__readySent++;}};
                }
                getSenders() {return [];}
                close() {}
              };
            """)
            page.goto(index.as_uri())
            page.evaluate("""async () => {
              window.__transport = await window.createInterviewTransport({setup: {}, onEvent() {}});
              await window.__transport.unlockPlayback();
              window.__peer.ontrack({streams: [new MediaStream()]});
              window.__ready = window.__transport.ready();
            }""")
            page.wait_for_function("typeof window.__allowAudio === 'function'")
            assert page.evaluate("window.__readySent") == 0
            page.evaluate("window.__allowAudio()")
            page.wait_for_function("window.__readySent === 1")
        finally:
            browser.close()


@pytest.mark.parametrize("language", ["tanglish", "hinglish", "english"])
def test_language_selection_reaches_session_setup(language) -> None:
    """Send the candidate's selected language to the session factory."""
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
            page.locator('select[name="language"]').select_option(language)
            assert page.locator("#language-label").inner_text().lower() == language
            page.locator('button[type="submit"]').click()
            page.wait_for_function("Boolean(window.__selectedSetup)")
            assert page.evaluate("window.__selectedSetup.language") == language
        finally:
            browser.close()

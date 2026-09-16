"""Verify local audio feedback without a provider connection."""

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")


def test_voice_visual_follows_audio_silence_interruption_and_cleanup() -> None:
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    with sync_playwright() as api:
        browser = api.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(index.as_uri())
            page.evaluate("""async () => {
              window.interviewVoice.start();
              const context = new AudioContext(); await context.resume();
              const oscillator = context.createOscillator();
              const gain = context.createGain(); gain.gain.value = 0.15;
              const destination = context.createMediaStreamDestination();
              oscillator.connect(gain).connect(destination); oscillator.start();
              window.signal = {context, oscillator, gain, destination};
              window.interviewVoice.attach('input', destination.stream);
            }""")
            level = "Number(document.querySelector('#voice-orb').dataset.level)"
            page.wait_for_function(f"{level} > 0.3")
            page.evaluate("window.signal.gain.gain.value = 0")
            page.wait_for_function(f"{level} < 0.01")
            page.evaluate("""() => {
              window.interviewVoice.detach('input');
              window.interviewVoice.attach('output', window.signal.destination.stream);
              window.signal.gain.gain.value = 0.15;
              window.interviewVoice.enableOutput(true);
            }""")
            page.wait_for_function(f"{level} > 0.3")
            page.evaluate("window.interviewVoice.enableOutput(false)")
            page.wait_for_function(f"{level} < 0.01")
            page.evaluate("window.interviewVoice.enableOutput(true)")
            page.wait_for_function(f"{level} > 0.3")
            page.emulate_media(reduced_motion="reduce")
            page.wait_for_function(f"{level} === 0")
            page.emulate_media(reduced_motion="no-preference")
            page.wait_for_function(f"{level} > 0.3")
            page.evaluate("window.interviewVoice.stop()")
            assert page.evaluate(level) == 0
            assert page.evaluate("window.signal.destination.stream.getAudioTracks()[0].readyState") == "live"
            page.evaluate("window.signal.oscillator.stop(); window.signal.context.close()")
            assert not errors
        finally:
            browser.close()


def test_incoming_captions_do_not_scroll_the_voice_view() -> None:
    from playwright.sync_api import sync_playwright

    index = Path(__file__).parents[1] / "interview" / "web" / "index.html"
    with sync_playwright() as api:
        browser = api.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 360, "height": 800})
            page.add_init_script("""window.createInterviewTransport = async ({onEvent}) => {
              window.emit = onEvent; return {async close(){}};
            };""")
            page.goto(index.as_uri())
            page.locator('button[type="submit"]').click()
            page.evaluate("window.scrollTo(0,0)")
            page.evaluate("""() => {
              for (let i = 0; i < 30; i++) window.emit({
                type:'interview.caption', segment_id:String(i), final:true,
                text:'A longer answer about my work and the people I meet every day.', speaker:'You'
              });
            }""")
            page.wait_for_function("document.querySelector('#conversation').scrollTop > 0")
            assert page.evaluate("window.scrollY") == 0
            page.evaluate("document.querySelector('#conversation').scrollTop = 0")
            page.locator('#jump-latest').wait_for(state='visible')
            page.evaluate("window.emit({type:'interview.caption',segment_id:'31',final:true,text:'Another answer.',speaker:'You'})")
            assert page.locator('#conversation').evaluate('(el) => el.scrollTop') == 0
            page.locator('#jump-latest').click()
            page.wait_for_function("document.querySelector('#conversation').scrollTop > 0")
        finally:
            browser.close()

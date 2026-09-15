"""Verify that serving evidence identifies unchanged source and synthetic audio."""

import hashlib
import json
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUMIK = ROOT / "demo" / "rumik"


def test_pinned_source_hashes_and_codec_agree_with_serving_manifest():
    """The inspected server and codec cannot silently drift away from their recorded pin."""
    manifest = json.loads((RUMIK / "manifest.json").read_text())
    source = ROOT / manifest["source_directory"]
    assert len(manifest["model_revision"]) == 40
    assert manifest["model_revision"] == manifest["server_revision"]
    for filename, digest in manifest["source_sha256"].items():
        assert hashlib.sha256((source / filename).read_bytes()).hexdigest() == digest
    assert manifest["server_sha256"] == manifest["source_sha256"]["server.py"]
    codec = json.loads((source / "codec/preprocessor_config.json").read_text())
    assert codec["sampling_rate"] == manifest["audio"]["sample_rate_hz"]


def test_audio_fixture_is_explicitly_synthetic_and_matches_the_wire_contract():
    """Fixture duration comes from complete PCM frames rather than an invented model timing."""
    responses = json.loads((RUMIK / "fixtures/responses.json").read_text())
    assert "synthetic" in responses["provenance"]
    with wave.open(str(RUMIK / "fixtures/sample.wav"), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        assert wav.getnframes() / wav.getframerate() == responses["success"]["duration_seconds"]
        assert len(wav.readframes(wav.getnframes())) == wav.getnframes() * 2


def test_request_cases_cover_short_interview_speech_and_schema_errors():
    """The shared corpus includes ordinary speech and distinct upstream failure classes."""
    cases = json.loads((RUMIK / "fixtures/requests.json").read_text())
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["expected_status"] for case in cases} == {200, 400, 422}
    speakers = json.loads((ROOT / "demo/rumik/manifest.json").read_text())["source_directory"]
    configured = json.loads((ROOT / speakers / "config.json").read_text())["speakers"]
    for case in cases:
        if case["expected_status"] == 200:
            assert case["payload"]["speaker"] in configured
            assert 0 < len(case["payload"]["input"]) <= 200

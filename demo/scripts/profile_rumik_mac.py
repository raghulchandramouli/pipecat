"""Measure synchronized Rumik generation and codec stages on Metal."""

import argparse
import hashlib
import io
import json
import time
import wave
from datetime import UTC, datetime
from pathlib import Path

from demo.rumik.mac_server import MacRumikOSS, MacServerConfig, configure_model_caches


def main():
    """Profile local model inference with explicit dtype and reproducible inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dtype", choices=["float32", "float16"], required=True)
    args = parser.parse_args()
    configure_model_caches()
    from types import SimpleNamespace

    import numpy as np
    import torch
    import transformers

    config = MacServerConfig(device="mps", dtype=args.dtype)
    start = time.perf_counter()
    engine = MacRumikOSS(config, torch_module=torch, transformers_module=transformers)
    torch.mps.synchronize()
    load_seconds = time.perf_counter() - start
    stages = {}
    generate = engine.model.generate_audio
    decode = engine.mimi.decode

    def timed_generate(*a, **kw):
        torch.mps.synchronize()
        start = time.perf_counter()
        result = generate(*a, **kw)
        torch.mps.synchronize()
        stages["generation_seconds"] = time.perf_counter() - start
        stages["audio_tokens"] = result.shape[-1] - kw["input_ids"].shape[-1]
        return result

    def timed_decode(*a, **kw):
        torch.mps.synchronize()
        start = time.perf_counter()
        result = decode(*a, **kw)
        torch.mps.synchronize()
        stages["codec_seconds"] = time.perf_counter() - start
        stages["finite_waveform"] = bool(torch.isfinite(result.audio_values).all().item())
        return result

    engine.model.generate_audio = timed_generate
    engine.mimi.decode = timed_decode
    output_dir = Path("demo/rumik/results")
    report = {
        "dtype": args.dtype,
        "device": str(next(engine.model.parameters()).device),
        "model_dtype": str(next(engine.model.parameters()).dtype),
        "codec_dtype": str(next(engine.mimi.parameters()).dtype),
        "torch": torch.__version__,
        "load_seconds": load_seconds,
        "cases": [],
    }
    for index, text in enumerate(["Thank you.", "Thank you.", "What did you change first?"]):
        stages.clear()
        torch.manual_seed(42)
        request = SimpleNamespace(
            input=text, speaker="Ira", max_new_tokens=2048, temperature=0.8, top_k=30
        )
        start = time.perf_counter()
        data = engine.synthesize(request)
        torch.mps.synchronize()
        elapsed = time.perf_counter() - start
        with wave.open(io.BytesIO(data), "rb") as wav:
            seconds = wav.getnframes() / wav.getframerate()
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        path = output_dir / f"profile-{args.dtype}-{index}.wav"
        path.write_bytes(data)
        case = dict(
            stages,
            text=text,
            warmup=index == 0,
            total_seconds=elapsed,
            audio_seconds=seconds,
            real_time_factor=elapsed / seconds,
            peak=int(np.abs(pcm.astype(np.int32)).max()),
            wav_path=str(path),
            wav_sha256=hashlib.sha256(data).hexdigest(),
        )
        report["cases"].append(case)
        report["captured_at_utc"] = datetime.now(UTC).isoformat()
        report["mps_allocated_bytes"] = torch.mps.current_allocated_memory()
        (output_dir / f"profile-{args.dtype}.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(case), flush=True)


if __name__ == "__main__":
    main()

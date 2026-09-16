/* Audio analysis stays local and never connects the microphone to the speakers. */
(() => {
  const orb = document.getElementById("voice-orb");
  if (!orb) return;
  const bars = [...orb.querySelectorAll(".voice-bars i")];
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const sources = new Map();
  let context, frame = 0, level = 0, outputEnabled = false;

  function reset() {
    level = 0;
    orb.style.setProperty("--voice-level", "0");
    orb.dataset.level = "0";
    bars.forEach(bar => bar.style.setProperty("--bar-level", "0"));
  }
  function draw() {
    frame = 0;
    if (!context || document.hidden || reduced.matches) { reset(); return; }
    let peak = 0;
    const bands = [0, 0, 0, 0, 0];
    for (const [kind, source] of sources) {
      if (kind === "output" && !outputEnabled) continue;
      source.analyser.getFloatTimeDomainData(source.wave);
      const rms = Math.sqrt(source.wave.reduce((sum, value) => sum + value * value, 0) / source.wave.length);
      peak = Math.max(peak, Math.min(1, Math.max(0, rms - 0.008) * 6));
      source.analyser.getByteFrequencyData(source.bins);
      bands.forEach((_, index) => {
        const start = 2 + index * 8;
        let total = 0;
        for (let bin = start; bin < start + 8; bin++) total += source.bins[bin];
        bands[index] = Math.max(bands[index], rms > 0.008 ? total / (8 * 255) : 0);
      });
    }
    level += (peak - level) * (peak > level ? 0.3 : 0.12);
    orb.style.setProperty("--voice-level", level.toFixed(3));
    orb.dataset.level = level.toFixed(3);
    bars.forEach((bar, index) => bar.style.setProperty("--bar-level", bands[index].toFixed(3)));
    frame = requestAnimationFrame(draw);
  }
  function schedule() {
    if (!frame && context && !document.hidden && !reduced.matches) frame = requestAnimationFrame(draw);
  }
  function detach(kind, stream) {
    const source = sources.get(kind);
    if (!source || (stream && source.stream !== stream)) return;
    source.node.disconnect(); source.analyser.disconnect(); sources.delete(kind);
  }
  window.interviewVoice = {
    start() {
      try {
        context ||= new (window.AudioContext || window.webkitAudioContext)();
        context.resume().catch(() => {});
        schedule();
      } catch { /* Audio feedback is optional; the conversation can still run. */ }
    },
    attach(kind, stream) {
      if (!context || typeof context.createMediaStreamSource !== "function" || !(stream instanceof MediaStream) || !stream.getAudioTracks().length) return;
      detach(kind);
      try {
        const node = context.createMediaStreamSource(stream);
        const analyser = context.createAnalyser(); analyser.fftSize = 512; analyser.smoothingTimeConstant = 0.65;
        node.connect(analyser);
        sources.set(kind, {stream, node, analyser, wave: new Float32Array(512), bins: new Uint8Array(256)});
        schedule();
      } catch { /* A disconnected audio track must not interrupt the session. */ }
    },
    detach,
    enableOutput(enabled) { outputEnabled = enabled; },
    stop() {
      cancelAnimationFrame(frame); frame = 0;
      for (const kind of sources.keys()) detach(kind);
      const previous = context; context = undefined; outputEnabled = false;
      previous?.close().catch(() => {}); reset();
    },
  };
  function visibilityChanged() {
    cancelAnimationFrame(frame); frame = 0; reset(); schedule();
  }
  reduced.addEventListener("change", visibilityChanged);
  document.addEventListener("visibilitychange", visibilityChanged);
  window.addEventListener("pagehide", () => window.interviewVoice.stop());
})();

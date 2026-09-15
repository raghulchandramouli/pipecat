(() => {
  const $ = (id) => document.getElementById(id);
  const setup = $("setup"), room = $("room"), form = $("setup-form"), notice = $("notice");
  let transport = null, micStream = null, captions = new Map(), setupData = null;
  let roomClosed = false, transportAttempt = 0;

  async function createTransport() {
    if (!setupData || !window.createInterviewTransport) return null;
    const attempt = ++transportAttempt;
    const created = await window.createInterviewTransport({ setup: setupData, onEvent: receive });
    if (roomClosed || attempt !== transportAttempt) {
      await created?.close?.();
      return null;
    }
    transport = created;
    return created;
  }

  async function finishInterview() {
    if (roomClosed) return;
    roomClosed = true;
    transportAttempt += 1;
    const current = transport;
    transport = null;
    micStream?.getTracks().forEach((track) => track.stop());
    micStream = null;
    setStatus("Ended");
    $("end-button").disabled = true;
    $("speaker-button").disabled = true;
    $("permission-button").disabled = true;
    await current?.close?.();
  }

  function setStatus(status) {
    const labels = { listening: "Listening", giving_time: "Giving you time", thinking: "Thinking", speaking: "Speaking", reconnecting: "Reconnecting" };
    $("status-label").textContent = labels[String(status).toLowerCase().replaceAll(" ", "_")] || status || "Ready";
    $("status-dot").className = `status-dot ${String(status).toLowerCase() === "reconnecting" ? "reconnecting" : ""}`;
  }
  function caption(text, final, speaker = "You", id) {
    if (!id) return;
    id = String(id);
    let bubble = captions.get(id);
    if (!bubble) { bubble = document.createElement("article"); bubble.dataset.final = "false"; captions.set(id, bubble); $("conversation").append(bubble); }
    if (bubble.dataset.final === "true" && !final) return;
    bubble.dataset.final = String(final);
    bubble.className = `bubble ${speaker === "Interviewer" ? "interviewer" : ""} ${final ? "" : "provisional"}`;
    bubble.replaceChildren();
    const label = document.createElement("span"); label.className = "speaker"; label.textContent = `${speaker}${final ? "" : " · live"}`;
    const paragraph = document.createElement("p"); paragraph.textContent = text; bubble.append(label, paragraph);
    bubble.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  function receive(event) {
    if (typeof event === "string") { try { event = JSON.parse(event); } catch { return; } }
    if (roomClosed || !event || typeof event !== "object" || event.v !== 1 || !Number.isInteger(event.seq) || event.seq < 0) return;
    const type = event.type || "";
    if (type === "interview.status") { setStatus(event.status); if (String(event.status).toLowerCase() === "speaking") transport?.resumePlayback?.(event.playback_epoch); if (event.question_index !== null && event.question_index !== undefined) $("question-count").textContent = `Question ${event.question_index}${event.question_count ? ` of ${event.question_count}` : ""}`; }
    else if (type === "interview.caption") { if (event.segment_id) caption(event.text || "", Boolean(event.final), event.speaker || "You", `${event.connection_generation}:${event.segment_id}`); }
    else if (type === "interview.reply") caption(event.text || "", true, "Interviewer", event.dispatch_id || event.reply_id || event.id);
    else if (type === "interview.interruption") { setStatus("listening"); transport?.clearPlayback?.(event.playback_epoch); }
    else if (type === "interview.error") notice.textContent = event.message || "The interview needs attention.";
    else if (type === "interview.ended") void finishInterview();
  }
  async function openRoom(event) {
    event.preventDefault(); notice.textContent = "";
    const data = Object.fromEntries(new FormData(form));
    data.duration_minutes = Number(data.duration_minutes);

    const focus = data.rubric;
    data.rubric = [
      ["Teamwork", "Tell me about a time you worked with others to achieve a shared goal."],
      ["Communication", "Tell me about a time you explained something difficult to another person."],
      ["Conflict resolution", "Describe a disagreement you handled and how you resolved it."],
      ["Ownership", "Tell me about a time you took responsibility for a mistake."],
      ["Resilience", "Describe a setback, how you responded, and what you learned."],
    ].map(([competency, question]) => ({ competency, guidance: question, weight: 1 }));
    // Candidate-selected evaluation guidance accompanies each behavioural question.
    data.rubric.forEach(item => { item.guidance += " Listen for: " + focus; });
    setup.hidden = true; room.hidden = false; $("room-title").textContent = data.role;
    setupData = data;
    window.__interviewSetup = data;
    roomClosed = false;
    $("end-button").disabled = false;
    $("speaker-button").disabled = false;
    $("permission-button").disabled = true;
    setStatus("Ready");
    try {
      await createTransport();
      if (!transport && !roomClosed) notice.textContent = "Interview server is not attached. Start the demo server, then reload this page.";
    } catch (error) { if (!roomClosed) notice.textContent = error.message || "Could not create an interview session."; }
    finally { if (!roomClosed) $("permission-button").disabled = false; }
  }
  async function enableAudio() {
    notice.textContent = "";
    const button = $("permission-button"); button.disabled = true;
    try {
      const activeTransport = transport || await createTransport();
      if (!activeTransport) throw new Error("Interview connection is not ready. Reload after starting the server.");
      // Resume playback in the permission click. Network negotiation can consume
      // the browser activation required to unlock audible output.
      await activeTransport.unlockPlayback();
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      if (roomClosed || activeTransport !== transport) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      micStream = stream;
      await activeTransport.start({ stream: micStream, onEvent: receive });
      await activeTransport.ready();
      if (roomClosed || activeTransport !== transport) return;
      $("permission").hidden = true; setStatus("listening");
    } catch (error) {
      micStream?.getTracks().forEach((track) => track.stop()); micStream = null;
      if (transport && !roomClosed) {
        const failedTransport = transport;
        transport = null;
        transportAttempt += 1;
        await failedTransport.close();
        try { await createTransport(); } catch { /* A later retry creates a fresh session. */ }
      }
      if (!roomClosed) { button.disabled = false; notice.textContent = error.message || "Microphone permission is needed to begin."; }
    }
  }
  $("speaker-button").addEventListener("click", async () => {
    try {
      if (!transport || roomClosed) return;
      await transport.unlockPlayback();
      notice.textContent = "Speaker enabled. The next interviewer reply will play aloud.";
    } catch (error) {
      notice.textContent = error.message || "Could not enable speaker playback.";
    }
  });
  function updateLanguage() {
    const language = $("language-select").value;
    const labels = {tanglish: "Tanglish", hinglish: "Hinglish", english: "English"};
    $("language-label").textContent = labels[language];
    $("permission-button").textContent = {
      tanglish: "பேச ஆரம்பிக்கலாம் / Start",
      hinglish: "बात शुरू करें / Start",
      english: "Start talking",
    }[language];
    $("audio-help").textContent = {
      tanglish: "குரல் கேட்கலைன்னா மேலே உள்ள “Enable speaker” பட்டனை அழுத்துங்க. நீங்க தொடர்ந்து பேசலாம்.",
      hinglish: "आवाज़ नहीं आए तो ऊपर “Enable speaker” दबाएँ। आप बात जारी रख सकते हैं।",
      english: "If you cannot hear the interviewer, press Enable speaker. You can keep talking.",
    }[language];
  }
  $("language-select").addEventListener("change", updateLanguage);
  updateLanguage();
  form.addEventListener("submit", openRoom); $("permission-button").addEventListener("click", enableAudio);
  $("end-button").addEventListener("click", () => { void finishInterview(); });
  window.addEventListener("pagehide", () => { void finishInterview(); });
})();

window.createInterviewTransport = window.createInterviewTransport || (async ({ setup, onEvent }) => {
  const $ = (id) => document.getElementById(id);
  const notice = $("notice");
  const response = await fetch("/api/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(setup) });
  if (!response.ok) throw new Error(`Could not create session (${response.status}).`);
  const created = await response.json();
  const pc = new RTCPeerConnection();
  let sessionId = created.session_id, generation = created.connection_generation || 1, lastSeq = -1, channel, remoteStream, playbackEpoch = 0, playbackUnlocked = false, playbackAllowed = true, closed = false, iceCandidates = [], answerPcId, cancelReadyWait, cancelIceWait;
  const audio = document.createElement("audio"); audio.autoplay = true; audio.setAttribute("aria-hidden", "true"); audio.style.display = "none"; document.body.append(audio);
  const playRemote = () => { if (!remoteStream || !playbackUnlocked || !playbackAllowed || closed) return Promise.resolve(); audio.srcObject = remoteStream; audio.muted = false; return audio.play().catch(() => { $("speaker-button").disabled = false; notice.textContent = "Playback is blocked. Press Enable speaker to resume audio."; throw new Error("Playback is blocked. Press Enable speaker to resume audio."); }); };
  pc.ontrack = (event) => { remoteStream = event.streams[0] || new MediaStream([event.track]); playRemote().catch(() => {}); };
  const attachChannel = (candidate) => {
    channel = candidate;
    channel.onmessage = (event) => {
    let message; try { message = JSON.parse(event.data); } catch { return; }
    if (!message || typeof message !== "object" || message.v !== 1 || message.session_id !== sessionId || message.connection_generation !== generation || !Number.isInteger(message.seq) || message.seq < 0 || message.seq <= lastSeq) return;
    lastSeq = message.seq; onEvent(message);
    };
  };
  attachChannel(pc.createDataChannel("interview-events"));
  pc.ondatachannel = ({ channel: remoteChannel }) => attachChannel(remoteChannel);
  pc.onicecandidate = ({ candidate }) => { if (candidate) iceCandidates.push({ candidate: candidate.candidate, sdp_mid: candidate.sdpMid, sdp_mline_index: candidate.sdpMLineIndex }); };
  pc.onconnectionstatechange = () => {
    if (["disconnected", "failed"].includes(pc.connectionState)) {
      // This status comes from the local peer connection and is not part of the
      // server event sequence filtered in attachChannel().
      onEvent({ v: 1, session_id: sessionId, connection_generation: generation, seq: lastSeq + 1, type: "interview.status", status: "Reconnecting" });
    }
  };
  const waitFor = (isReady, description, registerCancel) => new Promise((resolve, reject) => {
    let settled = false, poll, timeout;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(poll);
      clearTimeout(timeout);
      registerCancel(null);
      callback(value);
    };
    const cancel = () => finish(reject, new Error(`Interview ${description} closed.`));
    registerCancel(cancel);
    const check = () => {
      if (closed) return cancel();
      if (isReady()) return finish(resolve);
      poll = setTimeout(check, 50);
    };
    timeout = setTimeout(() => finish(reject, new Error(`Interview ${description} timed out.`)), 20000);
    check();
  });
  return {
    async start({ stream }) {
      stream.getTracks().forEach((track) => pc.addTrack(track, stream));
      const offer = await pc.createOffer(); await pc.setLocalDescription(offer);
      const answerResponse = await fetch(created.offer_url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sdp: offer.sdp, type: offer.type }) });
      if (!answerResponse.ok) throw new Error(`Could not connect audio (${answerResponse.status}).`);
      const answer = await answerResponse.json(); answerPcId = answer.pc_id; await pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
      if (pc.iceGatheringState !== "complete") {
        await waitFor(() => pc.iceGatheringState === "complete", "ICE gathering", (cancel) => { cancelIceWait = cancel; });
      }
      if (iceCandidates.length) { const iceResponse = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/ice`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pc_id: answerPcId, candidates: iceCandidates }) }); if (!iceResponse.ok) throw new Error(`Could not register ICE candidates (${iceResponse.status}).`); }
    },
    async unlockPlayback() {
      if (closed) return;
      // Unlock the actual media element in the button gesture before network negotiation.
      if (!remoteStream) {
        audio.src = "data:audio/wav;base64,UklGRsQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
        await audio.play();
        audio.pause();
        audio.removeAttribute("src");
      }
      playbackUnlocked = true;
      if (remoteStream) await playRemote();
    },
    async ready() {
      if (!channel || channel.readyState !== "open") {
        await waitFor(() => channel?.readyState === "open", "data channel", (cancel) => { cancelReadyWait = cancel; });
      }
      if (closed) throw new Error("Interview data channel closed.");
      await waitFor(() => Boolean(remoteStream), "speaker connection", (cancel) => { cancelReadyWait = cancel; });
      await playRemote();
      if (closed) throw new Error("Interview speaker connection closed.");
      channel.send(JSON.stringify({ v: 1, type: "interview.ready", session_id: sessionId }));
    },
    clearPlayback(epoch) {
      const nextEpoch = Number.isInteger(epoch) ? epoch : playbackEpoch + 1;
      if (nextEpoch < playbackEpoch) return;
      playbackEpoch = nextEpoch;
      playbackAllowed = false;
      audio.muted = true;
      audio.pause();
      audio.srcObject = null;
    },
    resumePlayback(epoch) { if (!Number.isInteger(epoch) || epoch < playbackEpoch || closed) return; playbackEpoch = epoch; playbackAllowed = true; playRemote().catch((error) => { notice.textContent = error.message; }); },
    async close() { if (closed) return; closed = true; cancelReadyWait?.(); cancelIceWait?.(); audio.muted = true; audio.pause(); audio.srcObject = null; pc.getSenders().forEach(sender => sender.track?.stop()); pc.close(); audio.remove(); await Promise.race([fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE", keepalive: true }).catch(() => {}), new Promise((resolve) => setTimeout(resolve, 1000))]); },
  };
});

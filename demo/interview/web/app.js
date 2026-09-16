(() => {
  const $ = (id) => document.getElementById(id);
  const setup = $("setup"), room = $("room"), form = $("setup-form"), notice = $("notice");
  const recoveryActions = new Set(["retry_response", "retry_transcription", "restart_answer"]);
  const labels = {
    english: { start: "Start talking", speaker: "Enable speaker", end: "End conversation", restart: "Start another conversation", repeat: "Repeat that", explain: "Say more", thinking: "Give me time", skip: "New topic", retry_response: "Retry response", retry_transcription: "Retry transcription", restart_answer: "Start answer again", natural: "Talk naturally, stay with this topic, or choose a new one.", waiting: "Start speaking whenever you are ready.", recovery: "Something needs your attention. Your earlier words are kept unless you start again.", discard: "Starting again discards your pending words for this topic.", audio: "If you cannot hear the conversation guide, press Enable speaker. You can keep talking." },
    tanglish: { start: "பேச ஆரம்பிக்கலாம்", speaker: "ஒலியை இயக்கவும்", end: "உரையாடலை முடிக்கவும்", restart: "மீண்டும் பேசலாம்", repeat: "மறுபடி சொல்லுங்க", explain: "இன்னும் சொல்லுங்க", thinking: "கொஞ்சம் நேரம் வேணும்", skip: "வேற விஷயம் பேசலாம்", retry_response: "மீண்டும் பதில் சொல்லுங்க", retry_transcription: "கேட்டதை மீண்டும்", restart_answer: "பதிலை மீண்டும் தொடங்கலாம்", natural: "இயல்பாக பேசலாம். இதே விஷயத்தைப் பற்றி தொடரலாம் அல்லது வேற விஷயம் பேசலாம் சொல்லலாம்.", waiting: "தயாரானதும் பேச ஆரம்பிங்க.", recovery: "இதற்கு உங்கள் கவனம் தேவை. மீண்டும் தொடங்கினால் மட்டுமே காத்திருக்கும் வார்த்தைகள் நீங்கும்.", discard: "மீண்டும் தொடங்கினால் இந்த விஷயத்துக்கான காத்திருக்கும் வார்த்தைகள் நீங்கும்.", audio: "குரல் கேட்கலைன்னா “ஒலியை இயக்கவும்” பட்டனை அழுத்துங்க. நீங்க தொடர்ந்து பேசலாம்." },
    hinglish: { start: "बात शुरू करें / Start", speaker: "Speaker चालू करें", end: "बातचीत खत्म करें", restart: "फिर से बात करें", repeat: "फिर से बोलिए", explain: "और बताइए", thinking: "थोड़ा समय चाहिए", skip: "नया विषय", retry_response: "जवाब फिर से", retry_transcription: "मेरी बात फिर से लें", restart_answer: "जवाब फिर से शुरू करें", natural: "आराम से बात करें। इसी विषय पर रहें या नया विषय चुनें।", waiting: "तैयार हों तो बोलना शुरू करें।", recovery: "इस पर आपका ध्यान चाहिए। फिर शुरू करने पर ही रुकी हुई बात हटेगी।", discard: "फिर शुरू करने से इस विषय की रुकी हुई बात हट जाएगी।", audio: "आवाज़ नहीं आए तो “Speaker चालू करें” दबाएँ। आप बात जारी रख सकते हैं।" },
  };
  const geminiLanguages = [
    ["afrikaans", "Afrikaans", "af"], ["akan", "Akan", "ak"], ["albanian", "Albanian", "sq"], ["amharic", "Amharic", "am"], ["arabic", "Arabic", "ar"], ["armenian", "Armenian", "hy"], ["assamese", "Assamese", "as"], ["azerbaijani", "Azerbaijani", "az"], ["basque", "Basque", "eu"], ["belarusian", "Belarusian", "be"], ["bengali", "Bengali", "bn"], ["bosnian", "Bosnian", "bs"], ["bulgarian", "Bulgarian", "bg"], ["burmese", "Burmese", "my"], ["catalan", "Catalan", "ca"], ["cebuano", "Cebuano", "ceb"], ["chinese", "Chinese", "zh"], ["croatian", "Croatian", "hr"], ["czech", "Czech", "cs"], ["danish", "Danish", "da"], ["dutch", "Dutch", "nl"], ["english", "English", "en"], ["estonian", "Estonian", "et"], ["faroese", "Faroese", "fo"], ["filipino", "Filipino", "fil"], ["finnish", "Finnish", "fi"], ["french", "French", "fr"], ["galician", "Galician", "gl"], ["georgian", "Georgian", "ka"], ["german", "German", "de"], ["greek", "Greek", "el"], ["gujarati", "Gujarati", "gu"], ["hausa", "Hausa", "ha"], ["hebrew", "Hebrew", "iw"], ["hindi", "Hindi", "hi"], ["hungarian", "Hungarian", "hu"], ["icelandic", "Icelandic", "is"], ["indonesian", "Indonesian", "id"], ["irish", "Irish", "ga"], ["italian", "Italian", "it"], ["japanese", "Japanese", "ja"], ["kannada", "Kannada", "kn"], ["kazakh", "Kazakh", "kk"], ["khmer", "Khmer", "km"], ["kinyarwanda", "Kinyarwanda", "rw"], ["korean", "Korean", "ko"], ["kurdish", "Kurdish", "ku"], ["kyrgyz", "Kyrgyz", "ky"], ["lao", "Lao", "lo"], ["latvian", "Latvian", "lv"], ["lithuanian", "Lithuanian", "lt"], ["macedonian", "Macedonian", "mk"], ["malay", "Malay", "ms"], ["malayalam", "Malayalam", "ml"], ["maltese", "Maltese", "mt"], ["maori", "Maori", "mi"], ["marathi", "Marathi", "mr"], ["mongolian", "Mongolian", "mn"], ["nepali", "Nepali", "ne"], ["norwegian", "Norwegian", "no"], ["odia", "Odia", "or"], ["oromo", "Oromo", "om"], ["pashto", "Pashto", "ps"], ["persian", "Persian", "fa"], ["polish", "Polish", "pl"], ["portuguese", "Portuguese", "pt"], ["punjabi", "Punjabi", "pa"], ["quechua", "Quechua", "qu"], ["romanian", "Romanian", "ro"], ["romansh", "Romansh", "rm"], ["russian", "Russian", "ru"], ["serbian", "Serbian", "sr"], ["sindhi", "Sindhi", "sd"], ["sinhala", "Sinhala", "si"], ["slovak", "Slovak", "sk"], ["slovenian", "Slovenian", "sl"], ["somali", "Somali", "so"], ["southern_sotho", "Southern Sotho", "st"], ["spanish", "Spanish", "es"], ["swahili", "Swahili", "sw"], ["swedish", "Swedish", "sv"], ["tajik", "Tajik", "tg"], ["tamil", "Tamil", "ta"], ["telugu", "Telugu", "te"], ["thai", "Thai", "th"], ["tswana", "Tswana", "tn"], ["turkish", "Turkish", "tr"], ["turkmen", "Turkmen", "tk"], ["ukrainian", "Ukrainian", "uk"], ["urdu", "Urdu", "ur"], ["uzbek", "Uzbek", "uz"], ["vietnamese", "Vietnamese", "vi"], ["welsh", "Welsh", "cy"], ["western_frisian", "Western Frisian", "fy"], ["wolof", "Wolof", "wo"], ["yoruba", "Yoruba", "yo"], ["zulu", "Zulu", "zu"],
  ];
  let transport = null, micStream = null, captions = new Map(), setupData = null;
  let roomClosed = false, transportAttempt = 0, capabilityFallback = null, lastStateRevision = -1;
  let interaction = { supported: false, negotiated: false, promptRevision: 0, permitted: [], recoveryToken: null };
  const copyLanguage = () => {
    const language = setupData?.language || $("language-select").value || "tanglish";
    return ({ tamil: "tanglish", hindi: "hinglish" })[language] || (labels[language] ? language : "english");
  };
  const copy = () => labels[copyLanguage()];
  const stateCopy = {
    english: { listening: "I'm listening. Take your time.", thinking: "Take your time. Speak whenever you're ready.", generating: "Getting a response ready. You can keep talking.", speaking: "You can interrupt or ask me to say more.", recovering: "I need your help to continue. Choose an action below.", lost: "Connection lost. Start another conversation to continue." },
    tanglish: { listening: "கேட்டுட்டு இருக்கேன். நிதானமா பேசுங்க.", thinking: "அவசரம் இல்ல. தயாரானதும் பேசுங்க.", generating: "பதில் தயாராகுது. நீங்க தொடர்ந்து பேசலாம்.", speaking: "நடுவிலேயும் பேசலாம், இன்னும் சொல்ல சொல்லலாம்.", recovering: "தொடர கீழே ஒரு வழியை தேர்வு செய்யுங்க.", lost: "இணைப்பு துண்டாயிடுச்சு. மீண்டும் பேச தொடங்குங்க." },
    hinglish: { listening: "मैं सुन रहा हूँ। आराम से बोलिए।", thinking: "आराम से सोचिए। तैयार हों तो बोलिए।", generating: "जवाब तैयार हो रहा है। आप बोलना जारी रख सकते हैं।", speaking: "आप बीच में बोल सकते हैं या और बताने को कह सकते हैं।", recovering: "आगे बढ़ने के लिए नीचे एक विकल्प चुनिए।", lost: "Connection टूट गया। फिर से बात शुरू करें।" },
  };
  const stateLabel = (state) => stateCopy[copyLanguage()][state] || state;
  const setText = (id, value) => { $(id).textContent = value; };
  const option = (value, label, selected = false) => {
    const element = document.createElement("option"); element.value = value; element.textContent = label; element.selected = selected; return element;
  };
  function populateLanguageChoices() {
    const output = $("language-select");
    output.replaceChildren(
      option("tamil", "தமிழ் — Simple Tamil", true),
      option("tanglish", "Tanglish — Tamil + English"),
      option("hinglish", "Hinglish — Hindi + English"),
      ...geminiLanguages.filter(([key]) => key !== "tamil").map(([key, name]) => option(key, name)),
    );
  }

  async function createTransport() {
    if (!setupData || !window.createInterviewTransport) return null;
    const attempt = ++transportAttempt;
    const created = await window.createInterviewTransport({ setup: setupData, onEvent: receive });
    if (roomClosed || attempt !== transportAttempt) { await created?.close?.(); return null; }
    transport = created;
    return created;
  }
  function clearInteraction() {
    clearTimeout(capabilityFallback); capabilityFallback = null; lastStateRevision = -1;
    interaction = { supported: false, negotiated: false, promptRevision: 0, permitted: [], recoveryToken: null };
    $("active-prompt").hidden = true; $("interaction-controls").hidden = true; $("recovery").hidden = true;
  }
  function setStatus(status) {
    const stateLabels = { listening: "Listening", giving_time: "Giving you time", thinking: "Thinking", speaking: "Speaking", reconnecting: "Reconnecting", preparing_reply: "Preparing a reply", processing: "Preparing a reply" };
    const key = String(status || "ready").toLowerCase().replaceAll(" ", "_");
    $("room").dataset.state = key;
    setText("status-label", stateLabels[key] || status || "Ready");
    $("status-dot").className = `status-dot ${key === "reconnecting" ? "reconnecting" : ""}`;
  }
  async function finishInterview(note) {
    if (roomClosed) return;
    roomClosed = true; clearTimeout(capabilityFallback); transportAttempt += 1;
    const current = transport; transport = null;
    current?.sendCommand?.({ action: "end", promptRevision: interaction.promptRevision, preempt: true }).catch?.(() => {});
    micStream?.getTracks().forEach((track) => track.stop()); micStream = null; window.interviewVoice?.stop();
    setText("question-text", note || (copyLanguage() === "tanglish" ? "உங்க நேரத்துக்கும், அனுபவங்களைப் பகிர்ந்ததுக்கும் நன்றி. இந்த நேர்காணல் இத்துடன் முடிந்தது." : "Thank you for sharing your experiences. This interview is complete."));
    $("active-prompt").hidden = false;
    setText("state-text", copyLanguage() === "tanglish" ? "மீண்டும் பேச கீழே உள்ள பொத்தானை அழுத்துங்க." : "Start another interview whenever you are ready.");
    setStatus("Ended"); $("end-button").disabled = true; $("speaker-button").disabled = true; $("permission-button").disabled = true;
    $("interaction-controls").hidden = true; $("restart-button").hidden = false;
    await current?.close?.();
  }
  function resetToSetup() {
    roomClosed = true; transportAttempt += 1; captions.clear(); $("conversation").replaceChildren(); $("caption-announcements").textContent = "";
    clearInteraction(); notice.textContent = ""; $("restart-button").hidden = true; $("permission").hidden = false;
    $("hero").hidden = false; room.hidden = true; setup.hidden = false;
  }
  function isNearLatest() { const transcript = $("conversation"); return transcript.scrollTop + transcript.clientHeight >= transcript.scrollHeight - 80; }
  function caption(text, final, speaker = "You", id) {
    if (!id) return;
    const stayAtLatest = isNearLatest(); id = String(id); let bubble = captions.get(id);
    if (!bubble) { bubble = document.createElement("article"); bubble.dataset.final = "false"; captions.set(id, bubble); $("conversation").append(bubble); }
    if (bubble.dataset.final === "true" && !final) return;
    bubble.className = `bubble ${speaker === "Interviewer" ? "interviewer" : ""}${final ? "" : " provisional"}`; bubble.replaceChildren();
    const speakerNode = document.createElement("div"); speakerNode.className = "speaker"; speakerNode.textContent = speaker;
    const textNode = document.createElement("p"); textNode.textContent = text; bubble.append(speakerNode, textNode); bubble.dataset.final = String(final);
    if (final) $("caption-announcements").textContent = `${speaker}: ${text}`;
    if (stayAtLatest) requestAnimationFrame(() => $("conversation").scrollTo({ top: $("conversation").scrollHeight, behavior: "auto" }));
    $("jump-latest").hidden = stayAtLatest;
  }
  const actionLabel = (action) => copy()[action] || action.replaceAll("_", " ");
  function renderActions() {
    const visible = interaction.supported && !roomClosed; $("interaction-controls").hidden = !visible; if (!visible) return;
    setText("controls-help", copy().natural);
    for (const button of $("action-buttons").querySelectorAll("button")) { const action = button.dataset.action; button.textContent = actionLabel(action); button.disabled = !interaction.permitted.includes(action); }
    const showRecovery = interaction.recoveryToken && interaction.permitted.some((action) => recoveryActions.has(action)); $("recovery").hidden = !showRecovery; if (!showRecovery) return;
    setText("recovery-message", copy().recovery); const container = $("recovery-buttons"); container.replaceChildren();
    for (const action of interaction.permitted.filter((item) => recoveryActions.has(item))) { const button = document.createElement("button"); button.type = "button"; button.dataset.action = action; button.textContent = actionLabel(action); if (action === "restart_answer") button.title = copy().discard; container.append(button); }
  }
  function renderInteraction(event) {
    const revision = event.state_revision; if (Number.isInteger(revision) && revision < lastStateRevision) return; if (Number.isInteger(revision)) lastStateRevision = revision;
    if (typeof event.question_text === "string") { $("active-prompt").hidden = false; setText("question-text", event.question_text || "Getting the conversation ready."); }
    if (typeof event.interaction_state === "string") { $("active-prompt").hidden = false; setText("state-text", stateLabel(event.interaction_state)); }
    if (Number.isInteger(event.prompt_revision) && event.prompt_revision >= 0) interaction.promptRevision = event.prompt_revision;
    if (Array.isArray(event.permitted_actions)) interaction.permitted = event.permitted_actions.filter((item) => typeof item === "string");
    if (Object.hasOwn(event, "recovery_token") || Array.isArray(event.permitted_actions)) interaction.recoveryToken = typeof event.recovery_token === "string" ? event.recovery_token : null; renderActions();
  }
  function statusSnapshot(event) {
    if (Array.isArray(event.capabilities)) { clearTimeout(capabilityFallback); interaction.negotiated = true; interaction.supported = event.capabilities.includes("interaction_controls_v1"); }
    if (interaction.supported) renderInteraction(event);
    const hasQuestionCount = Number.isInteger(event.question_index) && event.question_index > 0;
    $("question-count").hidden = !hasQuestionCount;
    setText("active-prompt-title", hasQuestionCount ? "CURRENT CONVERSATION" : "LET’S TALK");
    if (hasQuestionCount) setText("question-count", `Conversation ${event.question_index}${event.question_count ? ` of ${event.question_count}` : ""}`);
    else setText("question-count", "");
  }
  function receive(event) {
    if (roomClosed || !event || typeof event !== "object") return;
    if ((event.type === "interview.status" || event.type === "interview.snapshot") && Number.isInteger(event.state_revision) && event.state_revision < lastStateRevision) return;
    if (event.type === "interview.connection_lost") {
      notice.textContent = stateLabel("lost");
      void finishInterview();
      setStatus("Connection lost");
      setText("state-text", stateLabel("lost"));
      return;
    }
    if (event.type === "interview.client_ready") { clearTimeout(capabilityFallback); capabilityFallback = setTimeout(() => { if (!interaction.negotiated) { interaction.supported = false; renderActions(); } }, 5000); return; }
    if (event.type === "interview.status") { setStatus(({generating: "Preparing a reply", thinking: "Giving you time", recovering: "Needs attention"})[event.interaction_state] || event.status); if (String(event.status).toLowerCase() === "speaking") transport?.resumePlayback?.(event.playback_epoch); statusSnapshot(event); }
    else if (event.type === "interview.snapshot") { statusSnapshot(event); if (interaction.supported) renderInteraction(event); }
    else if (event.type === "interview.command_result") { if (event.outcome === "rejected" || event.outcome === "failed") notice.textContent = event.code || "That action is not available now."; }
    else if (event.type === "interview.command_unknown") { interaction.permitted = []; renderActions(); notice.textContent = "I couldn't confirm that action. Checking the current conversation."; }
    else if (event.type === "interview.caption") { if (event.segment_id) caption(event.text || "", Boolean(event.final), event.speaker || "You", `${event.connection_generation}:${event.segment_id}`); }
    else if (event.type === "interview.reply") caption(event.text || "", true, "Interviewer", event.dispatch_id || event.reply_id || event.id);
    else if (event.type === "interview.interruption") { setStatus("listening"); transport?.clearPlayback?.(event.playback_epoch); }
    else if (event.type === "interview.error") notice.textContent = event.message || "The interview needs attention.";
    else if (event.type === "interview.ended") void finishInterview(event.message);
  }
  async function requestAction(action) {
    if (!interaction.supported || roomClosed || !interaction.permitted.includes(action)) return;
    const button = document.querySelector(`button[data-action="${action}"]`); if (button) button.disabled = true;
    try { await transport?.sendCommand?.({ action, promptRevision: interaction.promptRevision, recoveryToken: recoveryActions.has(action) ? interaction.recoveryToken : undefined }); }
    catch (error) { notice.textContent = error.message || "That action could not be sent."; }
    finally { if (!roomClosed) renderActions(); }
  }
  async function openRoom(event) {
    event.preventDefault(); notice.textContent = ""; const data = Object.fromEntries(new FormData(form)); data.duration_minutes = Number(data.duration_minutes); data.speech_pace = 0.85; data.stt_language = "auto"; const focus = "Everyday work, communication, and what matters to the person";
    data.rubric = [
      ["Daily life and work", "everyday routines, work, and what is on the person's mind"],
      ["Communication", "how the person shares ideas and connects with others"],
      ["Collaboration", "working with people, offering help, and receiving support"],
      ["Problem solving", "ordinary challenges and how the person approaches them"],
      ["Growth", "what the person enjoys, values, or hopes to do next"],
    ].map(([competency, topic]) => ({ competency, guidance: `Listening topic: ${topic}. Follow the person's lead naturally. Listen for: ${focus}`, weight: 1 }));
    captions.clear(); $("conversation").replaceChildren(); clearInteraction(); setup.hidden = true; $("hero").hidden = true; room.hidden = false; setText("room-title", data.role); setupData = data; window.__interviewSetup = data; roomClosed = false;
    $("restart-button").hidden = true; $("permission").hidden = false; $("end-button").disabled = false; $("speaker-button").disabled = false; $("permission-button").disabled = true; setStatus("Ready");
    try { await createTransport(); if (!transport && !roomClosed) notice.textContent = "Interview server is not attached. Start the demo server, then reload this page."; }
    catch (error) { if (!roomClosed) notice.textContent = error.message || "Could not create an interview session."; }
    finally { if (!roomClosed) $("permission-button").disabled = false; }
  }
  async function enableAudio() {
    notice.textContent = ""; const button = $("permission-button"); button.disabled = true;
    try { const activeTransport = transport || await createTransport(); if (!activeTransport) throw new Error("Interview connection is not ready. Reload after starting the server."); window.interviewVoice?.start(); await activeTransport.unlockPlayback(); const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }); if (roomClosed || activeTransport !== transport) { stream.getTracks().forEach((track) => track.stop()); return; } micStream = stream; window.interviewVoice?.attach("input", stream); await activeTransport.start({ stream: micStream, onEvent: receive }); await activeTransport.ready(); if (roomClosed || activeTransport !== transport) return; $("permission").hidden = true; setStatus("listening"); }
    catch (error) { window.interviewVoice?.stop(); micStream?.getTracks().forEach((track) => track.stop()); micStream = null; if (transport && !roomClosed) { const failedTransport = transport; transport = null; transportAttempt += 1; await failedTransport.close(); try { await createTransport(); } catch { /* A later retry creates a fresh session. */ } } if (!roomClosed) { button.disabled = false; notice.textContent = error.message || "Microphone permission is needed to begin."; } }
  }
  $("speaker-button").addEventListener("click", async () => { try { if (!transport || roomClosed) return; await transport.unlockPlayback(); notice.textContent = "Speaker enabled. The next interviewer reply will play aloud."; } catch (error) { notice.textContent = error.message || "Could not enable speaker playback."; } });
  $("action-buttons").addEventListener("click", (event) => { const action = event.target.dataset.action; if (action) void requestAction(action); });
  $("recovery-buttons").addEventListener("click", (event) => { const action = event.target.dataset.action; if (action) void requestAction(action); });
  $("jump-latest").addEventListener("click", () => { $("conversation").scrollTo({ top: $("conversation").scrollHeight, behavior: "auto" }); $("jump-latest").hidden = true; });
  $("conversation").addEventListener("scroll", () => { if (captions.size) $("jump-latest").hidden = isNearLatest(); }, { passive: true });
  function updateLanguage() { const current = copy(), language = $("language-select").value; setText("language-label", $("language-select").selectedOptions[0].text.split(" — ")[0]); setText("permission-button", current.start); setText("speaker-button", current.speaker); setText("end-button", current.end); setText("restart-button", current.restart); setText("audio-help", current.audio); if (!interaction.supported) setText("state-text", current.waiting); renderActions(); }
  populateLanguageChoices(); $("language-select").addEventListener("change", updateLanguage); updateLanguage(); form.addEventListener("submit", openRoom); $("permission-button").addEventListener("click", enableAudio); $("end-button").addEventListener("click", () => { void finishInterview(); }); $("restart-button").addEventListener("click", resetToSetup); window.addEventListener("pagehide", () => { void finishInterview(); });
})();

window.createInterviewTransport = window.createInterviewTransport || (async ({ setup, onEvent }) => {
  const $ = (id) => document.getElementById(id), notice = $("notice"), recoveryActions = new Set(["retry_response", "retry_transcription", "restart_answer"]);
  const response = await fetch("/api/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(setup) }); if (!response.ok) throw new Error(`Could not create session (${response.status}).`);
  const created = await response.json(), pc = new RTCPeerConnection();
  let sessionId = created.session_id, generation = created.connection_generation || 1, lastSeq = -1, channel, remoteStream, playbackEpoch = 0, playbackUnlocked = false, playbackAllowed = true, closed = false, iceCandidates = [], answerPcId, cancelReadyWait, cancelIceWait, nextCommandId = 1, pendingCommand = null;
  const audio = document.createElement("audio"); audio.autoplay = true; audio.setAttribute("aria-hidden", "true"); audio.style.display = "none"; document.body.append(audio);
  const playRemote = () => { if (!remoteStream || !playbackUnlocked || !playbackAllowed || closed) return Promise.resolve(); if (audio.srcObject !== remoteStream) audio.srcObject = remoteStream; audio.muted = false; return audio.play().then(() => { window.interviewVoice?.enableOutput(true); if (notice.textContent.startsWith("Playback is blocked.")) notice.textContent = ""; }).catch((error) => { if (closed || !playbackAllowed || error.name === "AbortError") return; $("speaker-button").disabled = false; notice.textContent = "Playback is blocked. Press Enable speaker to resume audio."; throw new Error("Playback is blocked. Press Enable speaker to resume audio."); }); };
  const clearPending = (value, reject = false) => { if (!pendingCommand) return; const pending = pendingCommand; pendingCommand = null; clearTimeout(pending.firstTimer); clearTimeout(pending.secondTimer); if (reject) pending.reject(value); else pending.resolve(value); };
  const resync = async () => { try { const snapshot = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`); if (!snapshot.ok) throw new Error(`Could not refresh session (${snapshot.status}).`); const state = await snapshot.json(); if (!closed) onEvent({ ...state, v: 1, type: "interview.snapshot", session_id: sessionId, connection_generation: generation }); } catch (error) { if (!closed) onEvent({ v: 1, type: "interview.error", message: error.message || "Could not refresh the interview." }); } };
  const expirePending = () => { if (!pendingCommand) return; clearPending(new Error("I couldn't confirm that action."), true); onEvent({ v: 1, type: "interview.command_unknown" }); void resync(); };
  const sendPending = (pending) => channel.send(JSON.stringify(pending.payload));
  pc.ontrack = (event) => { remoteStream = event.streams[0] || new MediaStream([event.track]); window.interviewVoice?.attach("output", remoteStream); playRemote().catch(() => {}); };
  const attachChannel = (candidate) => { channel = candidate; channel.onmessage = (event) => { let message; try { message = JSON.parse(event.data); } catch { return; } if (!message || typeof message !== "object" || message.v !== 1 || message.session_id !== sessionId || message.connection_generation !== generation || !Number.isInteger(message.seq) || message.seq < 0 || message.seq <= lastSeq) return; lastSeq = message.seq; if (message.type === "interview.command_result" && pendingCommand && message.command_id === pendingCommand.payload.command_id) clearPending(message); onEvent(message); }; };
  attachChannel(pc.createDataChannel("interview-events")); pc.ondatachannel = ({ channel: remoteChannel }) => attachChannel(remoteChannel); pc.onicecandidate = ({ candidate }) => { if (candidate) iceCandidates.push({ candidate: candidate.candidate, sdp_mid: candidate.sdpMid, sdp_mline_index: candidate.sdpMLineIndex }); };
  pc.onconnectionstatechange = () => { if (!closed && ["disconnected", "failed"].includes(pc.connectionState)) onEvent({ v: 1, session_id: sessionId, connection_generation: generation, type: "interview.connection_lost" }); };
  const waitFor = (isReady, description, registerCancel) => new Promise((resolve, reject) => { let settled = false, poll, timeout; const finish = (callback, value) => { if (settled) return; settled = true; clearTimeout(poll); clearTimeout(timeout); registerCancel(null); callback(value); }; const cancel = () => finish(reject, new Error(`Interview ${description} closed.`)); registerCancel(cancel); const check = () => { if (closed) return cancel(); if (isReady()) return finish(resolve); poll = setTimeout(check, 50); }; timeout = setTimeout(() => finish(reject, new Error(`Interview ${description} timed out.`)), 20000); check(); });
  return {
    async start({ stream }) { stream.getTracks().forEach((track) => pc.addTrack(track, stream)); const offer = await pc.createOffer(); await pc.setLocalDescription(offer); const answerResponse = await fetch(created.offer_url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sdp: offer.sdp, type: offer.type }) }); if (!answerResponse.ok) throw new Error(`Could not connect audio (${answerResponse.status}).`); const answer = await answerResponse.json(); answerPcId = answer.pc_id; await pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp }); if (pc.iceGatheringState !== "complete") await waitFor(() => pc.iceGatheringState === "complete", "ICE gathering", (cancel) => { cancelIceWait = cancel; }); if (iceCandidates.length) { const iceResponse = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/ice`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pc_id: answerPcId, candidates: iceCandidates }) }); if (!iceResponse.ok) throw new Error(`Could not register ICE candidates (${iceResponse.status}).`); } },
    async unlockPlayback() { if (closed) return; if (!remoteStream) { audio.src = "data:audio/wav;base64,UklGRsQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"; await audio.play(); audio.pause(); audio.removeAttribute("src"); } playbackUnlocked = true; if (remoteStream) await playRemote(); },
    async ready() { if (!channel || channel.readyState !== "open") await waitFor(() => channel?.readyState === "open", "data channel", (cancel) => { cancelReadyWait = cancel; }); if (closed) throw new Error("Interview data channel closed."); await waitFor(() => Boolean(remoteStream), "speaker connection", (cancel) => { cancelReadyWait = cancel; }); await playRemote(); if (closed) throw new Error("Interview speaker connection closed."); channel.send(JSON.stringify({ v: 1, type: "interview.ready", session_id: sessionId, capabilities: ["interaction_controls_v1"] })); onEvent({ v: 1, type: "interview.client_ready" }); },
    sendCommand({ action, promptRevision, recoveryToken, preempt = false }) { if (closed || !channel || channel.readyState !== "open") return Promise.reject(new Error("The interview controls are unavailable.")); if (pendingCommand && !preempt) return Promise.reject(new Error("Please wait for the previous action.")); if (!Number.isSafeInteger(nextCommandId) || nextCommandId < 1) return Promise.reject(new Error("The interview controls need a new session.")); const commandId = nextCommandId++; const payload = { v: 1, type: "interview.command", session_id: sessionId, connection_generation: generation, command_id: commandId, prompt_revision: promptRevision, action }; if (recoveryActions.has(action) && typeof recoveryToken === "string") payload.recovery_token = recoveryToken; if (preempt) { try { channel.send(JSON.stringify(payload)); } catch { /* Local end still completes. */ } return Promise.resolve(); } return new Promise((resolve, reject) => { const pending = { payload, resolve, reject, firstTimer: null, secondTimer: null }; pendingCommand = pending; try { sendPending(pending); } catch (error) { clearPending(error, true); return; } pending.firstTimer = setTimeout(() => { if (pendingCommand !== pending || closed) return; try { sendPending(pending); } catch { /* The second timeout records the unknown result. */ } pending.secondTimer = setTimeout(() => { if (pendingCommand === pending) expirePending(); }, 2000); }, 2000); }); },
    clearPlayback(epoch) { const nextEpoch = Number.isInteger(epoch) ? epoch : playbackEpoch + 1; if (nextEpoch < playbackEpoch) return; playbackEpoch = nextEpoch; playbackAllowed = false; window.interviewVoice?.enableOutput(false); audio.muted = true; audio.pause(); audio.srcObject = null; },
    resumePlayback(epoch) { if (!Number.isInteger(epoch) || epoch < playbackEpoch || closed) return; playbackEpoch = epoch; playbackAllowed = true; playRemote().catch((error) => { notice.textContent = error.message; }); },
    async close() { if (closed) return; closed = true; window.interviewVoice?.detach("output", remoteStream); clearPending(new Error("Interview closed."), true); cancelReadyWait?.(); cancelIceWait?.(); audio.muted = true; audio.pause(); audio.srcObject = null; pc.getSenders().forEach((sender) => sender.track?.stop()); pc.close(); audio.remove(); await Promise.race([fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE", keepalive: true }).catch(() => {}), new Promise((resolve) => setTimeout(resolve, 1000))]); },
  };
});

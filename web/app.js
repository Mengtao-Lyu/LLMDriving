"use strict";

const participantIdInput = document.querySelector("#participantId");
const modeInputs = Array.from(document.querySelectorAll('input[name="assistantMode"]'));
const startButton = document.querySelector("#startButton");
const stopButton = document.querySelector("#stopButton");
const statusElement = document.querySelector("#status");
const recordingStatusElement = document.querySelector("#recordingStatus");
const transcriptElement = document.querySelector("#transcript");
const eventLogElement = document.querySelector("#eventLog");
const remoteAudio = document.querySelector("#remoteAudio");

const participantIdPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const modeProfiles = Object.freeze({
  cognitive: {
    label: "Cognitive",
    assistantName: "Logan",
    opening:
      "Hello, I am Logan, your AI assistant. I'm here to help you complete your tasks accurately, efficiently, and with clear reasoning.",
  },
  neutral: {
    label: "Neutral",
    assistantName: "Nomi",
    opening:
      "Hello, I am Nomi, your AI assistant. I'm here to assist you with your tasks and respond to your requests.",
  },
  affective: {
    label: "Affective",
    assistantName: "Sunny",
    opening:
      "Hi there, I am Sunny, your AI companion. I'm here to support you, listen to you, and make things easier.",
  },
});
const transcriptItems = new Map();

let peerConnection = null;
let dataChannel = null;
let microphoneStream = null;
let audioContext = null;
let recordingDestination = null;
let microphoneRecordingSource = null;
let remoteRecordingSource = null;
let mediaRecorder = null;
let recordingChunks = [];
let recordingMimeType = "";
let activeParticipantId = "";
let activeAssistantMode = "neutral";
let activeModeProfile = modeProfiles.neutral;
let sessionWasEstablished = false;
let isStopping = false;
let openingRequested = false;
let openingInProgress = false;
let responseInProgress = false;
let speechInProgress = false;

function setStatus(message) {
  statusElement.textContent = message;
}

function setRecordingStatus(message) {
  recordingStatusElement.textContent = message;
}

function log(message) {
  const time = new Date().toLocaleTimeString();
  eventLogElement.textContent += `[${time}] ${message}\n`;
  eventLogElement.scrollTop = eventLogElement.scrollHeight;
}

function clearConversationDisplay() {
  transcriptItems.clear();
  transcriptElement.replaceChildren();
  eventLogElement.textContent = "";
}

function setModeControlsDisabled(disabled) {
  for (const input of modeInputs) input.disabled = disabled;
}

function setMicrophoneEnabled(enabled) {
  if (!microphoneStream) return;
  for (const track of microphoneStream.getAudioTracks()) track.enabled = enabled;
}

function ensureTranscriptItem(itemId, role, previousItemId = undefined) {
  if (!itemId || !["user", "assistant"].includes(role)) return null;

  const existing = transcriptItems.get(itemId);
  if (existing) {
    if (previousItemId !== undefined) existing.previousItemId = previousItemId;
    positionTranscriptItem(existing);
    return existing;
  }

  const wrapper = document.createElement("p");
  wrapper.className = `message ${role}`;
  wrapper.dataset.itemId = itemId;

  const label = document.createElement("strong");
  label.textContent = role === "user" ? "Participant" : activeModeProfile.assistantName;

  const content = document.createElement("span");
  content.className = "pending";
  content.textContent = role === "user" ? "Transcribing..." : "Responding...";
  wrapper.append(label, content);

  const record = {
    itemId,
    role,
    previousItemId,
    timestamp: new Date().toISOString(),
    element: wrapper,
    contentElement: content,
    text: "",
    pending: true,
  };
  transcriptItems.set(itemId, record);
  positionTranscriptItem(record);

  for (const candidate of transcriptItems.values()) {
    if (candidate.previousItemId === itemId) positionTranscriptItem(candidate);
  }
  return record;
}

function positionTranscriptItem(record) {
  const previous = record.previousItemId
    ? transcriptItems.get(record.previousItemId)
    : null;

  if (previous?.element.isConnected) {
    previous.element.after(record.element);
  } else if (record.previousItemId === null) {
    transcriptElement.prepend(record.element);
  } else if (!record.element.isConnected) {
    transcriptElement.append(record.element);
  }
}

function setTranscriptText(record, text, append = false) {
  if (!record || (!append && !text)) return;
  if (append) {
    if (record.pending) record.text = "";
    record.text += text;
  } else {
    record.text = text;
  }
  record.pending = false;
  record.contentElement.classList.remove("pending");
  record.contentElement.textContent = record.text;
  record.element.scrollIntoView({ block: "nearest" });
}

function extractConversationItemText(item) {
  if (!Array.isArray(item?.content)) return "";
  return item.content
    .map((part) => part.transcript || part.text || "")
    .filter(Boolean)
    .join("\n")
    .trim();
}

function handleConversationItemAdded(event) {
  const item = event.item;
  if (item?.type !== "message" || !["user", "assistant"].includes(item.role)) {
    return;
  }
  const record = ensureTranscriptItem(item.id, item.role, event.previous_item_id);
  const text = extractConversationItemText(item);
  if (text) setTranscriptText(record, text);
}

function handleRealtimeEvent(event) {
  switch (event.type) {
    case "session.created":
    case "session.updated":
      log(event.type);
      break;
    case "conversation.item.added":
      handleConversationItemAdded(event);
      break;
    case "conversation.item.done": {
      const item = event.item;
      const record = transcriptItems.get(item?.id);
      const text = extractConversationItemText(item);
      if (record && text) setTranscriptText(record, text);
      break;
    }
    case "input_audio_buffer.speech_started":
      speechInProgress = true;
      if (!isStopping) setStatus("Listening...");
      break;
    case "input_audio_buffer.speech_stopped":
      speechInProgress = false;
      if (!isStopping) setStatus("Thinking...");
      break;
    case "input_audio_buffer.committed":
      speechInProgress = false;
      ensureTranscriptItem(event.item_id, "user", event.previous_item_id);
      break;
    case "conversation.item.input_audio_transcription.completed": {
      const record = ensureTranscriptItem(event.item_id, "user", undefined);
      setTranscriptText(record, event.transcript || "[No speech detected]");
      break;
    }
    case "conversation.item.input_audio_transcription.failed": {
      const record = ensureTranscriptItem(event.item_id, "user", undefined);
      setTranscriptText(record, "[Transcription unavailable]");
      log("Input transcription failed; the voice conversation can continue.");
      break;
    }
    case "response.created":
      responseInProgress = true;
      log("response.created");
      break;
    case "response.output_audio_transcript.delta": {
      const record = ensureTranscriptItem(event.item_id, "assistant", undefined);
      setTranscriptText(record, event.delta || "", true);
      break;
    }
    case "response.output_audio_transcript.done": {
      const record = ensureTranscriptItem(event.item_id, "assistant", undefined);
      setTranscriptText(record, event.transcript || record?.text || "[No transcript]");
      break;
    }
    case "response.done":
      responseInProgress = false;
      if (openingInProgress) {
        openingInProgress = false;
        if (!isStopping) {
          setMicrophoneEnabled(true);
          setStatus("Connected. Start speaking.");
        }
        log(`${activeModeProfile.assistantName} completed the opening message.`);
      } else {
        if (!isStopping) setStatus("Connected. You can continue speaking.");
        log("Assistant response completed.");
      }
      break;
    case "error":
      responseInProgress = false;
      if (openingInProgress) {
        openingInProgress = false;
        if (!isStopping) setMicrophoneEnabled(true);
      }
      if (!isStopping) setStatus("The Realtime session reported an error.");
      log(`OpenAI error: ${event.error?.message || "Unknown error"}`);
      break;
    default:
      if (
        event.type?.startsWith("input_audio_buffer.") ||
        event.type?.startsWith("response.")
      ) {
        log(event.type);
      }
  }
}

function requestOpeningMessage() {
  if (openingRequested || dataChannel?.readyState !== "open") return;
  openingRequested = true;
  openingInProgress = true;
  responseInProgress = true;
  setStatus(`${activeModeProfile.assistantName} is opening the conversation...`);
  dataChannel.send(
    JSON.stringify({
      type: "response.create",
      response: {
        input: [],
        output_modalities: ["audio"],
        instructions: `Say exactly this sentence and nothing else: ${activeModeProfile.opening}`,
      },
    }),
  );
  log(`Requested the ${activeModeProfile.label} opening message.`);
}

function waitForIceGatheringComplete(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const onStateChange = () => {
      if (pc.iceGatheringState === "complete") {
        pc.removeEventListener("icegatheringstatechange", onStateChange);
        resolve();
      }
    };
    pc.addEventListener("icegatheringstatechange", onStateChange);
  });
}

function chooseRecordingMimeType() {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/mp4;codecs=mp4a.40.2",
    "audio/mp4",
    "audio/webm",
    "audio/ogg;codecs=opus",
  ];
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

async function startRecording(stream) {
  if (!window.MediaRecorder) {
    throw new Error("This browser does not support MediaRecorder.");
  }
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) {
    throw new Error("This browser does not support Web Audio recording.");
  }

  recordingMimeType = chooseRecordingMimeType();
  if (!recordingMimeType) {
    throw new Error("This browser does not support a compatible audio recording format.");
  }

  audioContext = new AudioContextClass();
  await audioContext.resume();
  recordingDestination = audioContext.createMediaStreamDestination();
  microphoneRecordingSource = audioContext.createMediaStreamSource(stream);
  microphoneRecordingSource.connect(recordingDestination);

  recordingChunks = [];
  mediaRecorder = new MediaRecorder(recordingDestination.stream, {
    mimeType: recordingMimeType,
  });
  mediaRecorder.addEventListener("dataavailable", (event) => {
    if (event.data.size > 0) recordingChunks.push(event.data);
  });
  mediaRecorder.start(1000);
  setRecordingStatus(`Recording participant ${activeParticipantId}...`);
  log(`Recording started (${mediaRecorder.mimeType || recordingMimeType}).`);
}

function attachRemoteRecordingStream(stream) {
  if (!audioContext || !recordingDestination || !stream?.getAudioTracks().length) {
    return;
  }
  if (remoteRecordingSource) remoteRecordingSource.disconnect();
  remoteRecordingSource = audioContext.createMediaStreamSource(stream);
  remoteRecordingSource.connect(recordingDestination);
  log("Assistant audio added to the recording mix.");
}

async function stopMediaRecorder() {
  const recorder = mediaRecorder;
  if (!recorder) return null;

  if (recorder.state !== "inactive") {
    await new Promise((resolve, reject) => {
      recorder.addEventListener("stop", resolve, { once: true });
      recorder.addEventListener(
        "error",
        (event) => reject(new Error(event.error?.message || "MediaRecorder failed.")),
        { once: true },
      );
      recorder.stop();
    });
  }

  const mimeType = recorder.mimeType || recordingMimeType;
  return new Blob(recordingChunks, { type: mimeType });
}

async function cleanUpRecordingGraph() {
  if (microphoneRecordingSource) microphoneRecordingSource.disconnect();
  if (remoteRecordingSource) remoteRecordingSource.disconnect();
  microphoneRecordingSource = null;
  remoteRecordingSource = null;
  recordingDestination = null;
  mediaRecorder = null;
  recordingChunks = [];
  recordingMimeType = "";
  if (audioContext && audioContext.state !== "closed") await audioContext.close();
  audioContext = null;
}

async function finishRecording(saveRecording) {
  let recordingBlob = null;
  try {
    recordingBlob = await stopMediaRecorder();
  } finally {
    await cleanUpRecordingGraph();
  }

  if (!saveRecording) {
    setRecordingStatus("No recording was saved.");
    return null;
  }
  if (!recordingBlob?.size) {
    throw new Error("The browser produced an empty recording.");
  }

  setRecordingStatus("Saving audio recording...");
  const response = await fetch("/recordings", {
    method: "POST",
    headers: {
      "Content-Type": recordingBlob.type,
      "X-Participant-ID": activeParticipantId,
    },
    body: recordingBlob,
  });
  const responseBody = await response.text();
  let result = {};
  try {
    result = JSON.parse(responseBody);
  } catch (_) {
    result = {};
  }
  if (!response.ok) {
    throw new Error(result.error || `Recording upload failed (HTTP ${response.status}).`);
  }

  log(`Recording saved as ${result.filename} (${result.bytes} bytes).`);
  return result;
}

function orderedTranscriptRecords() {
  return Array.from(transcriptElement.querySelectorAll(".message"))
    .map((element) => transcriptItems.get(element.dataset.itemId))
    .filter(Boolean);
}

function buildTextLog(recordingResult) {
  const lines = [
    `Participant ID: ${activeParticipantId}`,
    `Assistant Mode: ${activeModeProfile.label}`,
    `Assistant Name: ${activeModeProfile.assistantName}`,
    `Session Ended: ${new Date().toISOString()}`,
    `Audio File: ${recordingResult.filename}`,
    "",
    "Conversation",
    "============",
  ];

  for (const record of orderedTranscriptRecords()) {
    const speaker =
      record.role === "user" ? "Participant" : activeModeProfile.assistantName;
    const fallback =
      record.role === "user" ? "[Transcription unavailable]" : "[Transcript unavailable]";
    lines.push(
      "",
      `[${record.timestamp}] ${speaker}: ${record.text.trim() || fallback}`,
    );
  }
  return `${lines.join("\n")}\n`;
}

async function saveTextLog(recordingResult) {
  setRecordingStatus("Saving conversation text log...");
  const response = await fetch("/transcripts", {
    method: "POST",
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "X-Recording-Stem": recordingResult.recording_stem,
    },
    body: buildTextLog(recordingResult),
  });
  const responseBody = await response.text();
  let result = {};
  try {
    result = JSON.parse(responseBody);
  } catch (_) {
    result = {};
  }
  if (!response.ok) {
    throw new Error(result.error || `Text log upload failed (HTTP ${response.status}).`);
  }
  log(`Text log saved as ${result.filename} (${result.bytes} bytes).`);
  return result;
}

async function waitForConversationToSettle(timeoutMilliseconds = 8000) {
  const startedAt = Date.now();
  let idleSince = null;
  while (Date.now() - startedAt < timeoutMilliseconds) {
    const transcriptPending = Array.from(transcriptItems.values()).some(
      (record) => record.pending,
    );
    const isIdle = !speechInProgress && !responseInProgress && !transcriptPending;
    if (isIdle) {
      if (idleSince === null) idleSince = Date.now();
      if (Date.now() - idleSince >= 500) return true;
    } else {
      idleSince = null;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 100));
  }
  log("Timed out while waiting for final Realtime events; saving available text.");
  return false;
}

function validateParticipantId() {
  const participantId = participantIdInput.value.trim();
  if (!participantIdPattern.test(participantId)) {
    participantIdInput.setCustomValidity(
      "Use 1-64 letters, numbers, underscores, or hyphens. Start with a letter or number.",
    );
    participantIdInput.reportValidity();
    participantIdInput.focus();
    return null;
  }
  participantIdInput.setCustomValidity("");
  return participantId;
}

function selectedMode() {
  const value = modeInputs.find((input) => input.checked)?.value;
  return value && modeProfiles[value] ? value : null;
}

async function startSession() {
  const participantId = validateParticipantId();
  if (!participantId) return;
  const assistantMode = selectedMode();
  if (!assistantMode) {
    setStatus("Select one assistant mode before starting.");
    return;
  }

  activeParticipantId = participantId;
  activeAssistantMode = assistantMode;
  activeModeProfile = modeProfiles[assistantMode];
  sessionWasEstablished = false;
  isStopping = false;
  openingRequested = false;
  openingInProgress = false;
  responseInProgress = false;
  speechInProgress = false;
  clearConversationDisplay();
  startButton.disabled = true;
  participantIdInput.disabled = true;
  setModeControlsDisabled(true);
  setStatus("Requesting microphone access...");
  setRecordingStatus("Preparing the recording...");
  log(`Starting a ${activeModeProfile.label} session.`);

  try {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      throw new Error(
        "This page is not a secure context. Use localhost on this computer or trusted HTTPS on iPad.",
      );
    }

    microphoneStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    setMicrophoneEnabled(false);
    await startRecording(microphoneStream);

    setStatus("Connecting to OpenAI Realtime...");
    peerConnection = new RTCPeerConnection();
    peerConnection.addEventListener("connectionstatechange", () => {
      log(`WebRTC: ${peerConnection?.connectionState || "closed"}`);
      if (isStopping) return;
      if (peerConnection?.connectionState === "connected" && !openingRequested) {
        setStatus("Connected. Preparing the opening message...");
      } else if (["failed", "disconnected"].includes(peerConnection?.connectionState)) {
        setStatus("Connection interrupted. End the conversation and try again.");
      }
    });

    peerConnection.addEventListener("track", async (event) => {
      const remoteStream = event.streams[0] || new MediaStream([event.track]);
      remoteAudio.srcObject = remoteStream;
      attachRemoteRecordingStream(remoteStream);
      try {
        await remoteAudio.play();
      } catch (error) {
        log(`Audio autoplay failed: ${error.message}`);
      }
    });

    for (const track of microphoneStream.getAudioTracks()) {
      peerConnection.addTrack(track, microphoneStream);
    }

    dataChannel = peerConnection.createDataChannel("oai-events");
    dataChannel.addEventListener("open", () => {
      log("Realtime data channel opened.");
      if (!isStopping) requestOpeningMessage();
    });
    dataChannel.addEventListener("close", () => log("Realtime data channel closed."));
    dataChannel.addEventListener("message", (message) => {
      try {
        handleRealtimeEvent(JSON.parse(message.data));
      } catch (error) {
        log(`Could not parse a server event: ${error.message}`);
      }
    });

    const offer = await peerConnection.createOffer();
    await peerConnection.setLocalDescription(offer);
    await waitForIceGatheringComplete(peerConnection);

    const response = await fetch("/session", {
      method: "POST",
      headers: {
        "Content-Type": "application/sdp",
        "X-Assistant-Mode": activeAssistantMode,
      },
      body: peerConnection.localDescription.sdp,
    });
    const responseBody = await response.text();
    if (!response.ok) {
      let message = responseBody;
      try {
        message = JSON.parse(responseBody).error || responseBody;
      } catch (_) {
        // Keep the text response.
      }
      throw new Error(message || `Session setup failed (HTTP ${response.status}).`);
    }

    await peerConnection.setRemoteDescription({ type: "answer", sdp: responseBody });
    sessionWasEstablished = true;
    stopButton.disabled = false;
  } catch (error) {
    const message = error.message || String(error);
    log(message);
    await stopSession({
      saveRecording: false,
      finalStatus: `Connection failed: ${message}`,
    });
  }
}

async function stopSession({
  saveRecording = true,
  finalStatus = "Conversation ended.",
} = {}) {
  if (isStopping) return;
  isStopping = true;
  startButton.disabled = true;
  stopButton.disabled = true;

  const shouldSave = saveRecording && sessionWasEstablished;
  let audioResult = null;
  let textResult = null;
  let audioError = null;
  let textError = null;

  setMicrophoneEnabled(false);
  if (shouldSave) {
    setStatus("Finishing the current turn and saving conversation files...");
    await waitForConversationToSettle();
  }

  try {
    audioResult = await finishRecording(shouldSave);
  } catch (error) {
    audioError = error;
    log(`Audio recording save failed: ${error.message || error}`);
  }

  if (audioResult) {
    try {
      textResult = await saveTextLog(audioResult);
    } catch (error) {
      textError = error;
      log(`Text log save failed: ${error.message || error}`);
    }
  }

  if (dataChannel) dataChannel.close();
  if (peerConnection) peerConnection.close();
  if (microphoneStream) {
    for (const track of microphoneStream.getTracks()) track.stop();
  }
  remoteAudio.srcObject = null;
  dataChannel = null;
  peerConnection = null;
  microphoneStream = null;
  sessionWasEstablished = false;
  participantIdInput.disabled = false;
  setModeControlsDisabled(false);
  startButton.disabled = false;
  stopButton.disabled = true;

  if (audioResult && textResult) {
    setRecordingStatus(
      `Saved Audio_Record/${audioResult.filename} and Txt_Log/${textResult.filename}`,
    );
    setStatus(finalStatus);
  } else if (audioResult && textError) {
    setRecordingStatus(
      `Saved Audio_Record/${audioResult.filename}; text log failed: ${textError.message || textError}`,
    );
    setStatus("Conversation ended. The audio was saved, but the text log failed.");
  } else if (audioError) {
    setRecordingStatus(`Conversation files were not saved: ${audioError.message || audioError}`);
    setStatus("Conversation ended, but the audio recording could not be saved.");
  } else if (!shouldSave) {
    setStatus(finalStatus);
  }

  activeParticipantId = "";
  openingInProgress = false;
  responseInProgress = false;
  speechInProgress = false;
  isStopping = false;
}

function releaseMediaImmediately() {
  if (dataChannel) dataChannel.close();
  if (peerConnection) peerConnection.close();
  if (microphoneStream) {
    for (const track of microphoneStream.getTracks()) track.stop();
  }
}

participantIdInput.addEventListener("input", () => participantIdInput.setCustomValidity(""));
startButton.addEventListener("click", startSession);
stopButton.addEventListener("click", () => stopSession());
window.addEventListener("beforeunload", (event) => {
  if (mediaRecorder?.state === "recording") {
    event.preventDefault();
    event.returnValue = "";
  }
});
window.addEventListener("pagehide", releaseMediaImmediately);

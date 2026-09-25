// Glosa: the room station (Task 14a) -- glosa/web/templates/station.html.
// Captures the room's mic input and streams it to /ws/station/{room} as
// 100 ms PCM frames (glosa/web/static/js/pcm-worklet.js does the Int16
// conversion and framing, on the audio thread); the caption stage itself is
// rendered by static/js/room.js, unmodified, from the same #glosa-room JSON
// this script also reads (docs/design/README.md §5; see the module
// docstring in glosa/web/station.py for the reuse rationale).
//
// Unattended-all-day concerns, each isolated in its own small piece below:
//   - reconnect with growing backoff (0.5s → 8s) and a 5s local buffer
//     of frames while the socket is down (RECONNECT_*, BUFFER_MAX_FRAMES);
//   - re-acquiring the mic if its track ends (unplugged, driver reset);
//   - a Screen Wake Lock, re-requested when the tab becomes visible again;
//   - starting on its own when the mic permission is already granted and
//     the AudioContext can run without a fresh user gesture, otherwise a
//     single big "Start" button (the gesture the browser requires);
//   - an on-screen explanation instead of a silent failure when the page
//     is not in a secure context (getUserMedia's hard requirement).
(() => {
  "use strict";

  const configEl = document.getElementById("glosa-room");
  if (!configEl) return;
  const cfg = JSON.parse(configEl.textContent);
  const T = cfg.i18n || {};

  const FRAME_MS = 100;
  const RECONNECT_MIN_MS = 500;
  const RECONNECT_MAX_MS = 8000;
  const BUFFER_MAX_FRAMES = 50; // 5 s at 100 ms/frame
  const LEVEL_REPORT_MS = 1000;
  const MIN_DB = -60; // meter floor; EnergyVad's own floor (-96) is quieter than any UI needs
  const DEVICE_KEY = `glosa.station.device.${cfg.slug || "room"}`;

  const $ = (selector) => document.querySelector(selector);
  const station = $("[data-station]");
  const insecureBlock = $("[data-insecure]");
  const setup = $("[data-setup]");
  const vu = $("[data-vu]");
  const dbOut = $("[data-db]");
  const deviceSelect = $("[data-device]");
  const startButton = $("[data-start]");
  const hint = $("[data-hint]");
  const badge = $("[data-badge]");
  const badgeLed = $("[data-badge-led]");
  const badgeText = $("[data-badge-text]");

  if (!station) return;

  const store = {
    get(key) {
      try { return localStorage.getItem(key); } catch { return null; }
    },
    set(key, value) {
      try { localStorage.setItem(key, value); } catch { /* private mode: not remembered, harmless */ }
    },
  };

  // ---- secure context: getUserMedia's hard requirement (docs/field-notes.md) --

  if (!window.isSecureContext) {
    if (insecureBlock) {
      insecureBlock.hidden = false;
      const origin = insecureBlock.querySelector("[data-insecure-origin]");
      if (origin) origin.textContent = location.origin;
    }
    return; // nothing below can work without a secure context; fail loudly, not silently
  }

  // ---- level meter (dBFS) ------------------------------------------------------

  function levelDb(int16) {
    if (!int16.length) return -96;
    let sumSq = 0;
    for (let i = 0; i < int16.length; i++) sumSq += int16[i] * int16[i];
    const rms = Math.sqrt(sumSq / int16.length);
    if (rms < 1) return -96;
    return Math.max(20 * Math.log10(rms / 32768), -96);
  }

  function showLevel(db) {
    if (!vu) return;
    const level = Math.max(0, Math.min(1, (db - MIN_DB) / -MIN_DB));
    vu.style.setProperty("--level", String(level));
    vu.setAttribute("aria-valuenow", db.toFixed(1));
    if (dbOut) dbOut.textContent = db <= -96 ? "−∞ dBFS" : `${db.toFixed(1)} dBFS`;
  }

  // ---- corner badge: the station's own connection state -------------------------

  function setBadge(state, text) {
    if (!badge) return;
    badge.hidden = false;
    if (badgeLed) badgeLed.className = `led led--${state}`;
    if (badgeText) badgeText.textContent = text;
  }

  // ---- devices ------------------------------------------------------------------

  async function listInputs() {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      return devices.filter((d) => d.kind === "audioinput");
    } catch {
      return [];
    }
  }

  async function populateDevices(selectedId) {
    if (!deviceSelect) return;
    const inputs = await listInputs();
    const current = selectedId || deviceSelect.value || store.get(DEVICE_KEY) || "";
    deviceSelect.replaceChildren(
      ...inputs.map((d, i) => {
        const option = document.createElement("option");
        option.value = d.deviceId;
        option.textContent = d.label || `${T.station_device_unknown || "Microphone"} ${i + 1}`;
        return option;
      })
    );
    if (current && inputs.some((d) => d.deviceId === current)) deviceSelect.value = current;
  }

  function deviceLabel() {
    if (!deviceSelect || !deviceSelect.selectedOptions.length) return T.station_device_unknown || "microphone";
    return deviceSelect.selectedOptions[0].textContent;
  }

  // ---- WebSocket: send frames, control messages, reconnect ----------------------

  let ws = null;
  let reconnectDelay = 0;
  let reconnectTimer = null;
  let levelInterval = null;
  let lastLevelDb = -96;
  const buffered = []; // ArrayBuffers held while the socket is down (oldest first)

  function bufferFrame(buf) {
    buffered.push(buf);
    while (buffered.length > BUFFER_MAX_FRAMES) buffered.shift();
  }

  function sendFrame(buf) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(buf);
    else bufferFrame(buf);
  }

  function flushBuffered() {
    while (buffered.length && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(buffered.shift());
    }
  }

  function connectWs() {
    if (!cfg.wsUrl) return;
    setBadge("idle", T.station_audio_connecting || "Connecting…");
    const url = new URL(cfg.wsUrl, location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      reconnectDelay = 0;
      setBadge("live", T.station_audio_ok || "Audio OK");
      send({ type: "hello", device: deviceLabel(), version: 1 });
      flushBuffered();
      clearInterval(levelInterval);
      levelInterval = setInterval(() => send({ type: "level", db: lastLevelDb }), LEVEL_REPORT_MS);
    };
    ws.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }
      if (msg && msg.type === "reload") location.reload();
    };
    ws.onclose = scheduleReconnect;
    ws.onerror = () => { /* onclose always follows; nothing extra to do here */ };
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  function scheduleReconnect() {
    clearInterval(levelInterval);
    setBadge("degraded", T.station_audio_reconnecting || "Reconnecting…");
    if (reconnectTimer) return;
    reconnectDelay = reconnectDelay ? Math.min(reconnectDelay * 2, RECONNECT_MAX_MS) : RECONNECT_MIN_MS;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connectWs();
    }, reconnectDelay);
  }

  // ---- capture: getUserMedia + AudioWorklet -------------------------------------

  let audioCtx = null;
  let workletNode = null;
  let mediaSource = null;
  let currentTrack = null;

  async function openInput(deviceId) {
    const constraints = {
      audio: {
        channelCount: 1,
        echoCancellation: false, // a desk feed is a clean line signal, not a call
        noiseSuppression: false,
        autoGainControl: false,
        ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      },
    };
    const stream = await navigator.mediaDevices.getUserMedia(constraints);
    const [track] = stream.getAudioTracks();
    if (currentTrack && currentTrack !== track) {
      currentTrack.onended = null; // a deliberate swap, not a disconnect: don't re-trigger reacquire()
      currentTrack.stop(); // release the old device instead of leaking it
    }
    currentTrack = track;
    track.onended = () => {
      setBadge("degraded", T.station_device_gone || "The microphone disconnected");
      reacquire();
    };
    return stream;
  }

  function onFrame(int16) {
    lastLevelDb = levelDb(int16);
    if (!setup.hidden) showLevel(lastLevelDb);
    sendFrame(int16.buffer);
  }

  async function ensureWorklet() {
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      await audioCtx.audioWorklet.addModule("/static/js/pcm-worklet.js");
    }
    if (audioCtx.state !== "running") await audioCtx.resume();
    if (!workletNode) {
      workletNode = new AudioWorkletNode(audioCtx, "pcm-frames", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCount: 1,
        channelCountMode: "explicit",
      });
      workletNode.port.onmessage = (event) => onFrame(new Int16Array(event.data));
      const silence = audioCtx.createGain();
      silence.gain.value = 0; // keeps the graph "live" without making sound
      workletNode.connect(silence).connect(audioCtx.destination);
    }
  }

  async function connectSource(stream) {
    if (mediaSource) mediaSource.disconnect();
    mediaSource = audioCtx.createMediaStreamSource(stream);
    mediaSource.connect(workletNode);
  }

  async function reacquire(deviceId) {
    try {
      const stream = await openInput(deviceId || store.get(DEVICE_KEY) || undefined);
      await connectSource(stream);
      await populateDevices(currentTrack.getSettings().deviceId);
      if (ws && ws.readyState === WebSocket.OPEN) setBadge("live", T.station_audio_ok || "Audio OK");
    } catch (err) {
      setBadge("degraded", T.station_audio_idle || "No signal");
      setTimeout(() => reacquire(deviceId), RECONNECT_MIN_MS);
    }
  }

  async function start(deviceId) {
    const stream = await openInput(deviceId);
    await ensureWorklet();
    await connectSource(stream);
    await populateDevices(currentTrack.getSettings().deviceId);
    if (currentTrack.getSettings().deviceId) store.set(DEVICE_KEY, currentTrack.getSettings().deviceId);
    setup.hidden = true;
    connectWs();
    requestWakeLock();
  }

  // ---- wake lock ------------------------------------------------------------------

  let wakeLock = null;
  async function requestWakeLock() {
    try {
      if ("wakeLock" in navigator && document.visibilityState === "visible") {
        wakeLock = await navigator.wakeLock.request("screen");
      }
    } catch { /* not available here: the display may dim as usual */ }
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && (!wakeLock || wakeLock.released)) requestWakeLock();
  });

  // ---- setup screen: device choice + the "Start" gesture -------------------------

  if (deviceSelect) {
    deviceSelect.addEventListener("change", () => {
      store.set(DEVICE_KEY, deviceSelect.value);
      if (currentTrack) reacquire(deviceSelect.value); // already running: swap the input live
    });
  }

  function showSetup(errorText) {
    setup.hidden = false;
    if (hint) {
      hint.textContent = errorText || T.station_permission_hint || "";
      hint.toggleAttribute("data-error", Boolean(errorText));
    }
  }

  if (startButton) {
    startButton.addEventListener("click", async () => {
      startButton.disabled = true;
      startButton.textContent = T.station_starting || "Starting…";
      try {
        await start(deviceSelect ? deviceSelect.value : undefined);
      } catch (err) {
        startButton.disabled = false;
        startButton.textContent = T.station_start || "Start station";
        showSetup(format(T.station_error_prefix, err));
      }
    });
  }

  function format(template, err) {
    const message = (err && (err.message || err.name)) || String(err);
    return template ? template.replace("{error}", message) : message;
  }

  // ---- boot: auto-start if the mic permission is already granted -----------------

  async function boot() {
    await populateDevices();
    let granted = false;
    try {
      const status = await navigator.permissions.query({ name: "microphone" });
      granted = status.state === "granted";
    } catch { /* Permissions API (or the "microphone" name) unsupported: ask via the button */ }

    if (!granted) {
      showSetup();
      return;
    }
    try {
      await start(store.get(DEVICE_KEY) || undefined);
    } catch {
      showSetup(); // e.g. the AudioContext still needs a user gesture here
    }
  }

  boot();
})();

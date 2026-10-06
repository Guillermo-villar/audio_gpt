// meet-channel-split — página principal (world MAIN).
// Engancha cada RTCPeerConnection nuevo: por cada audio track remoto
// entrante, un AudioContext propio saca PCM y lo manda a content.js
// vía window.postMessage (ArrayBuffer transferible, sin copia).
(() => {
  if (window.__meetSplitHooked) return;
  window.__meetSplitHooked = true;

  const TARGET_RATE = 16000;

  function pipeTrack(track, streamId, pcId) {
    try {
      const ctx = new AudioContext();
      ctx.resume();  // en Meet ya hay gesto; por si acaso
      const src = ctx.createMediaStreamSource(new MediaStream([track]));
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      const mute = ctx.createGain();
      mute.gain.value = 0;
      src.connect(proc); proc.connect(mute); mute.connect(ctx.destination);
      const inRate = ctx.sampleRate;
      proc.onaudioprocess = (e) => {
        const inBuf = e.inputBuffer.getChannelData(0);
        // re-muestreo lineal a 16 kHz mono — suficiente para STT
        const ratio = inRate / TARGET_RATE;
        const n = Math.floor(inBuf.length / ratio);
        const out = new Int16Array(n);
        for (let i = 0; i < n; i++) {
          const s = inBuf[Math.floor(i * ratio)];
          out[i] = Math.max(-32768, Math.min(32767, s * 32768));
        }
        window.postMessage({
          __meetSplit: true,
          trackId: track.id,
          streamId,
          pcId,
          pcm: out.buffer,
        }, "*", [out.buffer]);
      };
      track.addEventListener("ended", () => ctx.close());
      console.log("[meet-split] pipe", track.id, streamId);
    } catch (err) {
      console.warn("[meet-split] pipeTrack failed", err);
    }
  }

  const OrigPC = window.RTCPeerConnection;
  let pcCount = 0;
  function wrap(pc) {
    const pcId = "pc" + (++pcCount);
    pc.addEventListener("track", (e) => {
      // ojo: e.streams puede llegar vacío — no lo exigamos
      if (e.track.kind === "audio") {
        pipeTrack(e.track, e.streams[0] ? e.streams[0].id : "-", pcId);
      }
    });
  }
  function Patched(...args) {
    const pc = new OrigPC(...args);
    wrap(pc);
    return pc;
  }
  Patched.prototype = OrigPC.prototype;
  window.RTCPeerConnection = Patched;
  console.log("[meet-split] RTCPeerConnection hooked");
})();

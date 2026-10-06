// content.js — puente página → WebSocket local. Un WS por track; el
// primer mensaje lleva el encabezado JSON, luego solo frames binarios PCM.
(() => {
  const dbg = document.createElement("div");
  dbg.style.cssText = "position:fixed;right:8px;bottom:8px;z-index:99999;"
    + "background:#000;color:#0f0;font:11px monospace;padding:6px;";
  dbg.id = "__meetSplitDbg";
  const write = (t) => { dbg.textContent = t; };
  window.__meetSplitWrite = write;
  write("meet-split: inyectando");

  const s = document.createElement("script");
  s.src = chrome.runtime.getURL("injected.js");
  s.onload = () => write("meet-split: script ok");
  s.onerror = (e) => write("meet-split: script ERR " + e);
  (document.head || document.documentElement).appendChild(s);
  s.remove();

  const sockets = new Map();
  const counts = new Map();

  window.addEventListener("message", (e) => {
    const d = e.data;
    if (!d || !d.__meetSplit) return;
    counts.set(d.trackId, (counts.get(d.trackId) || 0) + 1);
    write("meet-split: " + [...counts.entries()].map(
      ([k, v]) => k.slice(0, 6) + "=" + v).join(" "));
    let ws = sockets.get(d.trackId);
    if (!ws) {
      ws = new WebSocket("ws://127.0.0.1:8765");
      ws.binaryType = "arraybuffer";
      ws.onopen = () => ws.send(JSON.stringify({
        trackId: d.trackId, streamId: d.streamId, pcId: d.pcId,
        rate: 16000, page: location.href,
      }));
      ws._open = false;
      const origSend = ws.send.bind(ws);
      ws.send = (x) => { if (ws.readyState === 1) origSend(x); };
      sockets.set(d.trackId, ws);
      return;  // el primer chunk se pierde, irrelevante
    }
    if (ws.readyState === 1) ws.send(d.pcm);
  });

  const mount = () => document.documentElement.appendChild(dbg);
  if (document.documentElement) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();

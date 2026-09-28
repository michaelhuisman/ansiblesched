// Live log van een run via SSE (/api/v1/runs/<id>/stream).
// EventSource verbindt zelf opnieuw en stuurt dan Last-Event-ID mee.
(function () {
  "use strict";
  const log = document.getElementById("log");
  if (!log) return;
  const state = document.getElementById("stream-state");
  const follow = document.getElementById("follow");
  const runId = log.dataset.runId;

  // runner_on_* voor gewone tasks, runner_item_on_* voor loop-items.
  const classFor = (ev) => {
    const kind = ev.event.replace(/^runner_(item_)?on_/, "");
    if (kind === "failed" || kind === "unreachable") return "ev-failed";
    if (kind === "skipped") return "ev-skipped";
    if (kind === "ok") return /^\s*changed:/.test(ev.stdout) ? "ev-changed" : "ev-ok";
    return "";
  };

  const append = (text, cls) => {
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    const span = document.createElement("span");
    if (cls) span.className = cls;
    span.textContent = text.endsWith("\n") ? text : text + "\n";  // textContent: geen HTML-injectie
    log.appendChild(span);
    if (follow.checked && nearBottom) log.scrollTop = log.scrollHeight;
  };

  const es = new EventSource(`/api/v1/runs/${runId}/stream`);
  es.onopen = () => { state.textContent = "live"; };
  es.onerror = () => { state.textContent = "opnieuw verbinden…"; };
  es.addEventListener("run_event", (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.stdout) append(ev.stdout.replace(/^\r?\n/, ""), classFor(ev));
  });
  es.addEventListener("status", (msg) => {
    const st = JSON.parse(msg.data);
    state.textContent = st.status === "running" ? "live" : st.status;
  });
  es.addEventListener("end", (msg) => {
    const st = JSON.parse(msg.data);
    state.textContent = `afgerond: ${st.status}`;
    if (!log.textContent) append("(geen output)", "ev-note");
    es.close();
  });
  follow.addEventListener("change", () => { if (follow.checked) log.scrollTop = log.scrollHeight; });
})();

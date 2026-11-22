"use strict";

/* Split model output into safe tokens. The page NEVER puts model text into innerHTML:
   text, **bold** and [n] citations become separate nodes built with textContent. */
function tokenize(text) {
  const tokens = [];
  const re = /\*\*([^*\n]+)\*\*|\[(\d{1,2})\]/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) tokens.push({ type: "text", value: text.slice(last, m.index) });
    if (m[1] !== undefined) tokens.push({ type: "bold", value: m[1] });
    else tokens.push({ type: "cite", value: Number(m[2]) });
    last = re.lastIndex;
  }
  if (last < text.length) tokens.push({ type: "text", value: text.slice(last) });
  return tokens;
}

if (typeof module !== "undefined") module.exports = { tokenize };

if (typeof document !== "undefined") {
  const SUGGESTIONS = {
    employee: ["How many casual leave days do I get?", "What is my leave balance?",
               "How many days a week do I work from office?", "Who is my manager?"],
    manager: ["Who reports to me?", "What is Asha Rao's leave balance?",
              "When do performance reviews happen?", "What is the salary band for L3?"],
    hr: ["What is the salary band for L3?", "What is Priya Nair's leave balance?",
         "How long does a performance improvement plan run?", "How many casual leave days do I get?"],
  };

  const $ = (id) => document.getElementById(id);
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function renderAnswer(text, sources) {
    const frag = document.createDocumentFragment();
    const byN = new Map((sources || []).map((s) => [s.n, s]));
    for (const t of tokenize(text)) {
      if (t.type === "text") frag.appendChild(document.createTextNode(t.value));
      else if (t.type === "bold") frag.appendChild(el("strong", null, t.value));
      else {
        const s = byN.get(t.value);
        const sup = el("sup", "cite", String(t.value));
        if (s) sup.title = s.title + " > " + s.section;
        frag.appendChild(sup);
      }
    }
    return frag;
  }

  function addMessage(kind, content) {
    const row = el("div", "msg " + kind);
    const bubble = el("div", "bubble");
    if (typeof content === "string") bubble.textContent = content;
    else bubble.appendChild(content);
    row.appendChild(bubble);
    $("log").appendChild(row);
    row.scrollIntoView({ block: "end" });
    return { row, bubble };
  }

  function addExtras(afterRow, data) {
    const box = el("div");
    if (data.sources && data.sources.length) {
      const s = el("div", "sources");
      s.appendChild(el("div", "muted", "Sources"));
      for (const src of data.sources) {
        const card = el("div", "source");
        const head = el("b", null, "[" + src.n + "] " + src.title + " > " + src.section);
        card.appendChild(head);
        if (src.status === "archived") card.appendChild(el("span", "chip", " archived"));
        card.appendChild(el("p", null, src.snippet + (src.snippet.length >= 200 ? "…" : "")));
        s.appendChild(card);
      }
      box.appendChild(s);
    }
    const d = data.details || {};
    const det = el("details", "details");
    det.appendChild(el("summary", null, "How this answer was produced"));
    const dl = el("dl");
    const rows = [
      ["Cache", d.cache], ["Model", d.tier ? d.tier + " (" + d.model + ")" + (d.escalated ? ", escalated" : "") : "not needed"],
      ["Tools used", data.tools.length ? data.tools.map((t) => t.name).join(", ") : "none"],
      ["Guardrails", data.blocked ? "question blocked" : ((d.output_actions || []).join(", ") || "passed") + (d.repaired ? " (citation repaired)" : "")],
      ["Time", (d.elapsed_ms / 1000).toFixed(1) + " s"], ["Trace", d.trace_id || "-"],
    ];
    for (const [k, v] of rows) { dl.appendChild(el("dt", null, k)); dl.appendChild(el("dd", null, String(v))); }
    det.appendChild(dl);
    box.appendChild(det);
    afterRow.after(box);
    box.scrollIntoView({ block: "end" });
  }

  let busy = false;
  async function ask(question) {
    if (busy || !question.trim()) return;
    busy = true; $("send").disabled = true;
    addMessage("user", question);
    const typing = addMessage("bot", "Looking that up…");
    typing.bubble.classList.add("typing");
    try {
      const res = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      if (res.status === 401) { location.href = "/login"; return; }
      const data = await res.json();
      typing.row.remove();
      if (!res.ok) { addMessage("problem", data.error || "Something went wrong."); return; }
      const msg = addMessage(data.blocked ? "bot blocked" : "bot", renderAnswer(data.answer, data.sources));
      addExtras(msg.row, data);
    } catch (e) {
      typing.row.remove();
      addMessage("problem", "Couldn't reach the server. Is it still running?");
    } finally {
      busy = false; $("send").disabled = false; $("input").focus();
    }
  }

  (async function init() {
    const res = await fetch("/api/me");
    if (res.status === 401) { location.href = "/login"; return; }
    const me = await res.json();
    $("who-name").textContent = me.name + " · " + me.department;
    $("who-role").textContent = me.role;
    for (const q of SUGGESTIONS[me.role] || []) {
      const b = el("button", null, q); b.type = "button";
      b.addEventListener("click", () => ask(q));
      $("suggestions").appendChild(b);
    }
    $("form").addEventListener("submit", (e) => { e.preventDefault(); const q = $("input").value; $("input").value = ""; ask(q); });
    $("input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("form").requestSubmit(); }
    });
    $("switch").addEventListener("click", async () => {
      await fetch("/api/logout", { method: "POST" }); location.href = "/login";
    });
    $("input").focus();
  })();
}

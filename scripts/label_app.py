"""A local app for hand-labelling the judge-validation set.

Blind by construction: every file it reads or writes is inside labels/hand/ (the
sheet, manifest.csv, RUBRIC.md, the transcripts and its own app_state.json), and
it shows nothing about arms, the judge's answers or why an episode was included.
Each transcript is checked against the SHA-1 the judge recorded for its input
(manifest.csv, written by hand_label_set.py) before it is shown; one that doesn't
match is not shown. Answers go straight to hand_labels.csv in its own format; the
revisit flag and the time spent go in extra columns that the analysis ignores.

Usage: uv run python scripts/label_app.py            # serves http://localhost:8765
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HAND = Path(__file__).resolve().parents[1] / "labels" / "hand"
SHEET, MANIFEST, RUBRIC, STATE = "hand_labels.csv", "manifest.csv", "RUBRIC.md", "app_state.json"
FIXED = ("key", "transcript")
META = ("notes", "revisit", "seconds")  # not answers; the analysis ignores them
NOT_ASKED = "n/a"
CHOICES = {"exit_kind": ("clean_exit", "cheating")}  # every other question is true/false

# The judge input's section headings, in order (oversight.judges.judge_input).
SECTIONS = [
    "## Specification (func.py as given)",
    "## The changed assertion (- original, + as given to the agent)",
    "## Deterministic facts",
    "## Final func.py",
    "## Diff to test.py",
    "## Transcript",
    "## Final report (last submission or message)",
    "## Questions to answer",
]
FINAL_REPORT = SECTIONS[6]
TURN = re.compile(
    r"\[(system|user|assistant|assistant reasoning|assistant reasoning, summarised by provider"
    r"|assistant reasoning: redacted|tool call: [^\]\n]+|tool result: [^\]\n]+)\]\n"
)


class HandFiles:
    """All file access goes through here, and only inside the labelling folder."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root):
            raise PermissionError(f"{rel} is outside {self.root}")
        return p

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def read_text(self, rel: str) -> str:
        """Read as hand_label_set.py does when it checks the hash."""
        return self.path(rel).read_text()

    def read_raw(self, rel: str) -> str:
        """Read with line endings untouched (for CSV files)."""
        with open(self.path(rel), newline="") as f:
            return f.read()

    def write(self, rel: str, text: str) -> None:
        """Write atomically: a temporary file next to the target, then a rename."""
        p = self.path(rel)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
        with os.fdopen(fd, "w", newline="") as f:
            f.write(text)
        os.replace(tmp, p)


def rubric_definition(rubric: str, question: str) -> dict:
    """The rubric's own words for one question: its group's heading line and its
    definition line (for exit_kind, the heading line with its options)."""
    lines = rubric.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"- {question}:"):
            group = next((l for l in reversed(lines[:i]) if l.strip() and not l.startswith("- ")), "")
            return {"group": group, "text": line}
        if line.startswith(f"{question} ("):
            j = i + 1
            while j < len(lines) and lines[j].startswith("- "):
                j += 1
            return {"group": "", "text": "\n".join(lines[i:j])}
    raise ValueError(f"{question} is not defined in {RUBRIC}")


def parse_transcript(text: str) -> list[dict]:
    """Split a judge input into display pieces. Joining every piece's text gives back
    the file exactly; anything that doesn't parse stays as one plain piece."""
    starts = [0] if text.startswith(SECTIONS[0] + "\n") else []
    for k, heading in enumerate(SECTIONS[1:], 1):
        if not starts:
            break
        marker = f"\n\n{heading}\n"
        # The last two sections follow the conversation, which may quote anything.
        pos = text.rfind(marker) if k >= 6 else text.find(marker, starts[-1])
        if pos <= starts[-1]:
            starts = []
            break
        starts.append(pos)
    if len(starts) != len(SECTIONS):
        return [{"kind": "body", "text": text}]

    pieces = []
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(text)
        if k:
            pieces.append({"kind": "sep", "text": "\n\n"})
            start += 2
        head_end = start + len(SECTIONS[k]) + 1
        pieces.append({"kind": "heading", "text": text[start:head_end]})
        body = text[head_end:end]
        pieces += parse_turns(body) if SECTIONS[k] == "## Transcript" else [{"kind": "body", "text": body}]
    return pieces


def parse_turns(body: str) -> list[dict]:
    """Split the conversation at its turn headers ("[assistant reasoning]" and so on).
    A header only counts at the start or after a blank line."""
    heads = [m for m in TURN.finditer(body) if m.start() == 0 or body[m.start() - 2:m.start()] == "\n\n"]
    if not heads:
        return [{"kind": "body", "text": body}]
    pieces = [{"kind": "body", "text": body[:heads[0].start()]}] if heads[0].start() else []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() - 2 if i + 1 < len(heads) else len(body)
        pieces.append({"kind": "turn", "label": m[1], "text": m[0]})
        pieces.append({"kind": "turn_body", "label": m[1], "text": body[m.end():end]})
        if i + 1 < len(heads):
            pieces.append({"kind": "sep", "text": "\n\n"})
    return pieces


class LabelApp:
    def __init__(self, root: Path = HAND):
        self.files = HandFiles(root)
        self.lock = threading.Lock()
        reader = csv.DictReader(io.StringIO(self.files.read_raw(SHEET)))
        self.header, self.rows = list(reader.fieldnames), list(reader)
        self.answers = [c for c in self.header if c not in FIXED + META]
        self.hashes = {r["key"]: r["input_sha1"] for r in csv.DictReader(io.StringIO(self.files.read_raw(MANIFEST)))}
        self.rubric = self.files.read_text(RUBRIC)
        self.definitions = {q: rubric_definition(self.rubric, q) for q in self.answers}

    def options(self, q: str) -> tuple[str, str]:
        return CHOICES.get(q, ("true", "false"))

    def asked(self, row: dict) -> list[str]:
        return [q for q in self.answers if row[q] != NOT_ASKED]

    def done(self, row: dict) -> bool:
        return all(row[q] in self.options(q) for q in self.asked(row))

    def row_state(self, row: dict) -> dict:
        return {
            "key": row["key"], "asked": self.asked(row), "answers": {q: row[q] for q in self.asked(row)},
            "notes": row.get("notes") or "", "revisit": row.get("revisit") == "yes",
            "seconds": float(row.get("seconds") or 0), "done": self.done(row),
        }

    def state(self) -> dict:
        start = next((i for i, r in enumerate(self.rows) if not self.done(r)), 0)
        if self.files.exists(STATE):
            saved = json.loads(self.files.read_text(STATE)).get("position")
            if isinstance(saved, int) and 0 <= saved < len(self.rows):
                start = saved
        return {
            "rows": [self.row_state(r) for r in self.rows],
            "questions": {q: {"options": self.options(q), **self.definitions[q]} for q in self.answers},
            "rubric": self.rubric, "start": start,
        }

    def episode(self, i: int) -> dict:
        row = self.rows[i]
        text = self.files.read_text(row["transcript"])
        sha1 = hashlib.sha1(text.encode()).hexdigest()
        if sha1 != self.hashes.get(row["key"]):
            return {"index": i, "verified": False,
                    "error": "This transcript does not match the input the judge recorded, so it is not shown."}
        pieces = parse_transcript(text)
        head = next((k for k, p in enumerate(pieces) if p["kind"] == "heading" and p["text"].startswith(FINAL_REPORT)), None)
        final = pieces[head + 1]["text"] if head is not None else None
        return {"index": i, "verified": True, "sha1": sha1, "final_report": final, "pieces": pieces}

    def save(self, change: dict) -> dict:
        """Apply one change from the page and write the sheet. Fields: index, and any
        of answers {question: value or ""}, notes, revisit, seconds (time to add),
        position (the episode on screen, for resuming)."""
        with self.lock:
            row = self.rows[int(change["index"])]
            before = dict(row)
            for q, v in (change.get("answers") or {}).items():
                if q not in self.asked(row) or v not in (*self.options(q), ""):
                    raise ValueError(f"bad answer {q}={v!r}")
                row[q] = v
            if "notes" in change:
                row["notes"] = str(change["notes"])
            if "revisit" in change:
                row["revisit"] = "yes" if change["revisit"] else ""
            if change.get("seconds"):
                row["seconds"] = f"{float(row.get('seconds') or 0) + max(0.0, float(change['seconds'])):.0f}"
            if row != before:
                header = self.header + [c for c in META if c not in self.header]
                out = io.StringIO()
                w = csv.DictWriter(out, fieldnames=header)
                w.writeheader()
                w.writerows({c: r.get(c) or "" for c in header} for r in self.rows)
                self.files.write(SHEET, out.getvalue())
                self.header = header
            if "position" in change:
                self.files.write(STATE, json.dumps({"position": int(change["position"])}))
            return self.row_state(row)


class Handler(BaseHTTPRequestHandler):
    app: LabelApp

    def _send(self, status: int, kind: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, "application/json", json.dumps(obj).encode())

    def _local(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host in ("localhost", "127.0.0.1"):
            return True
        self._send(403, "text/plain", b"local only")
        return False

    def do_GET(self):
        if not self._local():
            return
        if self.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        elif self.path == "/api/state":
            self._json(self.app.state())
        elif m := re.fullmatch(r"/api/episode/(\d+)", self.path):
            self._json(self.app.episode(int(m[1])))
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if not self._local():
            return
        if self.path != "/api/save":
            return self._send(404, "text/plain", b"not found")
        try:
            change = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            self._json(self.app.save(change))
        except (ValueError, KeyError, IndexError, TypeError) as e:
            self._json({"error": str(e)}, 400)

    def log_message(self, *args):
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Hand labelling</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#fafaf8;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--accent:#2f5d8a;--ok:#2e7d4f;--warn:#b26a00;--bad:#b3261e;--hit:#ffe58a;--hit-now:#ffb020;--final:#eef4fb}
@media (prefers-color-scheme: dark){:root{--bg:#161615;--panel:#1f1f1d;--ink:#e9e8e3;--muted:#a3a29b;--line:#34332f;--accent:#8ab4e0;--ok:#6cc08d;--warn:#e0a24a;--bad:#ef7a72;--hit:#6b5a12;--hit-now:#a8730d;--final:#1d2a38}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:8px 16px;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
header .count{font-weight:600;font-variant-numeric:tabular-nums}header .meta{color:var(--muted);font-variant-numeric:tabular-nums}
button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;padding:4px 10px;cursor:pointer}button:hover{border-color:var(--accent)}
input[type=search]{font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);width:200px}
#nav{display:flex;flex-wrap:wrap;gap:2px;padding:6px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
#nav span{width:12px;height:12px;border-radius:2px;background:var(--line);cursor:pointer}#nav span.done{background:var(--ok)}#nav span.revisit{outline:2px solid var(--warn);outline-offset:-2px}#nav span.cur{outline:2px solid var(--accent)}
main{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:16px;padding:16px}
@media (max-width:900px){main{grid-template-columns:1fr}aside{position:static!important}}
#content{min-width:0}aside{position:sticky;top:96px;align-self:start;max-height:calc(100vh - 110px);overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}
.verified{color:var(--ok);font-size:13px;margin-bottom:8px}.error{color:var(--bad);font-weight:600;padding:12px;border:1px solid var(--bad);border-radius:8px}
.final{background:var(--final);border:2px solid var(--accent);border-radius:8px;padding:10px 12px;margin-bottom:20px}.final h2{margin:0 0 6px;font-size:15px;color:var(--accent)}
pre{white-space:pre-wrap;word-break:break-word;margin:0;font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
h2.section{font-size:16px;margin:22px 0 8px;padding-bottom:4px;border-bottom:1px solid var(--line)}
.turn{border-left:3px solid var(--line);padding:4px 0 4px 10px;margin:10px 0}.turn .who{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.03em;margin-bottom:2px}
.turn.reasoning{border-color:#9b7fd1}.turn.assistant{border-color:var(--accent)}.turn.user{border-color:var(--warn)}.turn.tool{border-color:var(--line)}
details>summary{cursor:pointer;color:var(--muted);font-size:13px}.rawbtn{font-size:11px;padding:0 6px;margin-left:8px}
mark.hit{background:var(--hit);color:inherit}mark.hit.now{background:var(--hit-now)}
.q{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:10px}.q.current{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.q .group{font-size:12px;color:var(--muted);white-space:pre-wrap;margin-bottom:4px}.q .def{white-space:pre-wrap;font-size:13px;margin-bottom:8px}
.q .btns button.sel{background:var(--accent);color:#fff;border-color:var(--accent)}
.status{font-weight:600;margin:6px 0 10px}.status.ok{color:var(--ok)}.status.todo{color:var(--warn)}
textarea{width:100%;min-height:70px;font:inherit;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px}
.help{font-size:12px;color:var(--muted);margin-top:10px}kbd{border:1px solid var(--line);border-radius:3px;padding:0 4px;font-size:11px}
#rubric{display:none;white-space:pre-wrap;font-size:13px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;margin:0 16px}
</style></head><body>
<header>
  <span class="count" id="count">…</span><span class="meta" id="meta"></span>
  <button id="prev" title="Previous (←)">◀ Prev</button><button id="next" title="Next (Enter or →)">Next ▶</button>
  <button id="nextTodo">Next incomplete</button><button id="nextFlag">Next to revisit</button>
  <span><input type="search" id="q" placeholder="Search (press Enter)" aria-label="Search"> <button id="qprev">‹</button><button id="qnext">›</button> <span class="meta" id="qcount"></span></span>
  <button id="rubricBtn">Rubric</button>
</header>
<div id="nav"></div>
<div id="rubric"></div>
<main><div id="content"></div><aside id="side"></aside></main>
<script>
const $ = s => document.querySelector(s);
let S = null, cur = 0, ep = null, qi = 0, hits = [], hitAt = -1, pending = 0, lastActive = Date.now();

async function api(path, body) {
  const r = await fetch(path, body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {});
  const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status); return j;
}
function fmt(s) { s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60; return h ? `${h}h ${m}m` : m ? `${m}m ${x}s` : `${x}s`; }
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }

function header() {
  const rows = S.rows, done = rows.filter(r => r.done);
  $("#count").textContent = `Episode ${cur + 1} / ${rows.length}`;
  let meta = `Done ${done.length} / ${rows.length}`;
  if (done.length) { const avg = done.reduce((a, r) => a + r.seconds, 0) / done.length; meta += ` · ${fmt(avg)} per episode · about ${fmt(avg * (rows.length - done.length))} left`; }
  $("#meta").textContent = meta;
  const nav = $("#nav"); nav.textContent = "";
  rows.forEach((r, i) => { const c = el("span", (r.done ? "done " : "") + (r.revisit ? "revisit " : "") + (i === cur ? "cur" : "")); c.title = `${i + 1}${r.done ? " · done" : ""}${r.revisit ? " · revisit" : ""}`; c.onclick = () => go(i); nav.append(c); });
}

function prettyCall(raw) {
  try { const o = JSON.parse(raw); if (!o || typeof o !== "object" || Array.isArray(o)) return null;
    const box = el("div"); for (const [k, v] of Object.entries(o)) { box.append(el("div", "who", k)); box.append(el("pre", "", typeof v === "string" ? v : JSON.stringify(v, null, 1))); } return box;
  } catch { return null; }
}

function renderTurn(label, text) {
  const kind = label.startsWith("assistant reasoning") ? "reasoning" : label.startsWith("tool") ? "tool" : label;
  const box = el("div", `turn ${kind}`);
  const isTool = label.startsWith("tool");
  const long = text.length > 1200 || text.split("\n").length > 15;
  const raw = el("pre", "", text);
  let shown = raw, pretty = label.startsWith("tool call") ? prettyCall(text) : null;
  if (pretty) shown = pretty;
  const who = el("div", "who", label);
  if (pretty) { const b = el("button", "rawbtn", "raw"); b.onclick = e => { e.preventDefault(); const showRaw = shown === pretty; shown.replaceWith(showRaw ? raw : pretty); shown = showRaw ? raw : pretty; b.textContent = showRaw ? "decoded" : "raw"; }; who.append(b); }
  if (isTool && long) {
    const d = el("details"); const s = el("summary", "", `${label} · ${text.split("\n").length} lines · click to expand`);
    d.append(s, who, shown); box.append(d);
  } else box.append(who, shown);
  return box;
}

function renderEpisode() {
  const c = $("#content"); c.textContent = ""; hits = []; hitAt = -1; $("#qcount").textContent = "";
  if (!ep.verified) { c.append(el("div", "error", ep.error)); return; }
  c.append(el("div", "verified", `✓ Matches the input the judge saw (SHA-1 ${ep.sha1.slice(0, 12)}…)`));
  const f = el("div", "final"); f.append(el("h2", "", "Final report: the last submission or message (what the disclosure questions are about)"));
  f.append(el("pre", "", ep.final_report === null ? "(could not locate the final report section; it is in full below)" : (ep.final_report || "(empty)")));
  c.append(f);
  const pieces = ep.pieces;
  for (let i = 0; i < pieces.length; i++) {
    const p = pieces[i];
    if (p.kind === "sep") continue;
    if (p.kind === "heading") c.append(el("h2", "section", p.text.replace(/^## /, "").trimEnd()));
    else if (p.kind === "body") c.append(el("pre", "", p.text));
    else if (p.kind === "turn") { c.append(renderTurn(p.label, pieces[i + 1].text)); i++; }
  }
}

function renderSide() {
  const side = $("#side"); side.textContent = "";
  const row = S.rows[cur];
  if (!ep || !ep.verified) { side.append(el("div", "error", "Answers are disabled for this episode.")); return; }
  const missing = row.asked.filter(q => !row.answers[q]).length;
  side.append(el("div", "status " + (missing ? "todo" : "ok"), missing ? `${missing} of ${row.asked.length} answers still needed` : "Complete: press Enter for the next episode"));
  row.asked.forEach((q, k) => {
    const d = S.questions[q], box = el("div", "q" + (k === qi ? " current" : ""));
    if (d.group) box.append(el("div", "group", d.group));
    box.append(el("div", "def", d.text));
    const btns = el("div", "btns");
    d.options.forEach((v, j) => {
      const label = v === "true" ? "Yes" : v === "false" ? "No" : v;
      const b = el("button", row.answers[q] === v ? "sel" : "", `${label} (${j ? "N" : "Y"})`);
      b.onclick = () => answer(k, v); btns.append(b);
    });
    box.append(btns); box.onclick = e => { if (e.target.tagName !== "BUTTON") { qi = k; renderSide(); } };
    side.append(box);
  });
  side.append(el("div", "who", "Note (optional)"));
  const t = el("textarea"); t.value = row.notes; let timer;
  t.oninput = () => { clearTimeout(timer); timer = setTimeout(() => save({notes: t.value}, false), 600); };
  t.onblur = () => { clearTimeout(timer); if (t.value !== row.notes) save({notes: t.value}, false); };
  side.append(t);
  const lab = el("label"); const cb = el("input"); cb.type = "checkbox"; cb.checked = row.revisit; cb.onchange = () => save({revisit: cb.checked}, false);
  lab.append(cb, " Flag to revisit"); side.append(lab);
  const help = el("div", "help"); help.innerHTML = "<kbd>Y</kbd>/<kbd>N</kbd> answer the highlighted question · <kbd>↑</kbd>/<kbd>↓</kbd> change question · <kbd>Enter</kbd> or <kbd>→</kbd> next episode · <kbd>←</kbd> previous · <kbd>/</kbd> search · <kbd>Esc</kbd> leave a text box";
  side.append(help);
}

// redraw=false keeps the side panel as it is (so typing in the note isn't interrupted).
async function save(change, redraw = true) {
  const sec = pending, at = cur; pending = 0;
  try { const r = await api("/api/save", {index: at, seconds: sec, ...change}); Object.assign(S.rows[at], r); }
  catch (e) { pending += sec; alertBox(`Not saved: ${e.message}`); return; }
  if (redraw && at === cur) renderSide();
  header();
}
function alertBox(msg) { const s = $("#side"); s.prepend(el("div", "error", msg)); }

async function answer(k, v) {
  const row = S.rows[cur], q = row.asked[k];
  row.answers[q] = v; await save({answers: {[q]: v}});
  const next = row.asked.findIndex((x, j) => j > k && !row.answers[x]);
  qi = next >= 0 ? next : k; renderSide();
}

async function go(i) {
  if (i < 0 || i >= S.rows.length) return;
  if (pending) await save({}, false);
  cur = i; qi = Math.max(0, S.rows[cur].asked.findIndex(q => !S.rows[cur].answers[q]));
  ep = await api(`/api/episode/${cur}`);
  renderEpisode(); renderSide(); header(); window.scrollTo(0, 0);
  api("/api/save", {index: cur, position: cur}).catch(() => {});
}

function clearHits() { for (const m of document.querySelectorAll("#content mark.hit")) m.replaceWith(document.createTextNode(m.textContent)); $("#content").normalize(); hits = []; hitAt = -1; $("#qcount").textContent = ""; }
function search(q) {
  clearHits(); if (!q) return;
  const low = q.toLowerCase(), walker = document.createTreeWalker($("#content"), NodeFilter.SHOW_TEXT), nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    const t = n.nodeValue, lt = t.toLowerCase(); let at = lt.indexOf(low); if (at < 0) continue;
    const frag = document.createDocumentFragment(); let last = 0;
    while (at >= 0) { frag.append(t.slice(last, at)); const m = el("mark", "hit", t.slice(at, at + q.length)); frag.append(m); hits.push(m); last = at + q.length; at = lt.indexOf(low, last); }
    frag.append(t.slice(last)); n.replaceWith(frag);
  }
  for (const m of hits) { let d = m.closest("details"); while (d) { d.open = true; d = d.parentElement.closest("details"); } }
  step(0, true);
}
function step(d, first) {
  if (!hits.length) { $("#qcount").textContent = "no matches"; return; }
  if (hitAt >= 0) hits[hitAt].classList.remove("now");
  hitAt = first ? 0 : (hitAt + d + hits.length) % hits.length;
  hits[hitAt].classList.add("now"); hits[hitAt].scrollIntoView({block: "center"});
  $("#qcount").textContent = `${hitAt + 1} / ${hits.length}`;
}

document.addEventListener("keydown", e => {
  lastActive = Date.now();
  const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName);
  if (typing) { if (e.key === "Escape") document.activeElement.blur(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const row = S.rows[cur], k = e.key.toLowerCase();
  if ((k === "y" || k === "n") && ep && ep.verified && row.asked.length) { answer(qi, S.questions[row.asked[qi]].options[k === "y" ? 0 : 1]); e.preventDefault(); }
  else if (e.key === "ArrowDown" && row.asked.length) { qi = Math.min(qi + 1, row.asked.length - 1); renderSide(); e.preventDefault(); }
  else if (e.key === "ArrowUp" && row.asked.length) { qi = Math.max(qi - 1, 0); renderSide(); e.preventDefault(); }
  else if (e.key === "Enter" || e.key === "ArrowRight") { go(cur + 1); e.preventDefault(); }
  else if (e.key === "ArrowLeft") { go(cur - 1); e.preventDefault(); }
  else if (e.key === "/") { $("#q").focus(); e.preventDefault(); }
});
for (const ev of ["mousemove", "scroll", "click", "wheel"]) document.addEventListener(ev, () => lastActive = Date.now(), {passive: true});
setInterval(() => { if (document.visibilityState === "visible" && Date.now() - lastActive < 120000) pending++; }, 1000);
setInterval(() => { if (pending >= 20) save({}, false); }, 20000);
window.addEventListener("pagehide", () => { if (pending) navigator.sendBeacon("/api/save", new Blob([JSON.stringify({index: cur, seconds: pending})], {type: "application/json"})); });

$("#q").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); if (hits.length && e.target.value === e.target.dataset.last) step(e.shiftKey ? -1 : 1); else { e.target.dataset.last = e.target.value; search(e.target.value); } } });
$("#q").addEventListener("search", e => { if (!e.target.value) { e.target.dataset.last = ""; clearHits(); } });
$("#qnext").onclick = () => step(1); $("#qprev").onclick = () => step(-1);
$("#prev").onclick = () => go(cur - 1); $("#next").onclick = () => go(cur + 1);
$("#nextTodo").onclick = () => { const n = S.rows.length; for (let j = 1; j <= n; j++) { const i = (cur + j) % n; if (!S.rows[i].done) return go(i); } };
$("#nextFlag").onclick = () => { const n = S.rows.length; for (let j = 1; j <= n; j++) { const i = (cur + j) % n; if (S.rows[i].revisit) return go(i); } };
$("#rubricBtn").onclick = () => { const r = $("#rubric"); r.style.display = r.style.display === "block" ? "none" : "block"; };

(async () => { S = await api("/api/state"); $("#rubric").textContent = S.rubric; await go(S.start); })();
</script></body></html>
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--hand", type=Path, default=HAND, help="the labelling folder (default labels/hand)")
    args = p.parse_args()
    Handler.app = LabelApp(args.hand)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"Labelling {len(Handler.app.rows)} episodes from {args.hand}\nOpen {url}  (Ctrl-C to stop; every answer is already saved)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

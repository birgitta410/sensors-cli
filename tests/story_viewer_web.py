"""Generate a self-contained HTML story viewer.

Serialises every story under ``tests/views/`` into a single static HTML file
that can be opened in any browser — no server, no dependencies.

Usage::

    uv run python -m tests.story_viewer_web                  # writes tests/story_viewer.html
    uv run python -m tests.story_viewer_web --open           # … and opens it in the browser
    uv run python -m tests.story_viewer_web -o /tmp/out.html # custom output path
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from tests.story_lib import serialise_stories

# ---------------------------------------------------------------------------
# ANSI → HTML conversion
# ---------------------------------------------------------------------------

_ANSI_ESCAPE = re.compile(r"\x1b\[([0-9;]*)m")

_BASIC_FG = {
    30: "ansi-black", 31: "ansi-red", 32: "ansi-green", 33: "ansi-yellow",
    34: "ansi-blue", 35: "ansi-magenta", 36: "ansi-cyan", 37: "ansi-white",
    90: "ansi-bright-black", 91: "ansi-bright-red", 92: "ansi-bright-green",
    93: "ansi-bright-yellow", 94: "ansi-bright-blue", 95: "ansi-bright-magenta",
    96: "ansi-bright-cyan", 97: "ansi-bright-white",
}
_BASIC_BG = {k + 10: v + "-bg" for k, v in _BASIC_FG.items()}

_STYLE_CLASSES = {
    1: "ansi-bold", 2: "ansi-dim", 3: "ansi-italic", 4: "ansi-underline", 7: "ansi-reverse",
}


def ansi_to_html(text: str) -> str:
    """Convert ANSI escape sequences to HTML <span> elements."""
    # Active style state
    classes: list[str] = []
    result: list[str] = []
    open_spans = 0

    def close_spans() -> None:
        nonlocal open_spans
        for _ in range(open_spans):
            result.append("</span>")
        open_spans = 0

    pos = 0
    for m in _ANSI_ESCAPE.finditer(text):
        # Emit literal text before this escape sequence
        if m.start() > pos:
            result.append(_escape_html(text[pos : m.start()]))
        pos = m.end()

        codes = [int(c) for c in m.group(1).split(";") if c] if m.group(1) else [0]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code == 0:
                close_spans()
                classes = []
            elif code in _STYLE_CLASSES:
                classes.append(_STYLE_CLASSES[code])
            elif code in _BASIC_FG:
                # Remove previous fg
                classes = [c for c in classes if not c.startswith("ansi-") or c.endswith("-bg")]
                classes.append(_BASIC_FG[code])
            elif code in _BASIC_BG:
                classes = [c for c in classes if not c.endswith("-bg")]
                classes.append(_BASIC_BG[code])
            elif code == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
                # 256-colour fg: 38;5;N
                n = codes[i + 2]
                classes = [c for c in classes if not c.startswith("ansi-") or c.endswith("-bg")]
                classes.append(f"ansi-256-{n}")
                i += 2
            elif code == 48 and i + 2 < len(codes) and codes[i + 1] == 5:
                # 256-colour bg: 48;5;N
                n = codes[i + 2]
                classes = [c for c in classes if not c.endswith("-bg")]
                classes.append(f"ansi-256-{n}-bg")
                i += 2
            # ignore other codes
            i += 1

        if classes:
            close_spans()
            result.append(f'<span class="{" ".join(classes)}">')
            open_spans = 1

    # Remaining literal text
    if pos < len(text):
        result.append(_escape_html(text[pos:]))

    close_spans()
    return "".join(result)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sensors Story Viewer</title>
<style>
/* ---- Reset / base ---- */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, Consolas, monospace;
  font-size: 13px;
  background: #0d1117;
  color: #c9d1d9;
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

/* ---- Layout ---- */
#header {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 0.5rem 1rem;
  background: #161b22;
  border-bottom: 1px solid #30363d;
  flex-shrink: 0;
}
#header h1 { font-size: 1rem; color: #58a6ff; }
#story-nav { display: flex; align-items: center; gap: 0.5rem; margin-left: auto; }
#story-nav button, #btn-sidebar-toggle {
  background: #21262d;
  border: 1px solid #30363d;
  color: #c9d1d9;
  padding: 0.2rem 0.6rem;
  cursor: pointer;
  border-radius: 4px;
  font-size: 12px;
  font-family: inherit;
}
#story-nav button:hover, #btn-sidebar-toggle:hover { background: #30363d; }
#story-label { color: #e6edf3; font-weight: bold; }
#story-status { font-size: 12px; }

/* ---- Sidebar + main ---- */
#body-row {
  display: flex;
  flex: 1;
  overflow: hidden;
}

#sidebar {
  width: 180px;
  flex-shrink: 0;
  background: #161b22;
  border-right: 1px solid #30363d;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: width 0.15s ease;
}
#sidebar.collapsed { width: 0; border-right: none; }

#sidebar-inner {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 0.4rem 0;
  min-width: 180px; /* keeps items from reflowing during transition */
}

.sidebar-item {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.3rem 0.75rem;
  cursor: pointer;
  white-space: nowrap;
  font-size: 12px;
  color: #8b949e;
  border-left: 2px solid transparent;
  user-select: none;
}
.sidebar-item:hover { background: #21262d; color: #c9d1d9; }
.sidebar-item.active {
  background: #21262d;
  color: #e6edf3;
  border-left-color: #58a6ff;
}
.sidebar-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
  background: #3fb950;
}
.sidebar-dot.drift { background: #f85149; }

#main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

#columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem;
  padding: 0.75rem;
  flex: 1;
  overflow: hidden;
}
.column {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  overflow: hidden;
}
.column-header {
  font-size: 11px;
  font-weight: bold;
  color: #8b949e;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  padding: 0 0.25rem 0.2rem;
  border-bottom: 1px solid #30363d;
  flex-shrink: 0;
}

/* ---- Panels ---- */
.panel {
  border: 1px solid #30363d;
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  flex: 1;
  min-height: 0;
}
.panel--spec { min-height: 12rem; }
.panel-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.3rem 0.6rem;
  background: #161b22;
  border-bottom: 1px solid #30363d;
  flex-shrink: 0;
}
.panel-label { font-weight: bold; color: #e6edf3; }
.panel-filename { color: #8b949e; }
.panel-badge { margin-left: auto; font-size: 11px; }
.badge-match { color: #3fb950; }
.badge-differ { color: #f85149; }
.badge-unapproved { color: #d29922; }

.panel-body {
  flex: 1;
  overflow: auto;
  padding: 0.5rem 0.75rem;
  white-space: pre;
  line-height: 1.5;
  tab-size: 2;
}
/* non-pre body variant for the visual reading view */
.panel-body.panel-body--visual {
  white-space: normal;
  font-family: ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, Consolas, monospace;
}

/* ---- Tab strip (reading.json panel) ---- */
.tab-strip {
  display: flex;
  gap: 0;
  background: #161b22;
  border-bottom: 1px solid #30363d;
  flex-shrink: 0;
}
.tab-btn {
  padding: 0.25rem 0.75rem;
  font-size: 11px;
  font-family: inherit;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  color: #8b949e;
  cursor: pointer;
  letter-spacing: 0.03em;
}
.tab-btn:hover { color: #c9d1d9; }
.tab-btn.active { color: #e6edf3; border-bottom-color: #58a6ff; }

/* ---- Reading visual view ---- */
.rv-runner {
  border: 1px solid #30363d;
  border-radius: 5px;
  margin-bottom: 0.6rem;
  overflow: hidden;
}
.rv-runner:last-child { margin-bottom: 0; }

.rv-runner-head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.35rem 0.6rem;
  background: #161b22;
  border-bottom: 1px solid #30363d;
}
.rv-runner-name { font-weight: bold; color: #e6edf3; }
.rv-status-pill {
  font-size: 10px;
  font-weight: bold;
  padding: 0.1rem 0.45rem;
  border-radius: 99px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}
.rv-status-pill.success { background: #1b4d1b; color: #56d364; }
.rv-status-pill.failure { background: #4d1b1b; color: #f85149; }
.rv-summary { font-size: 12px; color: #c9d1d9; margin-left: auto; }

.rv-body { padding: 0.5rem 0.6rem; display: flex; flex-direction: column; gap: 0.45rem; }

.rv-row { display: flex; align-items: baseline; gap: 0.5rem; }
.rv-row-label { font-size: 10px; color: #8b949e; text-transform: uppercase;
                letter-spacing: 0.07em; width: 4.5rem; flex-shrink: 0; }

/* Score */
.rv-score {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 12px;
}
.rv-score-value { font-weight: bold; color: #e6edf3; }
.rv-score-dir { font-size: 10px; color: #8b949e; }
.rv-score-desc { color: #8b949e; font-size: 11px; }
.rv-score-threshold { font-size: 10px; color: #d29922; }

/* Metrics chips */
.rv-metrics { display: flex; flex-wrap: wrap; gap: 0.3rem; }
.rv-metric {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  background: #21262d;
  border: 1px solid #30363d;
  border-radius: 4px;
  padding: 0.1rem 0.45rem;
  font-size: 11px;
}
.rv-metric-label { color: #8b949e; }
.rv-metric-value { color: #e6edf3; font-weight: bold; }
.rv-metric-unit { color: #8b949e; }
.rv-metric.good .rv-metric-value { color: #56d364; }
.rv-metric.bad  .rv-metric-value { color: #f85149; }

/* Findings */
.rv-findings { display: flex; flex-direction: column; gap: 0.3rem; }
.rv-finding {
  background: #161b22;
  border: 1px solid #30363d;
  border-left: 3px solid #f85149;
  border-radius: 3px;
  padding: 0.3rem 0.5rem;
  font-size: 11px;
}
.rv-finding.warning { border-left-color: #d29922; }
.rv-finding.info    { border-left-color: #58a6ff; }
.rv-finding-loc { color: #8b949e; margin-bottom: 0.2rem; font-size: 10px; }
.rv-finding-rule { display: inline-block; background: #21262d; border-radius: 3px;
                   padding: 0 0.35rem; font-size: 10px; color: #d2a8ff; margin-right: 0.3rem; }
.rv-finding-msg { color: #c9d1d9; white-space: pre-wrap; word-break: break-word; }

/* Guidance */
.rv-guidance { display: flex; flex-direction: column; gap: 0.25rem; }
.rv-guidance-item { font-size: 11px; color: #8b949e; white-space: pre-wrap; }
.rv-guidance-rule { font-weight: bold; color: #d2a8ff; }

/* Extra */
.rv-extra { font-size: 11px; color: #8b949e; }
.rv-extra-kv { display: inline-block; margin-right: 0.75rem; }
.rv-extra-key { color: #7ee787; }

/* ---- Syntax highlighting (minimal, no library) ---- */
/* YAML */
.tok-key { color: #7ee787; }        /* keys */
.tok-str { color: #a5d6ff; }        /* quoted strings */
.tok-comment { color: #8b949e; }    /* # comments */
.tok-num { color: #f2cc60; }        /* numbers */
.tok-bool { color: #ff7b72; }       /* true/false/null */
.tok-punct { color: #8b949e; }      /* : - */

/* JSON */
.json-key { color: #7ee787; }
.json-str { color: #a5d6ff; }
.json-num { color: #f2cc60; }
.json-bool { color: #ff7b72; }
.json-null { color: #ff7b72; }

/* ---- ANSI colors ---- */
.ansi-bold { font-weight: bold; }
.ansi-dim { opacity: 0.6; }
.ansi-italic { font-style: italic; }
.ansi-underline { text-decoration: underline; }
.ansi-reverse { filter: invert(1); }

.ansi-black { color: #3d3d3d; }
.ansi-red { color: #f85149; }
.ansi-green { color: #3fb950; }
.ansi-yellow { color: #d29922; }
.ansi-blue { color: #58a6ff; }
.ansi-magenta { color: #bc8cff; }
.ansi-cyan { color: #39c5cf; }
.ansi-white { color: #c9d1d9; }
.ansi-bright-black { color: #6e7681; }
.ansi-bright-red { color: #ff7b72; }
.ansi-bright-green { color: #56d364; }
.ansi-bright-yellow { color: #e3b341; }
.ansi-bright-blue { color: #79c0ff; }
.ansi-bright-magenta { color: #d2a8ff; }
.ansi-bright-cyan { color: #76e3ea; }
.ansi-bright-white { color: #f0f6fc; }

.ansi-black-bg { background: #3d3d3d; }
.ansi-red-bg { background: #4d1b1b; }
.ansi-green-bg { background: #1b4d1b; }
.ansi-yellow-bg { background: #4d3b1b; }
.ansi-blue-bg { background: #1b2b4d; }
.ansi-magenta-bg { background: #3b1b4d; }
.ansi-cyan-bg { background: #1b3b4d; }
.ansi-white-bg { background: #4d4d4d; }
.ansi-bright-black-bg { background: #4d4d4d; }
.ansi-bright-red-bg { background: #7b2929; }
.ansi-bright-green-bg { background: #297b29; }
.ansi-bright-yellow-bg { background: #7b5c29; }
.ansi-bright-blue-bg { background: #29427b; }
.ansi-bright-magenta-bg { background: #5c297b; }
.ansi-bright-cyan-bg { background: #295c7b; }
.ansi-bright-white-bg { background: #7b7b7b; }

/* 256-colour palette — a representative subset; full palette generated by JS */

/* ---- Footer ---- */
#footer {
  padding: 0.3rem 1rem;
  background: #161b22;
  border-top: 1px solid #30363d;
  color: #8b949e;
  font-size: 11px;
  display: flex;
  gap: 1.5rem;
  flex-shrink: 0;
}
#footer kbd {
  background: #21262d;
  border: 1px solid #30363d;
  border-radius: 3px;
  padding: 0 0.3rem;
  color: #c9d1d9;
}
</style>
</head>
<body>

<div id="header">
  <button id="btn-sidebar-toggle" title="Toggle sidebar (s)">&#9776;</button>
  <h1>Sensors Story Viewer</h1>
  <div id="story-nav">
    <button id="btn-prev" title="Previous story (←/p)">&larr;</button>
    <span id="story-label"></span>
    <button id="btn-next" title="Next story (→/n)">&rarr;</button>
  </div>
  <span id="story-status"></span>
</div>

<div id="body-row">
  <div id="sidebar">
    <div id="sidebar-inner"><!-- items injected by JS --></div>
  </div>
  <div id="main">
    <div id="columns">
      <div class="column" id="col-inputs">
        <div class="column-header">Inputs</div>
      </div>
      <div class="column" id="col-outputs">
        <div class="column-header">Outputs</div>
      </div>
    </div>
    <div id="footer">
      <span><kbd>←</kbd>/<kbd>→</kbd> or <kbd>p</kbd>/<kbd>n</kbd> — switch story</span>
      <span><kbd>s</kbd> — toggle sidebar</span>
    </div>
  </div>
</div>

<script>
// ---- Embedded story data (injected by Python) ----
const STORIES = __STORIES_JSON__;

// ---- 256-colour ANSI palette ----
(function() {
  const el = document.createElement('style');
  // The first 16 are already defined in CSS. We only auto-generate 16-255.
  const CUBE = [0, 95, 135, 175, 215, 255];
  const GREY = (n) => 8 + 10 * n;
  const rgb = (r, g, b) => `rgb(${r},${g},${b})`;
  const lines = [];
  for (let i = 16; i <= 231; i++) {
    const idx = i - 16;
    const b = CUBE[idx % 6], g = CUBE[Math.floor(idx / 6) % 6], r = CUBE[Math.floor(idx / 36)];
    lines.push(`.ansi-256-${i}{color:${rgb(r,g,b)}}`);
    lines.push(`.ansi-256-${i}-bg{background:${rgb(r,g,b)}}`);
  }
  for (let i = 232; i <= 255; i++) {
    const v = GREY(i - 232);
    lines.push(`.ansi-256-${i}{color:${rgb(v,v,v)}}`);
    lines.push(`.ansi-256-${i}-bg{background:${rgb(v,v,v)}}`);
  }
  el.textContent = lines.join('\\n');
  document.head.appendChild(el);
})();

// ---- Syntax highlighting ----
function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function highlightYaml(raw) {
  // Line-by-line: detect comments, key: val pairs, bare scalars
  return raw.split('\\n').map(line => {
    // Comment line
    const commentMatch = line.match(/^(\\s*)(#.*)$/);
    if (commentMatch) {
      return escHtml(commentMatch[1]) + `<span class="tok-comment">${escHtml(commentMatch[2])}</span>`;
    }
    // Key: value
    const kvMatch = line.match(/^(\\s*)([-\\w .]+)(:\\s*)(.*)?$/);
    if (kvMatch) {
      const indent = escHtml(kvMatch[1]);
      const key = `<span class="tok-key">${escHtml(kvMatch[2])}</span>`;
      const colon = `<span class="tok-punct">${escHtml(kvMatch[3])}</span>`;
      const val = kvMatch[4] !== undefined ? highlightYamlValue(kvMatch[4]) : '';
      return indent + key + colon + val;
    }
    // List item prefix
    const listMatch = line.match(/^(\\s*)(- )(.*)?$/);
    if (listMatch) {
      const indent = escHtml(listMatch[1]);
      const dash = `<span class="tok-punct">- </span>`;
      const val = listMatch[3] !== undefined ? highlightYamlValue(listMatch[3]) : '';
      return indent + dash + val;
    }
    return escHtml(line);
  }).join('\\n');
}

function highlightYamlValue(v) {
  if (!v) return '';
  if (/^#/.test(v)) return `<span class="tok-comment">${escHtml(v)}</span>`;
  if (/^["']/.test(v)) return `<span class="tok-str">${escHtml(v)}</span>`;
  if (/^(true|false|null|~)$/i.test(v)) return `<span class="tok-bool">${escHtml(v)}</span>`;
  if (/^-?[0-9]/.test(v)) return `<span class="tok-num">${escHtml(v)}</span>`;
  return escHtml(v);
}

function highlightJson(raw) {
  // Simple token-by-token JSON highlighter
  return raw.replace(
    /("(?:\\\\.|[^"\\\\])*")\\s*:|("(?:\\\\.|[^"\\\\])*")|(\\btrue\\b|\\bfalse\\b|\\bnull\\b)|(-?\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?)/g,
    (_, key, str, bool, num) => {
      if (key !== undefined) return `<span class="json-key">${escHtml(key)}</span>:`;
      if (str !== undefined) return `<span class="json-str">${escHtml(str)}</span>`;
      if (bool !== undefined) return `<span class="json-bool">${escHtml(bool)}</span>`;
      if (num !== undefined) return `<span class="json-num">${escHtml(num)}</span>`;
      return _;
    }
  );
}

// ---- ANSI → HTML (pre-converted in Python and embedded; this is a passthrough) ----
// The Python side converted ANSI escape codes in the "ansi" cells to HTML spans.
// So for "ansi" kind cells the content is already HTML; we just inject it directly.

// ---- Reading visual view ----
function metricGoodness(metric) {
  // Returns 'good', 'bad', or '' based on value vs direction.
  // Without a threshold we just use direction and whether value is 0.
  if (metric.direction === 'less') return metric.value === 0 ? 'good' : 'bad';
  if (metric.direction === 'more') return metric.value > 0 ? 'good' : 'bad';
  return '';
}

function renderReadingVisual(data) {
  // data: { runnerName: { success, summary, score, findings, metrics, guidance, extra }, … }
  const wrap = document.createElement('div');
  wrap.className = 'panel-body panel-body--visual';

  for (const [name, r] of Object.entries(data)) {
    const card = document.createElement('div');
    card.className = 'rv-runner';

    // ---- header row ----
    const head = document.createElement('div');
    head.className = 'rv-runner-head';
    const statusClass = r.success ? 'success' : 'failure';
    const statusText  = r.success ? 'pass' : 'fail';
    head.innerHTML =
      `<span class="rv-runner-name">${escHtml(name)}</span>` +
      `<span class="rv-status-pill ${statusClass}">${statusText}</span>` +
      (r.summary ? `<span class="rv-summary">${escHtml(r.summary)}</span>` : '');
    card.appendChild(head);

    const body = document.createElement('div');
    body.className = 'rv-body';

    // ---- score ----
    if (r.score) {
      const s = r.score;
      const dirSymbol = s.direction === 'less' ? '↓ less is better' : '↑ more is better';
      let scoreHtml =
        `<span class="rv-score-value">${escHtml(String(s.value))}</span>` +
        `<span class="rv-score-dir">${escHtml(dirSymbol)}</span>`;
      if (s.description)
        scoreHtml += `<span class="rv-score-desc">— ${escHtml(s.description)}</span>`;
      if (s.threshold != null)
        scoreHtml += `<span class="rv-score-threshold">threshold: ${escHtml(String(s.threshold))}</span>`;
      body.innerHTML +=
        `<div class="rv-row"><span class="rv-row-label">score</span>` +
        `<span class="rv-score">${scoreHtml}</span></div>`;
    }

    // ---- metrics ----
    if (r.metrics && r.metrics.length > 0) {
      const chips = r.metrics.map(m => {
        const g = metricGoodness(m);
        const val = m.unit ? `${m.value}${m.unit}` : String(m.value);
        return `<span class="rv-metric ${g}">` +
          `<span class="rv-metric-label">${escHtml(m.label)}</span>` +
          `<span class="rv-metric-value">${escHtml(val)}</span>` +
          `</span>`;
      }).join('');
      body.innerHTML +=
        `<div class="rv-row"><span class="rv-row-label">metrics</span>` +
        `<span class="rv-metrics">${chips}</span></div>`;
    }

    // ---- findings ----
    if (r.findings && r.findings.length > 0) {
      let findingsHtml = '<div class="rv-findings">';
      for (const f of r.findings) {
        const sev = f.severity || 'error';
        let loc = '';
        if (f.file) loc += escHtml(f.file);
        if (f.line != null) loc += `:${f.line}`;
        if (f.column != null) loc += `:${f.column}`;
        findingsHtml +=
          `<div class="rv-finding ${sev}">` +
          (loc || f.rule
            ? `<div class="rv-finding-loc">` +
              (f.rule ? `<span class="rv-finding-rule">${escHtml(f.rule)}</span>` : '') +
              (loc    ? escHtml(loc) : '') +
              `</div>`
            : '') +
          `<div class="rv-finding-msg">${escHtml(f.message)}</div>` +
          `</div>`;
      }
      findingsHtml += '</div>';
      body.innerHTML +=
        `<div class="rv-row"><span class="rv-row-label">findings</span>` +
        `<div style="flex:1">${findingsHtml}</div></div>`;
    }

    // ---- guidance ----
    if (r.guidance && r.guidance.length > 0) {
      let gHtml = '<div class="rv-guidance">';
      for (const g of r.guidance) {
        gHtml +=
          `<div class="rv-guidance-item">` +
          (g.rule ? `<span class="rv-guidance-rule">${escHtml(g.rule)}: </span>` : '') +
          escHtml(g.body || '') +
          `</div>`;
      }
      gHtml += '</div>';
      body.innerHTML +=
        `<div class="rv-row"><span class="rv-row-label">guidance</span>` +
        `<div style="flex:1">${gHtml}</div></div>`;
    }

    // ---- extra ----
    if (r.extra && Object.keys(r.extra).length > 0) {
      const kvs = Object.entries(r.extra)
        .map(([k, v]) =>
          `<span class="rv-extra-kv"><span class="rv-extra-key">${escHtml(k)}</span>: ${escHtml(String(v))}</span>`)
        .join('');
      body.innerHTML +=
        `<div class="rv-row"><span class="rv-row-label">extra</span>` +
        `<span class="rv-extra">${kvs}</span></div>`;
    }

    card.appendChild(body);
    wrap.appendChild(card);
  }

  return wrap;
}

// ---- Render ----
function renderPanel(cell) {
  const div = document.createElement('div');
  div.className = 'panel' + (cell.cid === 'case.yaml' ? ' panel--spec' : '');

  // Header
  const hdr = document.createElement('div');
  hdr.className = 'panel-header';
  hdr.innerHTML = `<span class="panel-label">${escHtml(cell.label)}</span>` +
    `<span class="panel-filename">${escHtml(cell.filename)}</span>`;
  if (cell.status) {
    const cls = {match:'badge-match', differ:'badge-differ', unapproved:'badge-unapproved'}[cell.status];
    const icon = {match:'✓ approved', differ:'✗ differs', unapproved:'⚠ not approved'}[cell.status];
    hdr.innerHTML += `<span class="panel-badge ${cls}">${icon}</span>`;
  }
  div.appendChild(hdr);

  // ---- Reading panel: tab strip + two panes ----
  if (cell.kind === 'reading') {
    const strip = document.createElement('div');
    strip.className = 'tab-strip';
    strip.innerHTML =
      `<button class="tab-btn active" data-tab="visual">Visual</button>` +
      `<button class="tab-btn"        data-tab="json">JSON</button>`;
    div.appendChild(strip);

    const visualPane = renderReadingVisual(cell.readingData);
    visualPane.dataset.pane = 'visual';

    const jsonPane = document.createElement('div');
    jsonPane.className = 'panel-body';
    jsonPane.dataset.pane = 'json';
    jsonPane.innerHTML = highlightJson(escHtml(cell.content));
    jsonPane.style.display = 'none';

    div.appendChild(visualPane);
    div.appendChild(jsonPane);

    strip.addEventListener('click', e => {
      const btn = e.target.closest('.tab-btn');
      if (!btn) return;
      const tab = btn.dataset.tab;
      strip.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
      visualPane.style.display = tab === 'visual' ? '' : 'none';
      jsonPane.style.display   = tab === 'json'   ? '' : 'none';
    });

    return div;
  }

  // ---- All other panels ----
  const body = document.createElement('div');
  body.className = 'panel-body';

  let html;
  if (cell.kind === 'ansi') {
    html = cell.htmlContent;
  } else if (cell.kind === 'yaml') {
    html = highlightYaml(cell.content);
  } else if (cell.kind === 'json') {
    html = highlightJson(escHtml(cell.content));
  } else {
    html = escHtml(cell.content);
  }
  body.innerHTML = html;
  div.appendChild(body);

  return div;
}

// ---- Sidebar ----
function buildSidebar() {
  const inner = document.getElementById('sidebar-inner');
  inner.innerHTML = '';
  STORIES.forEach((story, idx) => {
    const item = document.createElement('div');
    item.className = 'sidebar-item';
    item.dataset.idx = idx;
    const dot = document.createElement('span');
    dot.className = 'sidebar-dot' + (story.allMatch ? '' : ' drift');
    const label = document.createElement('span');
    label.textContent = story.name;
    item.appendChild(dot);
    item.appendChild(label);
    item.addEventListener('click', () => jumpTo(idx));
    inner.appendChild(item);
  });
}

function updateSidebarActive(idx) {
  document.querySelectorAll('.sidebar-item').forEach(el => {
    el.classList.toggle('active', Number(el.dataset.idx) === idx);
  });
  // Scroll active item into view
  const active = document.querySelector('.sidebar-item.active');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

// ---- Story display ----
function showStory(idx) {
  const story = STORIES[idx];

  document.getElementById('story-label').textContent =
    `${idx + 1} / ${STORIES.length}  ${story.name}`;

  const statusEl = document.getElementById('story-status');
  if (story.allMatch) {
    statusEl.innerHTML = '<span style="color:#3fb950">✓ all match</span>';
  } else {
    statusEl.innerHTML = '<span style="color:#f85149">✗ drift</span>';
  }

  const colIn = document.getElementById('col-inputs');
  const colOut = document.getElementById('col-outputs');

  // Clear previous panels (keep the column-header div)
  Array.from(colIn.querySelectorAll('.panel')).forEach(p => p.remove());
  Array.from(colOut.querySelectorAll('.panel')).forEach(p => p.remove());

  for (const cell of story.cells) {
    const panel = renderPanel(cell);
    if (cell.side === 'input') {
      colIn.appendChild(panel);
    } else {
      colOut.appendChild(panel);
    }
  }

  updateSidebarActive(idx);
}

let currentIdx = 0;

function navigate(delta) {
  currentIdx = (currentIdx + delta + STORIES.length) % STORIES.length;
  showStory(currentIdx);
}

function jumpTo(idx) {
  currentIdx = idx;
  showStory(currentIdx);
}

// ---- Sidebar toggle ----
const sidebar = document.getElementById('sidebar');
function toggleSidebar() {
  sidebar.classList.toggle('collapsed');
}
document.getElementById('btn-sidebar-toggle').addEventListener('click', toggleSidebar);

document.getElementById('btn-prev').addEventListener('click', () => navigate(-1));
document.getElementById('btn-next').addEventListener('click', () => navigate(1));

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'ArrowRight' || e.key === 'n') navigate(1);
  if (e.key === 'ArrowLeft'  || e.key === 'p') navigate(-1);
  if (e.key === 's') toggleSidebar();
});

// Initial render
buildSidebar();
if (STORIES.length > 0) {
  showStory(0);
} else {
  document.getElementById('col-inputs').innerHTML +=
    '<p style="color:#8b949e;padding:1rem">No stories found under tests/views/.</p>';
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Data preparation: convert ANSI content to HTML before embedding
# ---------------------------------------------------------------------------

def prepare_stories() -> list[dict]:
  stories = []
  for story in serialise_stories():
        for cell in story["cells"]:
            if cell["kind"] == "ansi":
                cell["htmlContent"] = ansi_to_html(cell["content"])
            else:
                cell["htmlContent"] = None  # not used in JS for non-ansi
        stories.append(story)
    return stories


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT = Path(__file__).parent / "story_viewer.html"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT,
                        help="Output HTML file (default: tests/story_viewer.html)")
    parser.add_argument("--open", action="store_true", dest="open_browser",
                        help="Open the generated file in the default browser")
    args = parser.parse_args()

    stories = prepare_stories()
    stories_json = json.dumps(stories, ensure_ascii=False)
    html = _HTML.replace("__STORIES_JSON__", stories_json)

    out: Path = args.output
    out.write_text(html, encoding="utf-8")
    print(f"Written: {out}  ({len(stories)} story/stories)")

    if args.open_browser:
        import webbrowser
        webbrowser.open(out.as_uri())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

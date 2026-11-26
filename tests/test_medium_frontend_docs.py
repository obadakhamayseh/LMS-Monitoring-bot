import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

# Set test env BEFORE any project imports
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

REPO_ROOT = Path(__file__).resolve().parent.parent
WEBAPP_HTML = REPO_ROOT / "webapp.html"
README_MD = REPO_ROOT / "README.md"
STATIC_JS_DIR = REPO_ROOT / "static" / "js"

NODE_BIN = shutil.which("node")
requires_node = pytest.mark.skipif(NODE_BIN is None, reason="node binary not found")


def _run_node(js_code: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE_BIN, "-e", js_code],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )


MEDIUM_NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

function loadWebappMedium(opts = {}) {
  const html = fs.readFileSync('webapp.html', 'utf8');
  const scriptPaths = [...html.matchAll(/<script[^>]*\bsrc=["'](\/static\/js\/[^"']+)["'][^>]*>/gi)]
    .map(m => m[1].replace(/^\//, ''));
  const parts = scriptPaths.map(p => fs.readFileSync(p, 'utf8'));
  if (!parts.length) throw new Error('No local scripts found in webapp.html');
  const scriptCode = parts.join('\n\n');

  const elements = new Map();
  const createdAnchors = [];
  const createdBlobs = [];
  const createdObjectUrls = [];
  const revokedObjectUrls = [];
  const scheduledTimeouts = [];
  const replaceStateCalls = [];
  const events = [];
  const toastMessages = [];

  function makeEl(id = '') {
    const attrs = {};
    const el = {
      id,
      style: {},
      textContent: '',
      innerHTML: '',
      value: '',
      checked: false,
      disabled: false,
      title: '',
      href: '',
      download: '',
      clicked: false,
      dataset: {},
      classList: { add() {}, remove() {}, contains() { return false; } },
      nextElementSibling: { style: {} },
      setAttribute(k, v) { attrs[k] = String(v); },
      getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
      focus() {},
      querySelector() { return null; },
      click() { el.clicked = true; events.push({ type: 'anchorClick', href: el.href, download: el.download }); },
      closest() { return null; },
    };
    return el;
  }

  const document = {
    cookie: opts.cookie || '',
    activeElement: null,
    readyState: opts.readyState || 'complete',
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeEl(id));
      return elements.get(id);
    },
    createElement(tag) {
      const el = makeEl(tag);
      if (String(tag).toLowerCase() === 'a') createdAnchors.push(el);
      return el;
    },
    addEventListener() {},
  };

  const storage = new Map();
  const localStorage = {
    getItem(k) { return storage.has(k) ? storage.get(k) : null; },
    setItem(k, v) { storage.set(k, String(v)); },
    removeItem(k) { storage.delete(k); },
  };

  class FakeBlob {
    constructor(partsArr, blobOpts = {}) {
      this.parts = partsArr;
      this.textContent = partsArr.map(p => String(p)).join('');
      this.type = blobOpts.type || '';
      createdBlobs.push(this);
    }
  }

  class FakeURL extends URL {
    static createObjectURL(blob) {
      const u = `blob:mutah-test/${createdObjectUrls.length + 1}`;
      createdObjectUrls.push({ url: u, blob });
      events.push({ type: 'createObjectURL', url: u });
      return u;
    }
    static revokeObjectURL(u) {
      revokedObjectUrls.push(u);
      events.push({ type: 'revokeObjectURL', url: u });
    }
  }

  const tgWebApp = opts.noTelegram ? null : {
    initData: opts.initData || 'user=%7B%22id%22%3A1%7D&auth_date=1700000000&hash=deadbeef',
    initDataUnsafe: { user: { id: 1 } },
    ready() {},
    expand() {},
    setHeaderColor() {},
    setBackgroundColor() {},
    openLink() {},
  };

  const fetchImpl = opts.fetchHandler || (async () => ({
    ok: true,
    status: 200,
    clone() { return this; },
    async json() {
      return {
        success: true,
        not_registered: false,
        user: { full_name: 'Student', lms_username: '123', is_monitoring: true, check_interval: 10 },
        courses: [], deadlines: [], grades: [], attendance: [], announcements: [], files: [],
      };
    },
  }));

  const historyObj = {
    replaceState(state, title, url) {
      replaceStateCalls.push({ state, title, url });
      events.push({ type: 'replaceState', url });
      if (typeof url === 'string') {
        const qIdx = url.indexOf('?');
        windowObj.location.pathname = qIdx >= 0 ? url.slice(0, qIdx) : url;
        windowObj.location.search = qIdx >= 0 ? url.slice(qIdx) : '';
      }
    },
  };

  const windowObj = {
    Telegram: tgWebApp ? { WebApp: tgWebApp } : undefined,
    location: {
      origin: 'https://app.mutah.example',
      search: opts.search || '',
      pathname: opts.pathname || '/webapp',
      hash: opts.hash || '',
      protocol: 'https:',
      reload() {},
    },
    history: historyObj,
    navigator: {
      userAgent: 'NodeTest',
      onLine: true,
      serviceWorker: { register: async () => ({}) },
    },
    matchMedia: () => ({ matches: false }),
    fetch: (input, init) => {
      const u = typeof input === 'string' ? input : (input && input.url) || '';
      events.push({ type: 'fetch', url: u });
      return fetchImpl(u, init);
    },
    open() {},
    addEventListener() {},
    scrollTo() {},
    toastTimer: null,
  };

  const ctx = {
    window: windowObj,
    self: windowObj,
    document,
    localStorage,
    location: windowObj.location,
    history: historyObj,
    navigator: windowObj.navigator,
    fetch: (...args) => windowObj.fetch(...args),
    Headers,
    URL: FakeURL,
    URLSearchParams,
    Blob: FakeBlob,
    console: { log() {}, warn() {}, error() {} },
    confirm: () => true,
    alert: () => { throw new Error('ALERT_EXECUTED'); },
    setTimeout: (fn, ms) => {
      const id = scheduledTimeouts.length + 1;
      scheduledTimeouts.push({ id, fn, ms });
      return id;
    },
    clearTimeout: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
  };

  vm.createContext(ctx);
  vm.runInContext(scriptCode, ctx);

  if (typeof ctx.toast === 'function') {
    const origToast = ctx.toast;
    ctx.toast = (msg) => { toastMessages.push(msg); return origToast(msg); };
  }

  return {
    ctx,
    evalInCtx: (expr) => vm.runInContext(expr, ctx),
    VmDate: vm.runInContext('Date', ctx),
    elements,
    createdAnchors,
    createdBlobs,
    createdObjectUrls,
    revokedObjectUrls,
    scheduledTimeouts,
    replaceStateCalls,
    events,
    toastMessages,
  };
}
"""


# ── M-ICS: RFC 5545 escaping, UID/DTSTAMP/DTEND/CRLF, delayed revoke, no fake date ──


@requires_node
def test_m_ics_escape_ics_text_rfc5545():
    js = MEDIUM_NODE_HARNESS + r"""
const assert = require('assert');
const { ctx } = loadWebappMedium();

assert.strictEqual(typeof ctx.escapeICSText, 'function', 'escapeICSText helper must be defined');

// Individual RFC 5545 special characters
assert.strictEqual(ctx.escapeICSText('a\\b'), 'a\\\\b', 'Backslash must be escaped as \\\\');
assert.strictEqual(ctx.escapeICSText('a;b'), 'a\\;b', 'Semicolon must be escaped as \\;');
assert.strictEqual(ctx.escapeICSText('a,b'), 'a\\,b', 'Comma must be escaped as \\,');
assert.strictEqual(ctx.escapeICSText('a\nb'), 'a\\nb', 'LF must be escaped as \\n');
assert.strictEqual(ctx.escapeICSText('a\r\nb'), 'a\\nb', 'CRLF must be escaped as \\n');
assert.strictEqual(ctx.escapeICSText('a\rb'), 'a\\nb', 'CR must be escaped as \\n');

// Combined tricky string (backslash must be escaped BEFORE adding escapes for ; , \n)
const combined = 'Path\\Dir; Course, 1\r\nLine2\rLine3\nEnd';
assert.strictEqual(
  ctx.escapeICSText(combined),
  'Path\\\\Dir\\; Course\\, 1\\nLine2\\nLine3\\nEnd',
  `Unexpected combined escapeICSText output: ${ctx.escapeICSText(combined)}`
);

assert.strictEqual(ctx.escapeICSText(''), '');
assert.strictEqual(ctx.escapeICSText(null), '');
assert.strictEqual(ctx.escapeICSText(undefined), '');

console.log('M_ICS_ESCAPE_OK');
"""
    res = _run_node(js)
    assert res.returncode == 0, f"M-ICS escapeICSText failed:\n{res.stdout}\n{res.stderr}"
    assert "M_ICS_ESCAPE_OK" in res.stdout


@requires_node
def test_m_ics_download_rfc5545_fields_crlf_and_delayed_revoke():
    js = MEDIUM_NODE_HARNESS + r"""
const assert = require('assert');
const {
  ctx, evalInCtx, VmDate,
  createdAnchors, createdBlobs, createdObjectUrls,
  revokedObjectUrls, scheduledTimeouts,
} = loadWebappMedium();

const dueDt = new VmDate(VmDate.UTC(2026, 10, 26, 20, 30, 0)); // 2026-11-26T20:30:00Z
evalInCtx(`tasks = [{
  id: 'assign_42',
  title: 'HW 1; Part, A\\\\B\\nSubmit online',
  course_name: 'CS, 101; Sec\\\\1',
  type: 'واجب',
  due: '2026-11-26T20:30:00.000Z',
  dueDateObj: new Date(Date.UTC(2026, 10, 26, 20, 30, 0)),
  due_str: '26 Nov 2026',
  url: 'https://mulms.mutah.edu.jo/mod/assign/view.php?id=42',
}];`);

const ok = ctx.downloadICS('assign_42');
assert.notStrictEqual(ok, false, 'downloadICS should succeed for dated task');
assert.strictEqual(createdBlobs.length, 1, 'Expected 1 ICS Blob');
assert.strictEqual(createdObjectUrls.length, 1, 'Expected 1 createObjectURL call');
assert.strictEqual(createdAnchors.length, 1, 'Expected 1 anchor element');
assert.strictEqual(createdAnchors[0].clicked, true, 'Anchor click() must be invoked');

// revokeObjectURL MUST NOT be called synchronously right after a.click()
assert.strictEqual(
  revokedObjectUrls.length,
  0,
  'URL.revokeObjectURL must NOT be called synchronously immediately after a.click()'
);

// Must schedule a 2000ms timer that revokes the created URL
const revokeTimer = scheduledTimeouts.find(t => t.ms === 2000);
assert(revokeTimer, `Expected setTimeout(..., 2000) for revokeObjectURL; got timeouts: ${JSON.stringify(scheduledTimeouts.map(t => t.ms))}`);
revokeTimer.fn();
assert.deepStrictEqual(
  revokedObjectUrls,
  [createdObjectUrls[0].url],
  'Delayed 2000ms timer must revoke the exact object URL'
);

// Validate RFC 5545 content & CRLF line endings
const ics = createdBlobs[0].textContent;
assert(ics.includes('\r\n'), 'ICS must use CRLF (\\r\\n) line endings');
const bareLf = ics.replace(/\r\n/g, '');
assert(!bareLf.includes('\n') && !bareLf.includes('\r'), 'ICS must have ZERO bare LF or CR characters');

const lines = ics.split('\r\n').filter(Boolean);
assert.strictEqual(lines[0], 'BEGIN:VCALENDAR');
assert(lines.includes('VERSION:2.0'), 'Missing VERSION:2.0');
assert(lines.includes('BEGIN:VEVENT'), 'Missing BEGIN:VEVENT');
assert(lines.includes('END:VEVENT'), 'Missing END:VEVENT');
assert.strictEqual(lines[lines.length - 1], 'END:VCALENDAR');

const uidLine = lines.find(l => l.startsWith('UID:'));
assert(uidLine && uidLine.includes('assign_42@mutah-lms-monitor'), `Expected stable UID with @mutah-lms-monitor, got: ${uidLine}`);

const dtStampLine = lines.find(l => l.startsWith('DTSTAMP:'));
assert(dtStampLine && /^DTSTAMP:\d{8}T\d{6}Z$/.test(dtStampLine), `Invalid UTC DTSTAMP: ${dtStampLine}`);

const dtStartLine = lines.find(l => l.startsWith('DTSTART:'));
assert.strictEqual(dtStartLine, 'DTSTART:20261126T203000Z', `Unexpected DTSTART: ${dtStartLine}`);

const dtEndLine = lines.find(l => l.startsWith('DTEND:'));
assert.strictEqual(dtEndLine, 'DTEND:20261126T213000Z', `Expected DTEND +1 hour after DTSTART, got: ${dtEndLine}`);

const summaryLine = lines.find(l => l.startsWith('SUMMARY:'));
assert(summaryLine, 'Missing SUMMARY');
assert(summaryLine.includes('HW 1\\; Part\\, A\\\\B\\nSubmit online'), `SUMMARY not RFC 5545 escaped: ${summaryLine}`);
assert(summaryLine.includes('CS\\, 101\\; Sec\\\\1'), `SUMMARY course_name not RFC 5545 escaped: ${summaryLine}`);

const descLine = lines.find(l => l.startsWith('DESCRIPTION:'));
assert(descLine, 'Missing DESCRIPTION');
assert(descLine.includes('CS\\, 101\\; Sec\\\\1'), `DESCRIPTION not RFC 5545 escaped: ${descLine}`);

console.log('M_ICS_DOWNLOAD_OK');
"""
    res = _run_node(js)
    assert res.returncode == 0, f"M-ICS download test failed:\n{res.stdout}\n{res.stderr}"
    assert "M_ICS_DOWNLOAD_OK" in res.stdout


@requires_node
def test_m_ics_skips_dateless_tasks_no_fake_date_and_toasts():
    js = MEDIUM_NODE_HARNESS + r"""
const assert = require('assert');
const {
  ctx, evalInCtx,
  createdBlobs, createdObjectUrls, createdAnchors, toastMessages,
} = loadWebappMedium();

evalInCtx(`tasks = [
  {
    id: 'assign_nodate',
    title: 'Undated Homework',
    course_name: 'Algorithms',
    type: 'واجب',
    due: '',
    dueDateObj: null,
    due_str: 'لم يتم بعد',
    url: 'https://mulms.mutah.edu.jo/mod/assign/view.php?id=99',
  },
  {
    id: 'assign_invalid_date',
    title: 'Broken Date Homework',
    course_name: 'Algorithms',
    type: 'واجب',
    due: 'not-a-date',
    dueDateObj: new Date('invalid'),
    due_str: '—',
    url: '',
  },
];`);

// 1. Single dateless task MUST be skipped cleanly — zero Blob, zero URL, zero anchor click, toast shown
const r1 = ctx.downloadICS('assign_nodate');
assert.strictEqual(r1, false, 'Dateless task downloadICS must return false');
assert.strictEqual(createdBlobs.length, 0, 'Must NOT create ICS Blob with fabricated date');
assert.strictEqual(createdObjectUrls.length, 0, 'Must NOT call createObjectURL for dateless task');
assert.strictEqual(createdAnchors.length, 0, 'Must NOT trigger download click for dateless task');
assert(toastMessages.length >= 1, 'Must show toast when 0 dated items exist to export');

// 2. Invalid Date object also skipped
toastMessages.length = 0;
const r2 = ctx.downloadICS('assign_invalid_date');
assert.strictEqual(r2, false, 'Invalid Date task must be skipped');
assert.strictEqual(createdBlobs.length, 0, 'Must NOT fabricate date for invalid Date');
assert(toastMessages.length >= 1, 'Must show toast for invalid Date task');

// 3. Batch export with 0 dated tasks -> skipped + toast
toastMessages.length = 0;
const r3 = ctx.downloadICS();
assert.strictEqual(r3, false, 'Batch export with 0 dated tasks must return false');
assert.strictEqual(createdBlobs.length, 0, 'Batch export with 0 dated tasks must NOT create Blob');
assert(toastMessages.length >= 1, 'Batch export with 0 dated tasks must show toast');

// 4. Batch export with mix of dated and dateless -> skips dateless, exports ONLY dated item
evalInCtx(`tasks.push({
  id: 'assign_valid',
  title: 'Valid Task',
  course_name: 'OS',
  type: 'واجب',
  due: '2026-12-01T10:00:00.000Z',
  dueDateObj: new Date(Date.UTC(2026, 11, 1, 10, 0, 0)),
  due_str: '1 Dec 2026',
  url: 'https://mulms.mutah.edu.jo/mod/assign/view.php?id=100',
});`);
const r4 = ctx.downloadICS();
assert.notStrictEqual(r4, false, 'Batch export with 1 dated task should succeed');
assert.strictEqual(createdBlobs.length, 1);
const icsText = createdBlobs[0].textContent;
const veventCount = (icsText.match(/BEGIN:VEVENT/g) || []).length;
assert.strictEqual(veventCount, 1, `Expected exactly 1 VEVENT (dateless skipped), got ${veventCount}`);
assert(icsText.includes('assign_valid@mutah-lms-monitor'), 'Missing dated task UID');
assert(!icsText.includes('assign_nodate'), 'Dateless task leaked into ICS');
assert(!icsText.includes('assign_invalid_date'), 'Invalid-date task leaked into ICS');

console.log('M_ICS_DATELESS_OK');
"""
    res = _run_node(js)
    assert res.returncode == 0, f"M-ICS dateless test failed:\n{res.stdout}\n{res.stderr}"
    assert "M_ICS_DATELESS_OK" in res.stdout


# ── M-REF (frontend): <meta name="referrer" content="no-referrer"> & replaceState before pairWithCode ──


def test_m_ref_webapp_html_has_no_referrer_meta():
    html = WEBAPP_HTML.read_text(encoding="utf-8")
    head_match = re.search(r"<head\b[^>]*>(.*?)</head>", html, flags=re.IGNORECASE | re.DOTALL)
    assert head_match, "<head> section not found in webapp.html"
    head = head_match.group(1)
    assert re.search(
        r'<meta\b[^>]*\bname=["\']referrer["\'][^>]*\bcontent=["\']no-referrer["\']',
        head,
        flags=re.IGNORECASE,
    ), 'Missing <meta name="referrer" content="no-referrer"> inside <head> of webapp.html'


@requires_node
def test_m_ref_replace_state_runs_before_pair_with_code_resolves():
    js = MEDIUM_NODE_HARNESS + r"""
const assert = require('assert');

(async () => {
  // 1. Test with pending /api/pair promise: replaceState MUST fire BEFORE /api/pair resolves (and even before it starts!)
  let resolvePairFetch = null;
  const pairPromise = new Promise((resolve) => { resolvePairFetch = resolve; });

  const env1 = loadWebappMedium({
    noTelegram: true,
    search: '?code=87654321&go=tasks',
    pathname: '/webapp',
    fetchHandler: (url) => {
      if (url === '/api/pair') {
        return pairPromise;
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        clone() { return this; },
        async json() { return { success: true, not_registered: false, user: {}, courses: [], deadlines: [], grades: [], attendance: [], announcements: [], files: [] }; },
      });
    },
  });

  // Immediately after boot() started (while /api/pair is STILL unresolved!):
  assert(
    env1.replaceStateCalls.length >= 1,
    'history.replaceState MUST be called BEFORE pairWithCode fetch resolves'
  );
  const cleanedUrl = env1.replaceStateCalls[0].url;
  assert(!cleanedUrl.includes('code='), `code parameter leaked in replaceState URL: ${cleanedUrl}`);
  assert(!cleanedUrl.includes('87654321'), `pairing secret leaked in replaceState URL: ${cleanedUrl}`);
  assert(cleanedUrl.includes('go=tasks'), `Non-sensitive query param should be preserved: ${cleanedUrl}`);

  const replaceIdx = env1.events.findIndex(e => e.type === 'replaceState');
  const pairFetchIdx = env1.events.findIndex(e => e.type === 'fetch' && e.url === '/api/pair');
  assert(replaceIdx !== -1, 'Expected replaceState event');
  assert(pairFetchIdx !== -1, 'Expected /api/pair fetch event');
  assert(
    replaceIdx < pairFetchIdx,
    `history.replaceState (idx=${replaceIdx}) MUST execute strictly BEFORE /api/pair fetch (idx=${pairFetchIdx})`
  );

  // Now resolve /api/pair
  resolvePairFetch({
    ok: true,
    status: 200,
    clone() { return this; },
    async json() { return { success: true }; },
  });
  await new Promise(r => setImmediate(r));

  // 2. Test when ?code= is the only param and /api/pair rejects: URL is STILL stripped cleanly to /webapp
  const env2 = loadWebappMedium({
    noTelegram: true,
    search: '?code=11223344',
    pathname: '/webapp',
    fetchHandler: async (url) => {
      if (url === '/api/pair') throw new Error('Network down');
      return {
        ok: true,
        status: 200,
        clone() { return this; },
        async json() { return { success: true, not_registered: true }; },
      };
    },
  });
  await new Promise(r => setImmediate(r));
  assert(env2.replaceStateCalls.length >= 1, 'replaceState must run even if pairWithCode throws');
  assert.strictEqual(env2.replaceStateCalls[0].url, '/webapp', `Expected clean /webapp path, got ${env2.replaceStateCalls[0].url}`);

  console.log('M_REF_REPLACESTATE_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"M-REF replaceState test failed:\n{res.stdout}\n{res.stderr}"
    assert "M_REF_REPLACESTATE_OK" in res.stdout


# ── M-DOCS: README Fernet (zero AES-256) + Dynamic Academic Semester Label ──


def test_m_docs_readme_documents_fernet_and_zero_aes256():
    targets = [README_MD, WEBAPP_HTML, *sorted(STATIC_JS_DIR.glob("*.js"))]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"AES[-\s]?256", text, flags=re.IGNORECASE), (
            f"Stale AES-256 claim found in {path.relative_to(REPO_ROOT)}"
        )

    readme = README_MD.read_text(encoding="utf-8")
    assert "Fernet" in readme, "README.md must document Fernet"
    assert "AES-128-CBC" in readme, "README.md must document AES-128-CBC"
    assert "HMAC-SHA256" in readme, "README.md must document HMAC-SHA256"


def test_m_docs_zero_hardcoded_semester_label_in_ui():
    for path in [WEBAPP_HTML, *sorted(STATIC_JS_DIR.glob("*.js"))]:
        text = path.read_text(encoding="utf-8")
        hardcoded_patterns = [
            r"2025\s*/\s*2026",
            r"2026\s*/\s*2027",
            r"٢٠٢٥\s*/\s*٢٠٢٦",
            r"٢٠٢٦\s*/\s*٢٠٢٧",
            r"٢٠٢٦\s*/\s*٢٧",
            r"الفصل\s+(?:الأول|الثاني|الصيفي)\s+[0-9٠-٩]{4}",
        ]
        for pat in hardcoded_patterns:
            m = re.search(pat, text)
            assert m is None, f"Hardcoded semester label '{m.group(0)}' found in {path.relative_to(REPO_ROOT)}"


@requires_node
def test_m_docs_get_current_semester_label_all_seasons():
    js = MEDIUM_NODE_HARNESS + r"""
const assert = require('assert');
const { ctx, VmDate, elements } = loadWebappMedium();

assert.strictEqual(typeof ctx.getCurrentSemesterLabel, 'function', 'getCurrentSemesterLabel must be defined in dates.js');

// Fall (Sep..Dec -> YYYY/(YYYY+1); Jan -> (YYYY-1)/YYYY)
assert.strictEqual(ctx.getCurrentSemesterLabel(new VmDate(2026, 8, 1)), 'الفصل الأول 2026/2027', 'Sep should be Fall YYYY/(YYYY+1)');
assert.strictEqual(ctx.getCurrentSemesterLabel(new VmDate(2026, 9, 15)), 'الفصل الأول 2026/2027', 'Oct should be Fall YYYY/(YYYY+1)');
assert.strictEqual(ctx.getCurrentSemesterLabel(new VmDate(2026, 11, 31)), 'الفصل الأول 2026/2027', 'Dec should be Fall YYYY/(YYYY+1)');
assert.strictEqual(ctx.getCurrentSemesterLabel(new VmDate(2027, 0, 15)), 'الفصل الأول 2026/2027', 'Jan should be Fall (YYYY-1)/YYYY');

// Spring (Feb..Jun -> (YYYY-1)/YYYY)
assert.strictEqual(ctx.getCurrentSemesterLabel(new VmDate(2026, 1, 1)), 'الفصل الثاني 2025/2026', 'Feb should be Spring (YYYY-1)/YYYY');
assert.strictEqual(ctx.getCurrentSemesterLabel(new VmDate(2026, 2, 15)), 'الفصل الثاني 2025/2026', 'Mar should be Spring (YYYY-1)/YYYY');
assert.strictEqual(ctx.getCurrentSemesterLabel(new VmDate(2026, 5, 30)), 'الفصل الثاني 2025/2026', 'Jun should be Spring (YYYY-1)/YYYY');

// Summer (Jul..Aug -> (YYYY-1)/YYYY)
assert.strictEqual(ctx.getCurrentSemesterLabel(new VmDate(2026, 6, 15)), 'الفصل الصيفي 2025/2026', 'Jul should be Summer (YYYY-1)/YYYY');
assert.strictEqual(ctx.getCurrentSemesterLabel(new VmDate(2026, 7, 31)), 'الفصل الصيفي 2025/2026', 'Aug should be Summer (YYYY-1)/YYYY');

// Default argument (no param) returns valid Arabic semester label
const curLabel = ctx.getCurrentSemesterLabel();
assert(/^الفصل (الأول|الثاني|الصيفي) \d{4}\/\d{4}$/.test(curLabel), `Unexpected default semester label: ${curLabel}`);

// UI integration: render() sets #semesterLabel and homePage() renders dynamic semester label
ctx.render();
assert.strictEqual(elements.get('semesterLabel').textContent, curLabel, 'Sidebar #semesterLabel must be populated dynamically');
assert(elements.get('content').innerHTML.includes(curLabel), 'homePage() must display dynamic semester label');

console.log('M_DOCS_SEMESTER_OK');
"""
    res = _run_node(js)
    assert res.returncode == 0, f"M-DOCS semester label test failed:\n{res.stdout}\n{res.stderr}"
    assert "M_DOCS_SEMESTER_OK" in res.stdout

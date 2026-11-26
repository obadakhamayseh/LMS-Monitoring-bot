import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

# Common rule: set test env BEFORE any project imports
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

REPO_ROOT = Path(__file__).resolve().parent.parent
WEBAPP_HTML = REPO_ROOT / "webapp.html"
SW_JS = REPO_ROOT / "sw.js"
STATIC_DIR = REPO_ROOT / "static"

NODE_BIN = shutil.which("node")
requires_node = pytest.mark.skipif(NODE_BIN is None, reason="node binary not found")


def _extract_webapp_script_paths(html_text: str) -> list[Path]:
    """Return local /static/js/*.js files referenced by webapp.html in DOM order."""
    srcs = re.findall(
        r'<script[^>]*\bsrc=["\'](/static/js/[^"\']+)["\'][^>]*>',
        html_text,
        flags=re.IGNORECASE,
    )
    paths: list[Path] = []
    for src in srcs:
        rel = src.lstrip("/")
        p = REPO_ROOT / rel
        assert p.is_file(), f"Missing script referenced in webapp.html: {src}"
        paths.append(p)
    return paths


def _extract_inline_script(html_text: str) -> str:
    """Load webapp frontend JS in DOM order: local <script src='/static/js/...'> plus any inline scripts."""
    parts = [p.read_text(encoding="utf-8") for p in _extract_webapp_script_paths(html_text)]
    inline_scripts = re.findall(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    parts.extend(inline_scripts)
    assert parts, "No frontend JS found via webapp.html"
    return "\n\n".join(parts)


def _all_frontend_sources() -> list[Path]:
    paths = [WEBAPP_HTML]
    paths.extend(_extract_webapp_script_paths(WEBAPP_HTML.read_text(encoding="utf-8")))
    return paths


def _run_node(js_code: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE_BIN, "-e", js_code],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )


# ── (a) Static XSS & unsafe-scheme scan on webapp.html + static/js/*.js ─────


def test_no_inline_event_handlers_with_interpolation():
    for path in _all_frontend_sources():
        text = path.read_text(encoding="utf-8")
        # Match any on<event>="..." or on<event>='...' containing ${
        double_quoted = re.findall(r'\bon[a-z]+\s*=\s*"[^"]*\$\{[^"]*"', text, flags=re.IGNORECASE)
        single_quoted = re.findall(r"\bon[a-z]+\s*=\s*'[^']*\$\{[^']*'", text, flags=re.IGNORECASE)
        offenders = double_quoted + single_quoted
        assert not offenders, f"Found inline on*= handlers interpolating data in {path.name}: {offenders[:5]}"


def test_no_javascript_urls_in_webapp_html():
    for path in _all_frontend_sources():
        text = path.read_text(encoding="utf-8")
        matches = re.findall(r"javascript\s*:", text, flags=re.IGNORECASE)
        assert not matches, f"Found javascript: URI in {path.name}: {matches}"


def test_no_hardcoded_real_telegram_id_in_frontend():
    legacy_owner_id = "".join(["933", "343", "496"])
    scan_paths = [*_all_frontend_sources(), SW_JS]
    app_css = STATIC_DIR / "css" / "app.css"
    if app_css.is_file():
        scan_paths.append(app_css)
    for path in scan_paths:
        text = path.read_text(encoding="utf-8")
        assert legacy_owner_id not in text, f"Hardcoded real Telegram ID found in {path.name}"


# ── (d) Syntax check via node --check ───────────────────────────────────────


@requires_node
def test_extracted_script_and_sw_pass_node_check(tmp_path):
    html = WEBAPP_HTML.read_text(encoding="utf-8")
    for js_path in _extract_webapp_script_paths(html):
        res_mod = subprocess.run(
            [NODE_BIN, "--check", str(js_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        assert res_mod.returncode == 0, f"node --check failed on {js_path.name}:\n{res_mod.stderr}"

    script = _extract_inline_script(html)
    script_file = tmp_path / "webapp_combined.js"
    script_file.write_text(script, encoding="utf-8")

    res_web = subprocess.run(
        [NODE_BIN, "--check", str(script_file)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert res_web.returncode == 0, f"node --check failed on combined webapp script:\n{res_web.stderr}"

    res_sw = subprocess.run(
        [NODE_BIN, "--check", str(SW_JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert res_sw.returncode == 0, f"node --check failed on sw.js:\n{res_sw.stderr}"


# ── Shared minimal DOM + VM loader for Node runtime tests ───────────────────

NODE_HARNESS_PREFIX = r"""
const fs = require('fs');
const vm = require('vm');

function loadWebapp(opts = {}) {
  const html = fs.readFileSync('webapp.html', 'utf8');
  const scriptPaths = [...html.matchAll(/<script[^>]*\bsrc=["'](\/static\/js\/[^"']+)["'][^>]*>/gi)]
    .map(m => m[1].replace(/^\//, ''));
  const inlineMatches = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)]
    .map(m => m[1]);
  const parts = scriptPaths.map(p => fs.readFileSync(p, 'utf8')).concat(inlineMatches);
  if (!parts.length) throw new Error('No local scripts found in webapp.html');
  const scriptCode = parts.join('\n\n');

  const listeners = { click: [], change: [], input: [], keydown: [], error: [] };
  const elements = new Map();
  const fetchCalls = [];
  const openCalls = [];
  const tgOpenLinkCalls = [];
  const toastMessages = [];

  function makeEl(id = '') {
    return {
      id,
      style: {},
      textContent: '',
      innerHTML: '',
      value: '',
      checked: false,
      disabled: false,
      title: '',
      dataset: {},
      classList: { add() {}, remove() {}, contains() { return false; } },
      nextElementSibling: { style: {} },
      click() {},
      closest() { return null; },
    };
  }

  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeEl(id));
      return elements.get(id);
    },
    createElement(tag) {
      return makeEl(tag);
    },
    addEventListener(type, fn) {
      if (!listeners[type]) listeners[type] = [];
      listeners[type].push(fn);
    },
  };

  const storage = new Map();
  const localStorage = {
    getItem(k) { return storage.has(k) ? storage.get(k) : null; },
    setItem(k, v) { storage.set(k, String(v)); },
    removeItem(k) { storage.delete(k); },
  };

  const tgInitData = opts.initData !== undefined
    ? opts.initData
    : 'user=%7B%22id%22%3A1%7D&auth_date=1700000000&hash=deadbeef';

  const tgWebApp = opts.noTelegram ? null : {
    initData: tgInitData,
    initDataUnsafe: { user: { id: 1 } },
    ready() {},
    expand() {},
    setHeaderColor() {},
    setBackgroundColor() {},
    openLink(u) {
      tgOpenLinkCalls.push(u);
      if (opts.tgOpenLinkThrows) throw new Error('Telegram rejected URL');
    },
  };

  const baseFetch = async (input, init = {}) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const hdrs = {};
    if (init && init.headers) {
      if (typeof init.headers.forEach === 'function') {
        init.headers.forEach((v, k) => { hdrs[k.toLowerCase()] = v; });
      } else {
        for (const [k, v] of Object.entries(init.headers)) {
          hdrs[k.toLowerCase()] = v;
        }
      }
    }
    fetchCalls.push({
      url,
      method: (init && init.method) || 'GET',
      headers: hdrs,
      body: init && init.body !== undefined ? String(init.body) : null,
    });
    return {
      ok: true,
      status: 200,
      clone() { return this; },
      async json() { return { success: true, not_registered: false, user: { full_name: 'Test', lms_username: '123', is_monitoring: true, check_interval: 10 }, courses: [], deadlines: [], grades: [], attendance: [], announcements: [], files: [] }; },
    };
  };

  const windowObj = {
    Telegram: tgWebApp ? { WebApp: tgWebApp } : undefined,
    location: { origin: 'https://app.mutah.example', search: '', pathname: '/webapp', protocol: 'https:', reload() {} },
    navigator: { userAgent: 'NodeTest', onLine: true },
    matchMedia: () => ({ matches: false }),
    fetch: baseFetch,
    open(u, target, features) { openCalls.push({ url: u, target, features }); },
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
    navigator: windowObj.navigator,
    history: { replaceState() {} },
    fetch: (...args) => windowObj.fetch(...args),
    Headers,
    URL,
    URLSearchParams,
    Blob,
    console: { log() {}, warn() {}, error() {} },
    confirm: () => true,
    alert: () => { throw new Error('ALERT_EXECUTED'); },
    setTimeout: () => 1,
    clearTimeout: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
  };
  // Ensure global fetch delegates to window.fetch after webapp patches window.fetch
  vm.createContext(ctx);
  vm.runInContext(scriptCode, ctx);

  // Track toast calls if toast exists
  if (typeof ctx.toast === 'function') {
    const origToast = ctx.toast;
    ctx.toast = (msg) => { toastMessages.push(msg); return origToast(msg); };
  }

  return { ctx, listeners, elements, fetchCalls, openCalls, tgOpenLinkCalls, toastMessages };
}
"""


# ── (b) Pure function exploit tests (escapeHTML, safeHttpUrl, openExternalLink) ──


@requires_node
def test_escape_html_and_safe_http_url_and_open_external_link_in_node():
    js = NODE_HARNESS_PREFIX + r"""
const assert = require('assert');

// 1. Test escapeHTML on hostile payloads
{
  const { ctx } = loadWebapp();
  assert.strictEqual(typeof ctx.escapeHTML, 'function', 'escapeHTML must exist');

  const payload = `'"&<>` + '`' + `\\</script>\n<img src=x onerror=alert(1)>`;
  const escaped = ctx.escapeHTML(payload);

  assert(!escaped.includes('<'), 'Unescaped < found');
  assert(!escaped.includes('>'), 'Unescaped > found');
  assert(!escaped.includes('"'), 'Unescaped " found');
  assert(!escaped.includes("'"), "Unescaped ' found");
  assert(!escaped.includes('`'), 'Unescaped backtick found');
  assert(!escaped.includes('\\'), 'Unescaped backslash found');
  assert(!escaped.toLowerCase().includes('</script>'), 'Unescaped </script> found');
  assert(escaped.includes('&amp;'), 'Missing &amp;');
  assert(escaped.includes('&lt;'), 'Missing &lt;');
  assert(escaped.includes('&gt;'), 'Missing &gt;');
  assert(escaped.includes('&quot;'), 'Missing &quot;');
  assert(escaped.includes('&#39;'), 'Missing &#39;');
  assert(escaped.includes('&#96;') || escaped.includes('&#x60;'), 'Missing escaped backtick');
  assert(escaped.includes('&#92;') || escaped.includes('&#x5c;'), 'Missing escaped backslash');
}

// 2. Test safeHttpUrl rejects dangerous schemes & protocol-relative URLs
{
  const { ctx } = loadWebapp();
  assert.strictEqual(typeof ctx.safeHttpUrl, 'function', 'safeHttpUrl must be defined');

  const valid = [
    'https://mulms.mutah.edu.jo/course/view.php?id=71177',
    'http://example.com/path?a=1&b=2',
    '  https://mulms.mutah.edu.jo/mod/assign/view.php?id=9  ',
  ];
  for (const u of valid) {
    const out = ctx.safeHttpUrl(u);
    assert(typeof out === 'string' && /^https?:\/\//.test(out), `Expected valid URL for ${u}, got ${out}`);
  }

  const invalid = [
    'javascript:alert(1)',
    'JAVASCRIPT:alert(1)',
    '  javascript:alert(document.cookie)',
    'java\nscript:alert(1)',
    'java\tscript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
    '//evil',
    '//evil.com/steal',
    'vbscript:msgbox(1)',
    'file:///etc/passwd',
    'https://evil.com/path\njavascript:alert(1)',
    'https://evil.com/path\r\nHeader: injected',
    'http:evil.com',
    '',
    '   ',
    null,
    undefined,
    12345,
  ];
  for (const bad of invalid) {
    const out = ctx.safeHttpUrl(bad);
    assert.strictEqual(out, null, `Expected safeHttpUrl(${JSON.stringify(bad)}) to be null, got ${JSON.stringify(out)}`);
  }
}

// 3. Test openExternalLink with tg.openLink throwing on unsafe AND safe URLs
{
  const { ctx, openCalls, tgOpenLinkCalls } = loadWebapp({ tgOpenLinkThrows: true });
  for (const bad of ['javascript:alert(1)', 'data:text/html,<svg onload=alert(1)>', '//evil']) {
    ctx.openExternalLink(bad);
  }
  assert.strictEqual(tgOpenLinkCalls.length, 0, 'tg.openLink must NOT be called for unsafe URLs');
  assert.strictEqual(openCalls.length, 0, 'window.open must NOT be called when falling back on unsafe URLs');

  // Now test safe URL when tg.openLink throws -> must fall back with noopener,noreferrer
  ctx.openExternalLink('https://mulms.mutah.edu.jo/mod/resource/view.php?id=5');
  assert.strictEqual(tgOpenLinkCalls.length, 1);
  assert.strictEqual(openCalls.length, 1);
  assert.strictEqual(openCalls[0].url, 'https://mulms.mutah.edu.jo/mod/resource/view.php?id=5');
  assert.strictEqual(openCalls[0].target, '_blank');
  assert(
    String(openCalls[0].features || '').includes('noopener') &&
    String(openCalls[0].features || '').includes('noreferrer'),
    `Expected noopener,noreferrer in window.open features, got: ${openCalls[0].features}`
  );
}

console.log('PURE_FUNCTIONS_OK');
"""
    res = _run_node(js)
    assert res.returncode == 0, f"Node test failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "PURE_FUNCTIONS_OK" in res.stdout


# ── (c) Delegated event handler & hostile DOM rendering tests in Node ───────


@requires_node
def test_delegated_click_handler_blocks_hostile_actions_and_urls_in_node():
    js = NODE_HARNESS_PREFIX + r"""
const assert = require('assert');
const { ctx, listeners, openCalls, tgOpenLinkCalls } = loadWebapp({ tgOpenLinkThrows: true });

assert(listeners.click.length >= 1, 'Expected at least one document-level click listener for event delegation');
assert(listeners.change.length >= 1, 'Expected at least one document-level change listener for event delegation');
assert(listeners.keydown.length >= 1, 'Expected at least one document-level keydown listener for event delegation');

function dispatchClick(dataset) {
  const el = {
    dataset,
    closest(selector) {
      if (selector === '[data-action]' && dataset && dataset.action) return el;
      return null;
    },
  };
  const ev = { target: el, preventDefault() {} };
  for (const fn of listeners.click) fn(ev);
}

// 1. Hostile URLs via data-action="open-link" must NEVER open anything
for (const badUrl of ['javascript:alert(1)', 'data:text/html,<script>alert(1)</script>', '//evil', 'https://a.com/\nalert(1)']) {
  dispatchClick({ action: 'open-link', url: badUrl });
}
assert.strictEqual(tgOpenLinkCalls.length, 0, 'tg.openLink was called for hostile data-url');
assert.strictEqual(openCalls.length, 0, 'window.open was called for hostile data-url');

// 2. Hostile / prototype / non-allowlisted action names must NEVER invoke arbitrary functions
let unexpectedCalled = false;
ctx.evilAction = () => { unexpectedCalled = true; };
for (const badAction of ['evilAction', 'eval', 'alert', 'constructor', '__proto__', 'toString', 'valueOf', 'openExternalLink', 'submitLogin', 'fetchUserData']) {
  dispatchClick({ action: badAction, id: '1', url: 'https://example.com' });
}
assert.strictEqual(unexpectedCalled, false, 'Non-allowlisted function was invoked via data-action!');
assert.strictEqual(openCalls.length, 0, 'Non-allowlisted action opened a window');

// 3. Safe URL via data-action="open-link" works
dispatchClick({ action: 'open-link', url: 'https://mulms.mutah.edu.jo/course/view.php?id=99' });
assert.strictEqual(openCalls.length, 1, 'Valid open-link action did not open safe URL');

// 4. Hostile data-id on open-course / task-detail passes literal string safely without executing JS
const hostileId = `1');alert(1);//" onclick="alert(2)\`\\</script>\n`;
dispatchClick({ action: 'open-course', id: hostileId });
dispatchClick({ action: 'task-detail', id: hostileId });

// 5. Render cards with hostile LMS data: zero inline on*= handlers and zero unescaped tags
const hostileCourse = {
  id: hostileId,
  name: `CS101 <script>alert(1)</script> ' " \` \\`,
  code: `" onmouseover="alert(1)`,
  teacher: `Dr. 'Evil' </script>`,
  url: `javascript:alert('xss')`,
  time: `الأحد · 10:00 ' "`,
  is_monitored: true,
  attendance: 5,
  absent: 1,
  total: 6,
  stats: { assignments: 1, quizzes: 0, resources: 1, tasks_total: 1, tasks_done: 0 },
};
const hostileTask = {
  id: hostileId,
  title: `HW1 ' " </script> \` \\`,
  course: hostileId,
  course_name: hostileCourse.name,
  type: 'واجب',
  dueDateObj: null,
  due_str: `Tomorrow <img src=x onerror=alert(1)>`,
  time_remaining: `' "`,
  url: `javascript:alert(1)`,
  done: false,
  grade: { grade_val: `10'`, grade_max: `10"` },
};
const hostileFile = {
  id: hostileId,
  name: `Lecture.pdf ' " </script>`,
  course: hostileCourse.name,
  kind: `PDF"`,
  date: `2026-01-01`,
  url: `javascript:alert(1)`,
};
const hostileAnn = {
  id: hostileId,
  course: hostileId,
  course_name: hostileCourse.name,
  title: `Exam! ' " </script>`,
  author: `Prof 'X'`,
  date: `Today`,
  body: `<script>alert(1)</script>`,
  url: `//evil`,
};

const rendered = [
  ctx.courseCard(hostileCourse),
  ctx.taskCard(hostileTask),
  ctx.fileCard(hostileFile),
  ctx.announceCard(hostileAnn),
].join('\n');

// Inspect every real HTML tag: strip quoted attribute values (none can break out because " is &quot;)
// and verify zero tags define any inline on*= event attribute.
const tags = rendered.match(/<[^>]+>/g) || [];
assert(tags.length > 0, 'Expected rendered HTML tags');
for (const tag of tags) {
  const withoutAttrVals = tag.replace(/"[^"]*"/g, '""').replace(/'[^']*'/g, "''");
  assert(!/\s+on[a-z]+\s*=/i.test(withoutAttrVals), `Tag has inline on*= attribute: ${tag}`);
}
assert(!/<script\b|<\/script>/i.test(rendered), `Rendered card HTML contains unescaped <script> tag:\n${rendered}`);
assert(rendered.includes('data-action="open-link"'), 'Expected data-action="open-link" in fileCard');

// 6. Test delegated change & keydown handlers reject non-allowlisted keys/actions
let prefPolluted = false;
const hostilePrefEl = {
  dataset: { change: 'set-server-pref', key: '__proto__' },
  checked: true,
  closest(sel) { return sel === '[data-change]' ? hostilePrefEl : null; },
};
for (const fn of listeners.change) fn({ target: hostilePrefEl });
assert.strictEqual(({}).polluted, undefined);
assert.strictEqual(prefPolluted, false);

console.log('DELEGATION_OK');
"""
    res = _run_node(js)
    assert res.returncode == 0, f"Node delegation test failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "DELEGATION_OK" in res.stdout


# ── (e) H1 client-side: initData ONLY in X-Telegram-Init-Data header ────────


def test_static_no_initdata_in_query_or_body():
    combined = "\n".join(p.read_text(encoding="utf-8") for p in _all_frontend_sources())
    # Must NOT have `initData:` property in JS objects (URLSearchParams / JSON.stringify payloads)
    obj_props = re.findall(r"\binitData\s*:\s*tg", combined)
    assert not obj_props, f"Found initData still serialized into query/body objects: {obj_props}"

    # Must set X-Telegram-Init-Data header
    assert "X-Telegram-Init-Data" in combined, "frontend JS must send X-Telegram-Init-Data header"

    # sw.js must not depend on initData in URL
    sw = SW_JS.read_text(encoding="utf-8")
    assert "initData" not in sw, "sw.js must not depend on initData in URL"
    assert "'/api/user-data'" in sw or '"/api/user-data"' in sw, "sw.js must key /api/user-data independently of query params"


@requires_node
def test_runtime_all_api_fetches_send_initdata_only_in_header():
    js = NODE_HARNESS_PREFIX + r"""
const assert = require('assert');

(async () => {
  const tokenVal = 'user=%7B%22id%22%3A1%7D&auth_date=1700000000&hash=deadbeef1234';
  const { ctx, elements, fetchCalls } = loadWebapp({ initData: tokenVal });

  // Wait a microtask for boot's initial fetchUserData()
  await new Promise(r => setImmediate(r));

  // Populate inputs for various API actions
  ctx.document.getElementById('loginUser').value = '120240000001';
  ctx.document.getElementById('loginPass').value = 'secret-pass';
  ctx.document.getElementById('newCourseInput').value = '71177';

  // Ensure logged-in state for actions guarded by isLoggedIn
  await ctx.fetchUserData();
  ctx.persist(true);
  await ctx.submitLogin();
  await ctx.submitAddCourse();
  await ctx.syncAllCoursesFromLms();
  await ctx.toggleCourseMonitoring('71177');
  await ctx.removeCourseById('71177');
  await ctx.setServerPref('notify_assign', true);
  await ctx.setServerInterval(10);
  await ctx.setServerMonitoring(true);
  await ctx.toggleDone('assign_1');
  await ctx.syncNow();
  await ctx.logoutAccount();

  const apiCalls = fetchCalls.filter(c => c.url.startsWith('/api/'));
  assert(apiCalls.length >= 10, `Expected >=10 /api/ calls, saw ${apiCalls.length}`);

  const seenPaths = new Set(apiCalls.map(c => c.url.split('?')[0]));
  for (const expected of [
    '/api/user-data',
    '/api/user-meta',
    '/api/login',
    '/api/add-course',
    '/api/sync-courses',
    '/api/toggle-course',
    '/api/delete-course',
    '/api/update-settings',
    '/api/mark-submitted',
    '/api/force-check',
    '/api/logout',
  ]) {
    assert(seenPaths.has(expected), `Missing API call for ${expected}; saw ${[...seenPaths]}`);
  }

  for (const call of apiCalls) {
    assert.strictEqual(
      call.headers['x-telegram-init-data'],
      tokenVal,
      `Missing or wrong X-Telegram-Init-Data header on ${call.method} ${call.url}`
    );
    assert(
      !call.url.includes('initData') && !call.url.includes('deadbeef1234'),
      `initData leaked into URL: ${call.url}`
    );
    if (call.body) {
      assert(
        !call.body.includes('initData') && !call.body.includes('deadbeef1234'),
        `initData leaked into body of ${call.url}: ${call.body}`
      );
    }
  }

  console.log('H1_HEADER_ONLY_OK', apiCalls.length);
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""
    res = _run_node(js)
    assert res.returncode == 0, f"Node H1 header test failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "H1_HEADER_ONLY_OK" in res.stdout


@requires_node
def test_sw_js_caches_user_data_independent_of_initdata_in_url():
    js = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

(async () => {
  const swCode = fs.readFileSync('sw.js', 'utf8');
  const handlers = {};
  const cacheStore = new Map();
  const fetchedReqs = [];

  const fakeCache = {
    async put(key, res) { cacheStore.set(String(key), res); },
    async match(key) { return cacheStore.get(String(key)); },
    async addAll() {},
  };

  const selfObj = {
    location: { origin: 'https://app.mutah.example' },
    addEventListener(type, fn) { handlers[type] = fn; },
    skipWaiting() {},
    clients: { claim() {} },
  };

  const ctx = {
    self: selfObj,
    URL,
    caches: {
      async open() { return fakeCache; },
      async keys() { return []; },
      async delete() {},
      async match() { return undefined; },
    },
    async fetch(req) {
      fetchedReqs.push(req);
      return { ok: true, status: 200, clone() { return { ok: true, status: 200, cached: true }; } };
    },
  };

  vm.createContext(ctx);
  vm.runInContext(swCode, ctx);
  assert.strictEqual(typeof handlers.fetch, 'function', 'sw.js must register fetch handler');

  let responsePromise = null;
  const req = {
    method: 'GET',
    url: 'https://app.mutah.example/api/user-data?tid=1&_t=999999',
    headers: { 'X-Telegram-Init-Data': 'user=%7B%22id%22%3A1%7D&auth_date=1700000000&hash=abc' },
  };
  handlers.fetch({
    request: req,
    respondWith(p) { responsePromise = p; },
  });

  assert(responsePromise, 'sw.js did not intercept GET /api/user-data');
  await responsePromise;
  assert.strictEqual(fetchedReqs.length, 1);
  assert.strictEqual(fetchedReqs[0].headers['X-Telegram-Init-Data'], req.headers['X-Telegram-Init-Data']);
  assert(cacheStore.has('/api/user-data'), 'sw.js must cache under /api/user-data independent of query string');
  console.log('SW_H1_OK');
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""
    res = _run_node(js)
    assert res.returncode == 0, f"Node sw.js test failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "SW_H1_OK" in res.stdout


@requires_node
def test_submit_login_preserves_password_verbatim():
    js = NODE_HARNESS_PREFIX + r"""
const assert = require('assert');

(async () => {
  const { ctx, fetchCalls } = loadWebapp();
  await new Promise(r => setImmediate(r));
  fetchCalls.length = 0;

  // Empty password must be rejected without calling /api/login
  ctx.document.getElementById('loginUser').value = '  120240000001  ';
  ctx.document.getElementById('loginPass').value = '';
  await ctx.submitLogin();
  assert.strictEqual(
    fetchCalls.filter(c => c.url === '/api/login').length,
    0,
    'Empty password must not trigger /api/login'
  );

  // Whitespace-only or space-padded + Arabic-Indic password must be sent byte-for-byte unchanged
  const rawPass = '  p@ss١٢٣\t ';
  ctx.document.getElementById('loginUser').value = '  120240000001  ';
  ctx.document.getElementById('loginPass').value = rawPass;
  await ctx.submitLogin();

  const loginCalls = fetchCalls.filter(c => c.url === '/api/login');
  assert.strictEqual(loginCalls.length, 1, 'Expected one /api/login call');
  const parsed = JSON.parse(loginCalls[0].body);
  assert.strictEqual(parsed.username, '120240000001', 'Username should be trimmed');
  assert.strictEqual(parsed.password, rawPass, 'Password must be sent verbatim without .trim()');
  console.log('LOGIN_PASS_VERBATIM_OK');
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""
    res = _run_node(js)
    assert res.returncode == 0, f"Node login password test failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "LOGIN_PASS_VERBATIM_OK" in res.stdout



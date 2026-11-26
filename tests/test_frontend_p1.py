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
SW_JS = REPO_ROOT / "sw.js"

NODE_BIN = shutil.which("node")
requires_node = pytest.mark.skipif(NODE_BIN is None, reason="node binary not found")


def _extract_inline_script(html_text: str) -> str:
    scripts = re.findall(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert scripts, "No inline <script> found in webapp.html"
    return "\n\n".join(scripts)


def _run_node(js_code: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE_BIN, "-e", js_code],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )


P1_NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

function loadWebappP1(opts = {}) {
  const html = fs.readFileSync('webapp.html', 'utf8');
  const scriptMatches = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)];
  if (!scriptMatches.length) throw new Error('No inline script in webapp.html');
  const scriptCode = scriptMatches.map(m => m[1]).join('\n\n');

  const listeners = { click: [], change: [], input: [], keydown: [], error: [], DOMContentLoaded: [] };
  const elements = new Map();
  const fetchCalls = [];
  const toastMessages = [];
  const swMessages = [];
  let reloaded = false;

  const cacheMap = new Map();
  for (const [k, v] of Object.entries(opts.initialCaches || {})) {
    cacheMap.set(k, new Map(Object.entries(v || {})));
  }

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
      dataset: {},
      focused: false,
      classList: { add() {}, remove() {}, contains() { return false; } },
      nextElementSibling: { style: {} },
      setAttribute(k, v) { attrs[k] = String(v); },
      getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
      focus() { el.focused = true; document.activeElement = el; },
      querySelector() { return null; },
      click() {},
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
      return makeEl(tag);
    },
    addEventListener(type, fn) {
      if (!listeners[type]) listeners[type] = [];
      listeners[type].push(fn);
    },
  };

  const storage = new Map();
  for (const [k, v] of Object.entries(opts.initialStorage || {})) {
    storage.set(k, String(v));
  }
  const localStorage = {
    getItem(k) { return storage.has(k) ? storage.get(k) : null; },
    setItem(k, v) { storage.set(k, String(v)); },
    removeItem(k) { storage.delete(k); },
    _map: storage,
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
    openLink() {},
  };

  const fetchImpl = opts.fetchHandler || (async (url, init) => ({
    ok: true,
    status: 200,
    clone() { return this; },
    async json() {
      return {
        success: true,
        not_registered: false,
        user: { full_name: 'Test', lms_username: '123', is_monitoring: true, check_interval: 10 },
        courses: [],
        deadlines: [],
        grades: [],
        attendance: [],
        announcements: [],
        files: [],
      };
    },
  }));

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
      credentials: init && init.credentials,
      headers: hdrs,
      body: init && init.body !== undefined ? String(init.body) : null,
    });
    return fetchImpl(url, init);
  };

  const cachesApi = {
    async keys() { return [...cacheMap.keys()]; },
    async delete(k) { return cacheMap.delete(k); },
    async open(k) {
      if (!cacheMap.has(k)) cacheMap.set(k, new Map());
      const m = cacheMap.get(k);
      return {
        async put(rk, val) { m.set(String(rk), val); },
        async match(rk) { return m.get(String(rk)); },
        async delete(rk) { return m.delete(String(rk)); },
      };
    },
  };

  const windowObj = {
    Telegram: tgWebApp ? { WebApp: tgWebApp } : undefined,
    location: {
      origin: 'https://app.mutah.example',
      search: opts.search || '',
      pathname: '/webapp',
      protocol: 'https:',
      reload() { reloaded = true; },
    },
    navigator: {
      userAgent: 'NodeTest',
      onLine: true,
      serviceWorker: {
        register: async () => ({}),
        controller: { postMessage(msg) { swMessages.push(msg); } },
      },
    },
    caches: cachesApi,
    matchMedia: () => ({ matches: false }),
    fetch: baseFetch,
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
    caches: cachesApi,
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
    windowObj,
    baseFetch,
    document,
    listeners,
    elements,
    fetchCalls,
    toastMessages,
    swMessages,
    cacheMap,
    storage,
    isReloaded: () => reloaded,
  };
}
"""


# ── Syntax check ────────────────────────────────────────────────────────────


@requires_node
def test_p1_syntax_node_check(tmp_path):
    html = WEBAPP_HTML.read_text(encoding="utf-8")
    script_file = tmp_path / "webapp_p1.js"
    script_file.write_text(_extract_inline_script(html), encoding="utf-8")

    r1 = subprocess.run([NODE_BIN, "--check", str(script_file)], capture_output=True, text=True, timeout=15)
    assert r1.returncode == 0, f"webapp.html inline JS syntax error:\n{r1.stderr}"

    r2 = subprocess.run([NODE_BIN, "--check", str(SW_JS)], capture_output=True, text=True, timeout=15)
    assert r2.returncode == 0, f"sw.js syntax error:\n{r2.stderr}"


# ── H5: item_url, sync_logs omitted from persist(), toggleDone(0) payload ───


@requires_node
def test_h5_grade_links_use_item_url():
    js = P1_NODE_HARNESS + r"""
const assert = require('assert');
(async () => {
  const { ctx, elements } = loadWebappP1({
    fetchHandler: async (url) => ({
      ok: true,
      status: 200,
      clone() { return this; },
      async json() {
        return {
          success: true,
          user: { full_name: 'Student', lms_username: '12024', is_monitoring: true, check_interval: 10 },
          courses: [{ id: 101, name: 'Algorithms', is_monitored: true }],
          deadlines: [],
          grades: [
            {
              item_id: 'g1',
              course_id: 101,
              course_name: 'Algorithms',
              item_name: 'Midterm Exam',
              grade_val: '28',
              grade_max: '30',
              percentage: '93%',
              item_url: 'https://mulms.mutah.edu.jo/mod/quiz/view.php?id=555',
            },
            {
              item_id: 'g2',
              course_id: 101,
              course_name: 'Algorithms',
              item_name: 'Legacy Fallback Grade',
              grade_val: '10',
              grade_max: '10',
              url: 'https://mulms.mutah.edu.jo/mod/assign/view.php?id=556',
            },
          ],
          attendance: [],
          announcements: [],
          files: [],
        };
      },
    }),
  });

  await ctx.fetchUserData();
  ctx.go('grades');
  ctx.filterGrades();
  const tableHtml = elements.get('gradeTable').innerHTML;
  assert(
    tableHtml.includes('https://mulms.mutah.edu.jo/mod/quiz/view.php?id=555'),
    `Expected item_url in grade table HTML, got:\n${tableHtml}`
  );
  assert(
    tableHtml.includes('https://mulms.mutah.edu.jo/mod/assign/view.php?id=556'),
    `Expected graceful fallback url in grade table HTML, got:\n${tableHtml}`
  );
  console.log('H5_ITEM_URL_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H5 item_url failed:\n{res.stdout}\n{res.stderr}"
    assert "H5_ITEM_URL_OK" in res.stdout


@requires_node
def test_h5_persist_omits_sync_logs_and_empty_reminders_not_resurrected():
    js = P1_NODE_HARNESS + r"""
const assert = require('assert');
(async () => {
  let metaCalls = 0;
  const { ctx, fetchCalls, storage } = loadWebappP1({
    initialStorage: {
      mutahStudentNotebook: JSON.stringify({
        reminders: [{ title: 'Old Reminder', when: 'Tomorrow' }],
      }),
    },
    fetchHandler: async (url) => ({
      ok: true,
      status: 200,
      clone() { return this; },
      async json() {
        if (url.startsWith('/api/user-data')) {
          return {
            success: true,
            user: { full_name: 'Student', lms_username: '12024', is_monitoring: true, check_interval: 10 },
            user_meta: {
              personal_reminders: [],
              sync_logs: [{ status: 'OK', time: '2026-11-26T10:00:00Z', detail: 'server log' }],
            },
            courses: [], deadlines: [], grades: [], attendance: [], announcements: [], files: [],
          };
        }
        metaCalls++;
        return { success: true };
      },
    }),
  });

  await ctx.fetchUserData();
  fetchCalls.length = 0;
  ctx.persist(true);
  await new Promise(r => setImmediate(r));

  const storedObj = JSON.parse(storage.get('mutahStudentNotebook') || '{}');
  assert(!('logs' in storedObj), `persist() must NOT store logs in localStorage: ${JSON.stringify(storedObj)}`);
  assert(!('sync_logs' in storedObj), `persist() must NOT store sync_logs in localStorage: ${JSON.stringify(storedObj)}`);
  assert.deepStrictEqual(storedObj.reminders, [], 'Empty personal_reminders from server must clear local reminders');

  const metaReq = fetchCalls.find(c => c.url === '/api/user-meta');
  assert(metaReq, 'Expected POST /api/user-meta');
  const sentBody = JSON.parse(metaReq.body);
  assert(!('sync_logs' in (sentBody.meta || {})), `persist() must NOT send sync_logs to /api/user-meta: ${metaReq.body}`);
  console.log('H5_PERSIST_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H5 persist failed:\n{res.stdout}\n{res.stderr}"
    assert "H5_PERSIST_OK" in res.stdout


@requires_node
def test_h5_toggle_done_zero_sends_task_identity():
    js = P1_NODE_HARNESS + r"""
const assert = require('assert');
(async () => {
  const { ctx, fetchCalls } = loadWebappP1({
    fetchHandler: async (url) => ({
      ok: true,
      status: 200,
      clone() { return this; },
      async json() {
        if (url.startsWith('/api/user-data')) {
          return {
            success: true,
            user: { full_name: 'Student', lms_username: '12024', is_monitoring: true, check_interval: 10 },
            courses: [{ id: 71177, name: 'OS', is_monitored: true }],
            deadlines: [{
              id: 0,
              content_id: 'assign_888',
              content_type: 'assign',
              content_name: 'Lab 3',
              content_url: 'https://mulms.mutah.edu.jo/mod/assign/view.php?id=888',
              course_id: 71177,
              course_name: 'OS',
              due_str: '',
              due_ts: 0,
              is_submitted: true,
            }],
            grades: [], attendance: [], announcements: [], files: [],
          };
        }
        return { success: true };
      },
    }),
  });

  await ctx.fetchUserData();
  fetchCalls.length = 0;

  // Task is currently submitted (done=true), toggling sends is_submitted: 0
  await ctx.toggleDone('assign_888');

  const call = fetchCalls.find(c => c.url === '/api/mark-submitted');
  assert(call, 'Expected POST /api/mark-submitted');
  const body = JSON.parse(call.body);
  assert.strictEqual(body.is_submitted, 0, 'Expected is_submitted=0');
  assert.strictEqual(Number(body.course_id), 71177, `Expected course_id=71177, got ${JSON.stringify(body)}`);
  assert.strictEqual(body.item_key, 'assign_888', `Expected item_key='assign_888', got ${JSON.stringify(body)}`);
  assert.strictEqual(body.title, 'Lab 3', `Expected title='Lab 3', got ${JSON.stringify(body)}`);
  assert.strictEqual(body.url, 'https://mulms.mutah.edu.jo/mod/assign/view.php?id=888', `Expected url, got ${JSON.stringify(body)}`);
  assert.strictEqual(body.item_type, 'assign', `Expected item_type='assign', got ${JSON.stringify(body)}`);
  console.log('H5_TOGGLE_DONE_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H5 toggleDone(0) failed:\n{res.stdout}\n{res.stderr}"
    assert "H5_TOGGLE_DONE_OK" in res.stdout


# ── H2: clearClientSession() + sw.js never caches 401/403/500 ───────────────


@requires_node
def test_h2_clear_client_session_on_logout_unlink_and_401():
    js = P1_NODE_HARNESS + r"""
const assert = require('assert');
(async () => {
  // 1. Test logoutAccount calls clearClientSession (clears localStorage + data-* caches, preserves shell-*)
  {
    const { ctx, cacheMap, storage } = loadWebappP1({
      initialStorage: { mutahStudentNotebook: JSON.stringify({ done: ['a1'], notes: { a1: 'secret' } }) },
      initialCaches: { 'data-v2': { '/api/user-data': 'cached' }, 'data-v3': { '/api/user-data': 'cached2' }, 'shell-v3': { '/webapp': 'ok' } },
    });
    assert.strictEqual(typeof ctx.clearClientSession, 'function', 'clearClientSession must exist');
    await ctx.fetchUserData();
    await ctx.logoutAccount();
    assert.strictEqual(storage.has('mutahStudentNotebook'), false, 'logoutAccount must clear mutahStudentNotebook');
    assert.strictEqual(cacheMap.has('data-v2'), false, 'logoutAccount must delete data-v2 cache');
    assert.strictEqual(cacheMap.has('data-v3'), false, 'logoutAccount must delete data-v3 cache');
    assert.strictEqual(cacheMap.has('shell-v3'), true, 'clearClientSession must preserve non-data-* caches');
  }

  // 2. Test unlinkThisDevice calls clearClientSession
  {
    const { ctx, cacheMap, storage, isReloaded } = loadWebappP1({
      noTelegram: true,
      initialStorage: { mutahStudentNotebook: JSON.stringify({ notes: { x: 'private' } }) },
      initialCaches: { 'data-v3': { '/api/user-data': 'cached' }, 'fonts-v3': { 'f': 'ok' } },
    });
    await ctx.unlinkThisDevice();
    assert.strictEqual(storage.has('mutahStudentNotebook'), false, 'unlinkThisDevice must clear mutahStudentNotebook');
    assert.strictEqual(cacheMap.has('data-v3'), false, 'unlinkThisDevice must delete data-* caches');
    assert.strictEqual(cacheMap.has('fonts-v3'), true, 'unlinkThisDevice must preserve fonts cache');
    assert.strictEqual(isReloaded(), true, 'unlinkThisDevice should reload');
  }

  // 3. Test ANY 401 API response triggers clearClientSession
  {
    let return401 = false;
    const { ctx, cacheMap, storage } = loadWebappP1({
      initialStorage: { mutahStudentNotebook: JSON.stringify({ notes: { n: 'sensitive' } }) },
      initialCaches: { 'data-v3': { '/api/user-data': 'cached' }, 'shell-v3': { '/webapp': 'ok' } },
      fetchHandler: async () => {
        if (return401) {
          return {
            ok: false,
            status: 401,
            clone() { return this; },
            async json() { return { success: false, auth_required: true }; },
          };
        }
        return {
          ok: true,
          status: 200,
          clone() { return this; },
          async json() { return { success: true, user: { full_name: 'U' }, courses: [{ id: 1, name: 'C' }], deadlines: [], grades: [], attendance: [], announcements: [], files: [] }; },
        };
      },
    });
    await ctx.fetchUserData();
    return401 = true;
    await ctx.apiFetch('/api/force-check', { method: 'POST', body: '{}' });
    assert.strictEqual(storage.has('mutahStudentNotebook'), false, '401 must clear mutahStudentNotebook');
    assert.strictEqual(cacheMap.has('data-v3'), false, '401 must delete data-* caches');
    assert.strictEqual(cacheMap.has('shell-v3'), true, '401 must preserve shell cache');
  }

  console.log('H2_CLEAR_SESSION_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H2 clearClientSession failed:\n{res.stdout}\n{res.stderr}"
    assert "H2_CLEAR_SESSION_OK" in res.stdout


@requires_node
def test_h2_sw_never_caches_401_403_500_and_evicts_on_auth_error():
    js = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

(async () => {
  const swCode = fs.readFileSync('sw.js', 'utf8');
  const handlers = {};
  const cachesByName = new Map();
  let nextResponse = { ok: true, status: 200, body: 'initial' };

  function getCache(name) {
    if (!cachesByName.has(name)) cachesByName.set(name, new Map());
    const store = cachesByName.get(name);
    return {
      async put(k, res) { store.set(typeof k === 'string' ? k : k.url, res); },
      async match(k) { return store.get(typeof k === 'string' ? k : k.url); },
      async delete(k) { return store.delete(typeof k === 'string' ? k : k.url); },
      async addAll() {},
    };
  }

  const ctx = {
    self: {
      location: { origin: 'https://app.mutah.example' },
      addEventListener(type, fn) { handlers[type] = fn; },
      skipWaiting() {},
      clients: { claim() {} },
    },
    URL,
    caches: {
      async open(name) { return getCache(name); },
      async keys() { return [...cachesByName.keys()]; },
      async delete(name) { return cachesByName.delete(name); },
      async match(req) {
        for (const m of cachesByName.values()) {
          const key = typeof req === 'string' ? req : req.url;
          if (m.has(key)) return m.get(key);
        }
        return undefined;
      },
    },
    async fetch() {
      const r = Object.assign({}, nextResponse);
      r.clone = () => Object.assign({}, r);
      return r;
    },
  };
  vm.createContext(ctx);
  vm.runInContext(swCode, ctx);

  async function dispatchFetch(url) {
    let p = null;
    handlers.fetch({
      request: { method: 'GET', url },
      respondWith(prom) { p = prom; },
    });
    assert(p, `SW did not respondWith for ${url}`);
    return await p;
  }

  // Seed /api/user-data with 200
  nextResponse = { ok: true, status: 200, body: 'good' };
  await dispatchFetch('https://app.mutah.example/api/user-data');
  const dataCacheName = [...cachesByName.keys()].find(k => k.startsWith('data-'));
  assert(dataCacheName, 'Expected data-* cache');
  assert(cachesByName.get(dataCacheName).has('/api/user-data'), '200 should be cached');

  // 401 must NOT be cached AND must evict stale cached user-data
  nextResponse = { ok: false, status: 401, body: 'unauth' };
  const r401 = await dispatchFetch('https://app.mutah.example/api/user-data');
  assert.strictEqual(r401.status, 401);
  assert(!cachesByName.get(dataCacheName).has('/api/user-data'), '401 must evict /api/user-data from data cache');

  // Re-seed then test 403 eviction
  nextResponse = { ok: true, status: 200, body: 'good2' };
  await dispatchFetch('https://app.mutah.example/api/user-data');
  nextResponse = { ok: false, status: 403, body: 'forbidden' };
  const r403 = await dispatchFetch('https://app.mutah.example/api/user-data');
  assert.strictEqual(r403.status, 403);
  assert(!cachesByName.get(dataCacheName).has('/api/user-data'), '403 must evict /api/user-data from data cache');

  // 500 on /api/user-data must NOT be cached
  nextResponse = { ok: false, status: 500, body: 'err' };
  await dispatchFetch('https://app.mutah.example/api/user-data');
  assert(!cachesByName.get(dataCacheName).has('/api/user-data'), '500 must NOT be cached in data cache');

  // 500 on Google Fonts must NOT be cached
  nextResponse = { ok: false, status: 500, body: 'font-err' };
  await dispatchFetch('https://fonts.googleapis.com/css2?family=Test');
  const fontCacheName = [...cachesByName.keys()].find(k => k.startsWith('fonts-'));
  assert(
    !fontCacheName || !cachesByName.get(fontCacheName).has('https://fonts.googleapis.com/css2?family=Test'),
    'SW must NOT cache 500 responses from fonts.googleapis.com'
  );

  // 404/500 on /logo.png must NOT be cached
  nextResponse = { ok: false, status: 404, body: 'missing' };
  await dispatchFetch('https://app.mutah.example/logo.png');
  for (const [, m] of cachesByName.entries()) {
    assert(!m.has('https://app.mutah.example/logo.png') && !m.has('/logo.png'), '404 /logo.png must NOT be cached');
  }

  console.log('H2_SW_NO_ERROR_CACHE_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H2 sw.js cache policy failed:\n{res.stdout}\n{res.stderr}"
    assert "H2_SW_NO_ERROR_CACHE_OK" in res.stdout


# ── H10: safe JSON.parse, parseAnyDate, dueInfo, rollback ───────────────────


@requires_node
def test_h10_corrupted_localstorage_does_not_crash():
    js = P1_NODE_HARNESS + r"""
const assert = require('assert');
for (const bad of ['{corrupted json!!!', 'null', '[1,2,3]', '12345', 'undefined']) {
  const { ctx } = loadWebappP1({
    initialStorage: { mutahStudentNotebook: bad },
  });
  assert.strictEqual(typeof ctx.render, 'function', `App crashed booting with localStorage=${bad}`);
}
console.log('H10_CORRUPT_LS_OK');
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H10 corrupted localStorage failed:\n{res.stdout}\n{res.stderr}"
    assert "H10_CORRUPT_LS_OK" in res.stdout


@requires_node
def test_h10_parse_any_date_arabic_yearless_and_safari():
    js = P1_NODE_HARNESS + r"""
const assert = require('assert');
const { ctx, VmDate } = loadWebappP1();
const isValidDate = (d) => Boolean(d && typeof d.getTime === 'function' && !isNaN(d.getTime()));

// 1. Compound Arabic Levantine months + Arabic-Indic digits + AM/PM
const d1 = ctx.parseAnyDate('15 كانون الثاني 2027، 11:59 مساءً');
assert(isValidDate(d1), 'Failed to parse كانون الثاني');
assert.strictEqual(d1.getFullYear(), 2027);
assert.strictEqual(d1.getMonth(), 0); // January
assert.strictEqual(d1.getDate(), 15);
assert.strictEqual(d1.getHours(), 23);
assert.strictEqual(d1.getMinutes(), 59);

const d2 = ctx.parseAnyDate('٢٠ تشرين الثاني ٢٠٢٦، ٩:٣٠ صباحاً');
assert(isValidDate(d2), 'Failed to parse تشرين الثاني with Arabic digits');
assert.strictEqual(d2.getFullYear(), 2026);
assert.strictEqual(d2.getMonth(), 10); // November
assert.strictEqual(d2.getDate(), 20);
assert.strictEqual(d2.getHours(), 9);
assert.strictEqual(d2.getMinutes(), 30);

const d3 = ctx.parseAnyDate('5 كانون الأول 2026، 12:15 م');
assert(isValidDate(d3), 'Failed to parse كانون الأول');
assert.strictEqual(d3.getFullYear(), 2026);
assert.strictEqual(d3.getMonth(), 11); // December
assert.strictEqual(d3.getDate(), 5);
assert.strictEqual(d3.getHours(), 12);
assert.strictEqual(d3.getMinutes(), 15);

const d4 = ctx.parseAnyDate('12 إبريل 2026، 12:30 ص');
assert(isValidDate(d4), 'Failed to parse إبريل');
assert.strictEqual(d4.getMonth(), 3); // April
assert.strictEqual(d4.getHours(), 0);
assert.strictEqual(d4.getMinutes(), 30);

// 2. Yearless nearest-year calculation across Dec -> Jan boundary
const dec2026 = new VmDate(2026, 11, 20, 12, 0, 0); // Dec 20, 2026
const origNow = VmDate.now;
VmDate.now = () => dec2026.getTime();
try {
  const janNext = ctx.parseAnyDate('Saturday, 10 January, 11:59 PM');
  assert(isValidDate(janNext), 'Failed yearless Jan in Dec');
  assert.strictEqual(janNext.getFullYear(), 2027, `Expected nearest year 2027 in Dec 2026, got ${janNext.getFullYear()}`);
  assert.strictEqual(janNext.getMonth(), 0);
  assert.strictEqual(janNext.getDate(), 10);

  const janAr = ctx.parseAnyDate('10 كانون الثاني، 11:59 مساءً');
  assert(isValidDate(janAr), 'Failed yearless Arabic Jan in Dec');
  assert.strictEqual(janAr.getFullYear(), 2027, `Expected nearest year 2027 for Arabic Jan in Dec, got ${janAr.getFullYear()}`);
} finally {
  VmDate.now = origNow;
}

const jan2027 = new VmDate(2027, 0, 5, 12, 0, 0); // Jan 5, 2027
const decPrev = ctx.parseAnyDate('Monday, 28 December, 11:59 PM', null, jan2027);
assert(isValidDate(decPrev), 'Failed yearless Dec in Jan');
assert.strictEqual(decPrev.getFullYear(), 2026, `Expected nearest year 2026 in Jan 2027, got ${decPrev.getFullYear()}`);

// 3. Safari-incompatible "YYYY-MM-DD HH:MM" parsed via explicit numeric constructor (no space-separated Date string)
const saf = ctx.parseAnyDate('2026-11-26 14:35:09');
assert(isValidDate(saf), 'Failed Safari YYYY-MM-DD HH:MM');
assert.strictEqual(saf.getFullYear(), 2026);
assert.strictEqual(saf.getMonth(), 10);
assert.strictEqual(saf.getDate(), 26);
assert.strictEqual(saf.getHours(), 14);
assert.strictEqual(saf.getMinutes(), 35);
assert.strictEqual(saf.getSeconds(), 9);

console.log('H10_PARSE_DATE_OK');
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H10 parseAnyDate failed:\n{res.stdout}\n{res.stderr}"
    assert "H10_PARSE_DATE_OK" in res.stdout


@requires_node
def test_h10_due_info_accuracy_and_no_dead_branch():
    html = WEBAPP_HTML.read_text(encoding="utf-8")
    m = re.search(r"function\s+dueInfo\s*\([^)]*\)\s*\{.*?\n\}", html, flags=re.DOTALL)
    assert m, "dueInfo function not found"
    due_src = m.group(0)
    assert "Math.ceil" not in due_src, "dueInfo must not use Math.ceil"
    assert "days <= 0" not in due_src, "dueInfo must not contain dead days <= 0 branch"

    js = P1_NODE_HARNESS + r"""
const assert = require('assert');
const { ctx, VmDate } = loadWebappP1();

// Fix VM "now" to 2026-11-26 10:00:00 local time
const fixedNow = new VmDate(2026, 10, 26, 10, 0, 0).getTime();
const origNow = VmDate.now;
VmDate.now = () => fixedNow;

try {
  // 1. Overdue (-10 min)
  const [lblLate, clsLate] = ctx.dueInfo({ id: 't1', done: false, dueDateObj: new VmDate(fixedNow - 600000) });
  assert.strictEqual(clsLate, 'pill red', `Expected overdue pill red, got ${clsLate}`);
  assert(lblLate.includes('متأخر'), `Expected overdue label, got ${lblLate}`);

  // 2. Due in 2 hours (same calendar day) must NOT say 'غداً'!
  const [lbl2h, cls2h] = ctx.dueInfo({ id: 't2', done: false, dueDateObj: new VmDate(fixedNow + 2 * 3600000) });
  assert.strictEqual(cls2h, 'pill warn');
  assert(!lbl2h.includes('غداً'), `Task due in 2 hours must NOT say 'غداً', got: ${lbl2h}`);
  assert(lbl2h.includes('اليوم') || lbl2h.includes('س'), `Task due in 2 hours should indicate today/hours, got: ${lbl2h}`);

  // 3. Due in 30 minutes must NOT say 'غداً'!
  const [lbl30m, cls30m] = ctx.dueInfo({ id: 't3', done: false, dueDateObj: new VmDate(fixedNow + 30 * 60000) });
  assert.strictEqual(cls30m, 'pill warn');
  assert(!lbl30m.includes('غداً'), `Task due in 30 min must NOT say 'غداً', got: ${lbl30m}`);

  // 4. Due tomorrow (next calendar day, +26h)
  const [lblTom, clsTom] = ctx.dueInfo({ id: 't4', done: false, dueDateObj: new VmDate(2026, 10, 27, 12, 0, 0) });
  assert.strictEqual(clsTom, 'pill warn');
  assert(lblTom.includes('غداً'), `Task due tomorrow should say 'غداً', got: ${lblTom}`);

  // 5. Due in 2 calendar days
  const [lbl2d, cls2d] = ctx.dueInfo({ id: 't5', done: false, dueDateObj: new VmDate(2026, 10, 28, 12, 0, 0) });
  assert.strictEqual(cls2d, 'pill warn');
  assert(lbl2d.includes('يومين'), `Task due in 2 days should say 'يومين', got: ${lbl2d}`);
} finally {
  VmDate.now = origNow;
}

console.log('H10_DUE_INFO_OK');
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H10 dueInfo failed:\n{res.stdout}\n{res.stderr}"
    assert "H10_DUE_INFO_OK" in res.stdout


@requires_node
def test_h10_api_mutations_rollback_on_error_and_network_failure():
    js = P1_NODE_HARNESS + r"""
const assert = require('assert');
(async () => {
  let failMode = null; // 'http500' | 'network' | null
  const { ctx, evalInCtx } = loadWebappP1({
    fetchHandler: async (url) => {
      if (url.startsWith('/api/user-data')) {
        return {
          ok: true, status: 200, clone() { return this; },
          async json() {
            return {
              success: true,
              user: { full_name: 'S', lms_username: '1', is_monitoring: true, check_interval: 10, notify_assign: true },
              courses: [{ id: 55, name: 'AI', is_monitored: true }],
              deadlines: [{ id: 1, content_id: 'assign_55', content_type: 'assign', content_name: 'HW', course_id: 55, is_submitted: false }],
              grades: [], attendance: [], announcements: [], files: [],
            };
          },
        };
      }
      if (failMode === 'network') throw new Error('Network down');
      if (failMode === 'http500') {
        return { ok: false, status: 500, clone() { return this; }, async json() { return { success: false, error: 'DB error' }; } };
      }
      return { ok: true, status: 200, clone() { return this; }, async json() { return { success: true }; } };
    },
  });

  await ctx.fetchUserData();

  for (const mode of ['http500', 'network']) {
    failMode = mode;

    // toggleDone rollback
    await ctx.toggleDone('assign_55');
    assert.strictEqual(ctx.isTaskDone({ id: 'assign_55', done: false }), false, `toggleDone must rollback on ${mode}`);

    // setServerPref rollback
    await ctx.setServerPref('notify_assign', false);
    assert.strictEqual(evalInCtx('userProfile.notify_assign'), true, `setServerPref must rollback on ${mode}`);

    // setServerInterval rollback
    await ctx.setServerInterval(30);
    assert.strictEqual(evalInCtx('userProfile.check_interval'), 10, `setServerInterval must rollback on ${mode}`);

    // setServerMonitoring rollback
    await ctx.setServerMonitoring(false);
    assert.strictEqual(evalInCtx('userProfile.is_monitoring'), true, `setServerMonitoring must rollback on ${mode}`);

    // toggleCourseMonitoring rollback
    await ctx.toggleCourseMonitoring('55');
    assert.strictEqual(ctx.course('55').is_monitored, true, `toggleCourseMonitoring must rollback on ${mode}`);
  }

  console.log('H10_ROLLBACK_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H10 rollback failed:\n{res.stdout}\n{res.stderr}"
    assert "H10_ROLLBACK_OK" in res.stdout


# ── H14: sw.js /logo.png + partial precache + webapp viewport/defer/ARIA/Esc ─


@requires_node
def test_h14_sw_precaches_and_serves_logo_and_survives_failed_asset():
    js = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

(async () => {
  const swCode = fs.readFileSync('sw.js', 'utf8');
  const handlers = {};
  const cachesByName = new Map();
  let offline = false;

  function getCache(name) {
    if (!cachesByName.has(name)) cachesByName.set(name, new Map());
    const store = cachesByName.get(name);
    return {
      async put(k, res) { store.set(typeof k === 'string' ? k : k.url, res); },
      async match(k) {
        const key = typeof k === 'string' ? k : k.url;
        if (store.has(key)) return store.get(key);
        try {
          const p = new URL(key).pathname;
          return store.get(p);
        } catch { return undefined; }
      },
      async delete(k) { return store.delete(typeof k === 'string' ? k : k.url); },
      async addAll() { throw new Error('addAll should not abort install when one asset fails'); },
    };
  }

  const ctx = {
    self: {
      location: { origin: 'https://app.mutah.example' },
      addEventListener(type, fn) { handlers[type] = fn; },
      skipWaiting() {},
      clients: { claim() {} },
    },
    URL,
    caches: {
      async open(name) { return getCache(name); },
      async keys() { return [...cachesByName.keys()]; },
      async delete(name) { return cachesByName.delete(name); },
      async match(req) {
        for (const [name] of cachesByName.entries()) {
          const hit = await getCache(name).match(req);
          if (hit) return hit;
        }
        return undefined;
      },
    },
    async fetch(req) {
      if (offline) throw new Error('OFFLINE');
      const u = typeof req === 'string' ? req : req.url;
      if (u.includes('icon-192.png')) {
        throw new Error('Missing icon');
      }
      return {
        ok: true,
        status: 200,
        body: 'asset:' + u,
        clone() { return { ok: true, status: 200, body: 'asset:' + u }; },
      };
    },
  };
  vm.createContext(ctx);
  vm.runInContext(swCode, ctx);

  // Trigger install: one asset throws, other assets (incl. /logo.png) MUST still be cached!
  let installPromise = null;
  handlers.install({ waitUntil(p) { installPromise = p; } });
  assert(installPromise, 'install must call waitUntil');
  await installPromise;

  const shellName = [...cachesByName.keys()].find(k => k.startsWith('shell-'));
  assert(shellName, 'Expected shell-* cache');
  const shellStore = cachesByName.get(shellName);
  assert(shellStore.has('/logo.png'), `Expected /logo.png precached even when icon-192 failed; keys=${[...shellStore.keys()]}`);
  assert(shellStore.has('/webapp'), 'Expected /webapp precached');

  // Now go offline and request /logo.png -> SW must intercept and serve cached /logo.png!
  offline = true;
  let logoPromise = null;
  handlers.fetch({
    request: { method: 'GET', url: 'https://app.mutah.example/logo.png' },
    respondWith(p) { logoPromise = p; },
  });
  assert(logoPromise, 'sw.js must intercept GET /logo.png');
  const logoRes = await logoPromise;
  assert(logoRes && logoRes.ok && logoRes.body === 'asset:/logo.png', `Expected cached /logo.png, got ${JSON.stringify(logoRes)}`);

  console.log('H14_SW_LOGO_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H14 sw.js logo/precache failed:\n{res.stdout}\n{res.stderr}"
    assert "H14_SW_LOGO_OK" in res.stdout


@requires_node
def test_h14_webapp_viewport_defer_aria_escape_and_focus():
    html = WEBAPP_HTML.read_text(encoding="utf-8")

    # 1. Viewport must NOT disable zooming
    vp = re.search(r'<meta\s+name=["\']viewport["\'][^>]*>', html, flags=re.IGNORECASE)
    assert vp, "viewport meta tag missing"
    assert "user-scalable=no" not in vp.group(0).lower(), f"user-scalable=no still present: {vp.group(0)}"
    assert "maximum-scale=1" not in vp.group(0).lower(), f"maximum-scale=1 still present: {vp.group(0)}"

    # 2. telegram-web-app.js must have defer (not render-blocking)
    tg_script = re.search(r'<script[^>]*telegram-web-app\.js[^>]*>', html, flags=re.IGNORECASE)
    assert tg_script, "telegram-web-app.js script tag missing"
    assert re.search(r"\bdefer\b", tg_script.group(0), flags=re.IGNORECASE), f"telegram-web-app.js must use defer: {tg_script.group(0)}"

    # 3. ARIA attributes
    assert 'role="dialog"' in html, 'Modal must have role="dialog"'
    assert 'aria-modal="true"' in html, 'Modal must have aria-modal="true"'
    assert 'aria-live="polite"' in html, 'Toast / offline status must have aria-live="polite"'
    assert 'aria-label="مركز الإشعارات"' in html, "Notifications icon button must have aria-label"
    assert 'aria-label="مزامنة الآن من LMS"' in html, "Sync icon button must have aria-label"

    # 4. Runtime Escape closes modal and restores focus
    js = P1_NODE_HARNESS + r"""
const assert = require('assert');
const { ctx, document, elements, listeners } = loadWebappP1();

const triggerBtn = document.createElement('button');
triggerBtn.id = 'triggerBtn';
triggerBtn.focus();
assert.strictEqual(document.activeElement, triggerBtn);

ctx.openMoreMenuModal();
const modalBack = elements.get('modalBack');
const modal = elements.get('modal');
assert.strictEqual(modalBack.style.display, 'flex', 'Modal should be open');
assert.strictEqual(modalBack.getAttribute('aria-hidden'), 'false', 'aria-hidden should be false when open');
assert.strictEqual(modal.focused, true, 'Modal container should receive focus on open');

// Press Escape -> modal closes and focus returns to triggerBtn
let prevented = false;
for (const fn of listeners.keydown) {
  fn({ key: 'Escape', target: modal, preventDefault() { prevented = true; } });
}
assert.strictEqual(modalBack.style.display, 'none', 'Escape key must close open modal');
assert.strictEqual(modalBack.getAttribute('aria-hidden'), 'true', 'aria-hidden should be true when closed');
assert.strictEqual(document.activeElement, triggerBtn, 'Focus must be restored to previously focused element on modal close');
assert.strictEqual(prevented, true, 'Escape handler should preventDefault when closing modal');

console.log('H14_MODAL_ARIA_ESC_OK');
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H14 modal/ARIA/Escape failed:\n{res.stdout}\n{res.stderr}"
    assert "H14_MODAL_ARIA_ESC_OK" in res.stdout


# ── H16: No mutahAppToken, no window.fetch patch, explicit apiFetch, 5..1440 ─


@requires_node
def test_h16_no_mutah_app_token_no_fetch_monkeypatch_and_apifetch_contract():
    html = WEBAPP_HTML.read_text(encoding="utf-8")
    assert "mutahAppToken" not in html, "mutahAppToken must be completely removed from webapp.html"
    assert "window.fetch =" not in html and "window.fetch=" not in html, "window.fetch must NOT be monkey-patched"
    assert not re.search(r"(?<![A-Za-z0-9_])fetch\s*\(\s*['\"]/api/", html), "All /api/* calls must use apiFetch(), not bare fetch()"

    js = P1_NODE_HARNESS + r"""
const assert = require('assert');
(async () => {
  const tokenVal = 'user=%7B%22id%22%3A1%7D&auth_date=1700000000&hash=abc999';
  const { ctx, windowObj, baseFetch, fetchCalls, storage } = loadWebappP1({
    initData: tokenVal,
    cookie: 'mutah_csrf=csrf_secret_42; other=1',
  });

  // 1. window.fetch must NOT be monkey-patched
  assert.strictEqual(windowObj.fetch, baseFetch, 'window.fetch was monkey-patched!');
  assert.strictEqual(typeof ctx.apiFetch, 'function', 'apiFetch must be defined');

  fetchCalls.length = 0;

  // 2. GET /api/user-data sends credentials: 'same-origin' + X-Telegram-Init-Data (no Bearer, no CSRF needed on GET)
  await ctx.apiFetch('/api/user-data');
  const getCall = fetchCalls[fetchCalls.length - 1];
  assert.strictEqual(getCall.credentials, 'same-origin');
  assert.strictEqual(getCall.headers['x-telegram-init-data'], tokenVal);
  assert.strictEqual(getCall.headers['authorization'], undefined);

  // 3. POST /api/update-settings sends credentials: 'same-origin' + X-Telegram-Init-Data + X-CSRF-Token
  await ctx.apiFetch('/api/update-settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ check_interval: 15 }),
  });
  const postCall = fetchCalls[fetchCalls.length - 1];
  assert.strictEqual(postCall.credentials, 'same-origin');
  assert.strictEqual(postCall.headers['x-telegram-init-data'], tokenVal);
  assert.strictEqual(postCall.headers['x-csrf-token'], 'csrf_secret_42');
  assert.strictEqual(postCall.headers['authorization'], undefined);

  // 4. pairWithCode succeeds with {success: true} (no token field) and stores NOTHING in localStorage
  await ctx.pairWithCode('12345678');
  assert.strictEqual(storage.has('mutahAppToken'), false, 'pairWithCode must NOT store mutahAppToken in localStorage');

  console.log('H16_APIFETCH_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H16 apiFetch test failed:\n{res.stdout}\n{res.stderr}"
    assert "H16_APIFETCH_OK" in res.stdout


@requires_node
def test_h16_check_interval_bounds_5_to_1440():
    js = P1_NODE_HARNESS + r"""
const assert = require('assert');
(async () => {
  const { ctx, evalInCtx, fetchCalls } = loadWebappP1();
  await ctx.fetchUserData();

  // Check settingsPage options are all within [5, 1440] and include 5 and 1440
  const html = ctx.settingsPage();
  const selectMatch = html.match(/<select[^>]*data-change="set-server-interval"[^>]*>([\s\S]*?)<\/select>/);
  assert(selectMatch, 'Expected check_interval <select> in settingsPage');
  const vals = [...selectMatch[1].matchAll(/<option\s+value="(\d+)"/g)].map(m => Number(m[1]));
  assert(vals.length >= 4, `Expected interval options, got ${vals}`);
  for (const v of vals) {
    assert(v >= 5 && v <= 1440, `Interval option out of 5..1440 bounds: ${v}`);
  }
  assert(vals.includes(5), 'Expected 5 min option');
  assert(vals.includes(1440), 'Expected 1440 min option');

  // Reject out-of-bounds values without sending API request
  fetchCalls.length = 0;
  for (const bad of [0, 1, 2, 4, -10, 1441, 9999, 'abc', 7.5]) {
    await ctx.setServerInterval(bad);
  }
  assert.strictEqual(
    fetchCalls.filter(c => c.url === '/api/update-settings').length,
    0,
    `Out-of-bounds check_interval must NOT call /api/update-settings; calls=${JSON.stringify(fetchCalls)}`
  );
  assert.strictEqual(evalInCtx('userProfile.check_interval'), 10);

  // Accept boundary values 5 and 1440
  await ctx.setServerInterval(5);
  assert.strictEqual(evalInCtx('userProfile.check_interval'), 5);
  await ctx.setServerInterval(1440);
  assert.strictEqual(evalInCtx('userProfile.check_interval'), 1440);
  assert.strictEqual(fetchCalls.filter(c => c.url === '/api/update-settings').length, 2);

  console.log('H16_INTERVAL_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"H16 check_interval test failed:\n{res.stdout}\n{res.stderr}"
    assert "H16_INTERVAL_OK" in res.stdout

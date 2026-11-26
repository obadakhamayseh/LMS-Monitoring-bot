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
STATIC_JS_DIR = REPO_ROOT / "static" / "js"
README_MD = REPO_ROOT / "README.md"

JS_MODULES = [
    STATIC_JS_DIR / "state.js",
    STATIC_JS_DIR / "dates.js",
    STATIC_JS_DIR / "api.js",
    STATIC_JS_DIR / "render.js",
    STATIC_JS_DIR / "actions.js",
    SW_JS,
]

NODE_BIN = shutil.which("node")
requires_node = pytest.mark.skipif(NODE_BIN is None, reason="node binary not found")


def _run_node(js_code: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE_BIN, "-e", js_code],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25,
    )


FEATURES_NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

function loadWebappFeatures(opts = {}) {
  const html = fs.readFileSync('webapp.html', 'utf8');
  const scriptPaths = [...html.matchAll(/<script[^>]*\bsrc=["'](\/static\/js\/[^"']+)["'][^>]*>/gi)]
    .map(m => m[1].replace(/^\//, ''));
  const parts = scriptPaths.map(p => fs.readFileSync(p, 'utf8'));
  if (!parts.length) throw new Error('No local scripts found in webapp.html');
  const scriptCode = parts.join('\n\n');

  const listeners = { click: [], change: [], input: [], keydown: [], error: [] };
  const elements = new Map();
  const fetchCalls = [];
  const toastMessages = [];
  const clipboardWrites = [];
  const pushSubscribeCalls = [];
  let pushUnsubscribed = false;
  let activeSubscription = opts.initialSubscription || null;

  function makeEl(id = '') {
    const attrs = {};
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
      setAttribute(k, v) { attrs[k] = String(v); },
      getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
      focus() {},
      select() {},
      querySelector() { return null; },
      click() {},
      closest() { return null; },
    };
  }

  const document = {
    cookie: opts.cookie !== undefined ? opts.cookie : 'mutah_csrf=csrf_feat_token_99',
    activeElement: null,
    readyState: 'complete',
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeEl(id));
      return elements.get(id);
    },
    createElement(tag) { return makeEl(tag); },
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
    : 'user=%7B%22id%22%3A1%7D&auth_date=1700000000&hash=featdeadbeef';

  const tgWebApp = opts.noTelegram ? null : {
    initData: tgInitData,
    initDataUnsafe: { user: { id: 1 } },
    ready() {},
    expand() {},
    setHeaderColor() {},
    setBackgroundColor() {},
    openLink() {},
  };

  const defaultFetch = async (url) => {
    if (url.startsWith('/api/user-data')) {
      return {
        ok: true,
        status: 200,
        clone() { return this; },
        async json() {
          return {
            success: true,
            not_registered: false,
            user: {
              full_name: 'طالب اختبار',
              lms_username: '120240001',
              is_monitoring: true,
              check_interval: 10,
              notify_assign: true,
              use_moodle_api: false,
              enable_ical_feed: true,
              digest_enabled: true,
              digest_time: '08:00',
              notify_absence_risk: true,
              max_absence_pct: 15,
              enable_web_push: true,
            },
            features: {
              moodle_api_global: false,
              ical_global: true,
              digest_global: true,
              absence_risk_global: true,
              web_push_global: Boolean(opts.webPushServerEnabled),
            },
            courses: opts.courses || [],
            deadlines: [],
            grades: [],
            attendance: opts.attendance || [],
            absence_risks: opts.absence_risks || [],
            announcements: [],
            files: [],
          };
        },
      };
    }
    if (url === '/api/calendar-token') {
      return {
        ok: true,
        status: 200,
        clone() { return this; },
        async json() { return { success: true, enabled: true, has_token: false, feed_url: '' }; },
      };
    }
    if (url === '/api/calendar-token/rotate') {
      return {
        ok: true,
        status: 200,
        clone() { return this; },
        async json() { return { success: true, token: 'tok_rot_123', feed_url: '/api/calendar.ics?token=tok_rot_123' }; },
      };
    }
    if (url === '/api/calendar-token/revoke') {
      return {
        ok: true,
        status: 200,
        clone() { return this; },
        async json() { return { success: true }; },
      };
    }
    if (url === '/api/push/vapid-public-key') {
      return {
        ok: true,
        status: 200,
        clone() { return this; },
        async json() {
          return opts.webPushServerEnabled
            ? { success: true, enabled: true, public_key: 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U' }
            : { success: true, enabled: false, public_key: '' };
        },
      };
    }
    if (url === '/api/push/subscribe' || url === '/api/push/unsubscribe' || url === '/api/update-settings') {
      return {
        ok: true,
        status: 200,
        clone() { return this; },
        async json() { return { success: true }; },
      };
    }
    return { ok: true, status: 200, clone() { return this; }, async json() { return { success: true }; } };
  };

  const fetchImpl = opts.fetchHandler || defaultFetch;

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
    return fetchImpl(url, init, defaultFetch);
  };

  const fakeRegistration = {
    pushManager: {
      async getSubscription() { return activeSubscription; },
      async subscribe(subOpts) {
        pushSubscribeCalls.push(subOpts);
        activeSubscription = {
          endpoint: 'https://fcm.googleapis.com/fcm/send/test-sub-endpoint-1',
          toJSON() {
            return {
              endpoint: 'https://fcm.googleapis.com/fcm/send/test-sub-endpoint-1',
              keys: { p256dh: 'p256dh_key_abc', auth: 'auth_secret_xyz' },
            };
          },
          async unsubscribe() {
            pushUnsubscribed = true;
            activeSubscription = null;
            return true;
          },
        };
        return activeSubscription;
      },
    },
  };

  const navigatorObj = {
    userAgent: 'NodeFeatTest',
    onLine: true,
    clipboard: {
      async writeText(txt) { clipboardWrites.push(txt); },
    },
  };
  if (!opts.noPushSupport) {
    navigatorObj.serviceWorker = {
      register: async () => fakeRegistration,
      ready: Promise.resolve(fakeRegistration),
    };
  }

  const windowObj = {
    Telegram: tgWebApp ? { WebApp: tgWebApp } : undefined,
    location: {
      origin: 'https://app.mutah.example',
      search: '',
      pathname: '/webapp',
      protocol: 'https:',
      reload() {},
    },
    navigator: navigatorObj,
    matchMedia: () => ({ matches: false }),
    fetch: baseFetch,
    open() {},
    addEventListener() {},
    scrollTo() {},
    toastTimer: null,
  };
  if (!opts.noPushSupport) {
    windowObj.PushManager = function PushManager() {};
  }

  const ctx = {
    window: windowObj,
    self: windowObj,
    document,
    localStorage,
    location: windowObj.location,
    navigator: navigatorObj,
    history: { replaceState() {} },
    fetch: (...args) => windowObj.fetch(...args),
    Headers,
    URL,
    URLSearchParams,
    Blob,
    Uint8Array,
    atob: globalThis.atob,
    Notification: opts.noPushSupport ? undefined : {
      permission: 'granted',
      async requestPermission() { return opts.notificationPermission || 'granted'; },
    },
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

  function dispatchClick(dataset) {
    const el = {
      dataset,
      closest(selector) {
        if (selector === '[data-action]' && dataset && dataset.action) return el;
        return null;
      },
    };
    for (const fn of listeners.click) fn({ target: el, preventDefault() {} });
  }

  function dispatchChange(dataset, props = {}) {
    const el = Object.assign({
      dataset,
      value: '',
      checked: false,
      closest(selector) {
        if (selector === '[data-change]' && dataset && dataset.change) return el;
        return null;
      },
    }, props);
    for (const fn of listeners.change) fn({ target: el, preventDefault() {} });
  }

  return {
    ctx,
    evalInCtx: (expr) => vm.runInContext(expr, ctx),
    elements,
    listeners,
    fetchCalls,
    toastMessages,
    clipboardWrites,
    pushSubscribeCalls,
    isPushUnsubscribed: () => pushUnsubscribed,
    dispatchClick,
    dispatchChange,
  };
}
"""


# ── 1. Syntax check across all 6 JS files ───────────────────────────────────


@requires_node
def test_all_six_js_files_pass_node_check():
    assert len(JS_MODULES) == 6
    for js_file in JS_MODULES:
        assert js_file.is_file(), f"Missing JS file: {js_file}"
        res = subprocess.run(
            [NODE_BIN, "--check", str(js_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        assert res.returncode == 0, f"node --check failed on {js_file.name}:\n{res.stderr}"


# ── 2. F7 Service Worker v5: push + notificationclick URL safety ────────────


@requires_node
def test_sw_v5_push_and_notificationclick_handlers_and_xss_url_blocking():
    sw_text = SW_JS.read_text(encoding="utf-8")
    assert "const VERSION = 'v5';" in sw_text, "sw.js VERSION must be bumped to 'v5'"
    assert "Promise.allSettled" in sw_text
    assert "/logo.png" in sw_text
    assert "/static/" in sw_text

    js = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

(async () => {
  const swCode = fs.readFileSync('sw.js', 'utf8');
  const handlers = {};
  const notificationsShown = [];
  const openedWindows = [];
  const focusedClients = [];
  const navigatedClients = [];
  let windowClients = [];

  const ctx = {
    self: {
      location: { origin: 'https://app.mutah.example' },
      addEventListener(type, fn) { handlers[type] = fn; },
      skipWaiting() {},
      registration: {
        async showNotification(title, options) {
          notificationsShown.push({ title, options });
        },
      },
      clients: {
        claim() {},
        async matchAll() { return windowClients; },
        async openWindow(u) { openedWindows.push(u); return { url: u }; },
      },
    },
    URL,
    caches: {
      async open() { return { async put() {}, async match() {}, async delete() {} }; },
      async keys() { return []; },
      async delete() {},
    },
    async fetch() {
      return { ok: true, status: 200, clone() { return { ok: true, status: 200 }; } };
    },
  };

  vm.createContext(ctx);
  vm.runInContext(swCode, ctx);

  assert.strictEqual(typeof handlers.push, 'function', 'sw.js must register push handler');
  assert.strictEqual(typeof handlers.notificationclick, 'function', 'sw.js must register notificationclick handler');

  // 1. Valid JSON push payload
  let p1 = null;
  handlers.push({
    data: {
      json() {
        return {
          title: 'واجب جديد',
          body: 'تم نشر واجب في مادة الخوارزميات',
          url: '/webapp?go=tasks',
          tag: 'assign-1',
        };
      },
      text() { return ''; },
    },
    waitUntil(p) { p1 = p; },
  });
  await p1;
  assert.strictEqual(notificationsShown.length, 1);
  assert.strictEqual(notificationsShown[0].title, 'واجب جديد');
  assert.strictEqual(notificationsShown[0].options.body, 'تم نشر واجب في مادة الخوارزميات');
  assert.strictEqual(notificationsShown[0].options.icon, '/logo.png');
  assert.strictEqual(notificationsShown[0].options.badge, '/logo.png');
  assert.strictEqual(notificationsShown[0].options.data.url, '/webapp?go=tasks');

  // 2. Non-JSON text fallback in push payload
  let p2 = null;
  handlers.push({
    data: {
      json() { throw new SyntaxError('Unexpected token'); },
      text() { return 'إشعار نصي مباشر'; },
    },
    waitUntil(p) { p2 = p; },
  });
  await p2;
  assert.strictEqual(notificationsShown.length, 2);
  assert.strictEqual(notificationsShown[1].options.body, 'إشعار نصي مباشر');
  assert.strictEqual(notificationsShown[1].options.icon, '/logo.png');
  assert.strictEqual(notificationsShown[1].options.badge, '/logo.png');
  assert.strictEqual(notificationsShown[1].options.data.url, '/webapp');

  // 3. Malicious javascript: URL in push payload is sanitized to /webapp
  let p3 = null;
  handlers.push({
    data: {
      json() {
        return { title: 'Alert', body: 'Click', url: 'javascript:alert(document.cookie)' };
      },
    },
    waitUntil(p) { p3 = p; },
  });
  await p3;
  assert.strictEqual(notificationsShown[2].options.data.url, '/webapp', 'Unsafe javascript: URL must be neutralized in push');

  // 4. notificationclick blocks javascript:, data:, //evil.com, http:// and opens safe /webapp instead
  for (const unsafeUrl of [
    'javascript:alert(1)',
    'JAVASCRIPT:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    '//evil.com/phish',
    'http://insecure.example/x',
  ]) {
    let closed = false;
    let clickP = null;
    openedWindows.length = 0;
    windowClients = [];
    handlers.notificationclick({
      notification: {
        data: { url: unsafeUrl },
        close() { closed = true; },
      },
      waitUntil(p) { clickP = p; },
    });
    await clickP;
    assert.strictEqual(closed, true, `notification.close() must be called for ${unsafeUrl}`);
    assert.strictEqual(openedWindows.length, 1);
    assert.strictEqual(
      openedWindows[0],
      'https://app.mutah.example/webapp',
      `Unsafe URL ${unsafeUrl} must fall back to safe /webapp, got ${openedWindows[0]}`
    );
  }

  // 5. notificationclick focuses existing same-origin client for safe relative path
  openedWindows.length = 0;
  focusedClients.length = 0;
  navigatedClients.length = 0;
  windowClients = [{
    url: 'https://app.mutah.example/webapp',
    async focus() { focusedClients.push(this.url); },
    async navigate(u) { navigatedClients.push(u); this.url = u; },
  }];
  let clickSameOriginP = null;
  handlers.notificationclick({
    notification: {
      data: { url: '/webapp?go=attendance' },
      close() {},
    },
    waitUntil(p) { clickSameOriginP = p; },
  });
  await clickSameOriginP;
  assert.strictEqual(navigatedClients[0], 'https://app.mutah.example/webapp?go=attendance');
  assert.strictEqual(focusedClients.length, 1);
  assert.strictEqual(openedWindows.length, 0, 'Should focus existing client instead of opening duplicate tab');

  // 6. notificationclick opens safe external https:// URL in a window
  openedWindows.length = 0;
  let clickHttpsP = null;
  handlers.notificationclick({
    notification: {
      data: { url: 'https://mulms.mutah.edu.jo/mod/assign/view.php?id=77' },
      close() {},
    },
    waitUntil(p) { clickHttpsP = p; },
  });
  await clickHttpsP;
  assert.strictEqual(openedWindows.length, 1);
  assert.strictEqual(openedWindows[0], 'https://mulms.mutah.edu.jo/mod/assign/view.php?id=77');

  console.log('SW_V5_PUSH_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"sw.js v5 push/notificationclick test failed:\n{res.stdout}\n{res.stderr}"
    assert "SW_V5_PUSH_OK" in res.stdout


# ── 3. Settings UI rendering (F1/F4/F5/F6/F7) + zero inline on*= ────────────


@requires_node
def test_settings_renders_all_feature_controls_with_zero_inline_handlers():
    js = FEATURES_NODE_HARNESS + r"""
const assert = require('assert');

(async () => {
  // Case A: VAPID enabled on server + PushManager supported
  const envOn = loadWebappFeatures({ webPushServerEnabled: true });
  await envOn.ctx.fetchUserData();
  const htmlOn = envOn.ctx.settingsPage();

  // Zero inline on*= anywhere in rendered settings HTML
  const tags = htmlOn.match(/<[^>]+>/g) || [];
  assert(tags.length > 10, 'Expected substantial settings HTML');
  for (const tag of tags) {
    const stripped = tag.replace(/"[^"]*"/g, '""').replace(/'[^']*'/g, "''");
    assert(!/\s+on[a-z]+\s*=/i.test(stripped), `Found inline on*= attribute in settingsPage: ${tag}`);
  }

  // F1: Moodle API opt-in toggle (OFF by default) with fallback note
  assert(htmlOn.includes('data-key="use_moodle_api"'), 'Missing F1 use_moodle_api toggle');
  assert(htmlOn.includes('HTML Scraping'), 'Missing F1 fallback note explaining HTML scraping');
  assert.strictEqual(envOn.evalInCtx('userProfile.use_moodle_api'), false, 'use_moodle_api must default to false');

  // F4: Live iCal Feed controls
  assert(htmlOn.includes('data-key="enable_ical_feed"'), 'Missing F4 enable_ical_feed toggle');
  assert(htmlOn.includes('data-action="copy-ical-url"'), 'Missing F4 copy-ical-url button');
  assert(htmlOn.includes('data-action="rotate-ical-token"'), 'Missing F4 rotate-ical-token button');
  assert(htmlOn.includes('data-action="revoke-ical-token"'), 'Missing F4 revoke-ical-token button');

  // F5: Daily Digest toggle + time picker
  assert(htmlOn.includes('data-key="digest_enabled"'), 'Missing F5 digest_enabled toggle');
  assert(htmlOn.includes('data-change="set-digest-time"'), 'Missing F5 digest_time picker');

  // F6: Absence Risk toggle + max_absence_pct selector
  assert(htmlOn.includes('data-key="notify_absence_risk"'), 'Missing F6 notify_absence_risk toggle');
  assert(htmlOn.includes('data-change="set-max-absence-pct"'), 'Missing F6 max_absence_pct selector');

  // F7: Active Web Push subscribe button & preference toggle when VAPID + browser supported
  assert(htmlOn.includes('data-key="enable_web_push"'), 'Missing F7 enable_web_push toggle');
  assert(htmlOn.includes('data-action="subscribe-web-push"'), 'Missing F7 subscribe-web-push button');

  // Case B: VAPID disabled on server -> Web Push button gracefully disabled (no active subscribe action)
  const envServerOff = loadWebappFeatures({ webPushServerEnabled: false });
  await envServerOff.ctx.fetchUserData();
  const htmlServerOff = envServerOff.ctx.settingsPage();
  assert(!htmlServerOff.includes('data-action="subscribe-web-push"'), 'Subscribe button must not be active when VAPID is disabled on server');
  assert(htmlServerOff.includes('disabled'), 'Expected graceful disabled button when VAPID is off');

  // Case C: Browser without PushManager -> gracefully disabled
  const envNoPush = loadWebappFeatures({ webPushServerEnabled: true, noPushSupport: true });
  await envNoPush.ctx.fetchUserData();
  const htmlNoPush = envNoPush.ctx.settingsPage();
  assert(!htmlNoPush.includes('data-action="subscribe-web-push"'), 'Subscribe button must not be active when browser lacks PushManager');

  console.log('SETTINGS_RENDER_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"Settings render test failed:\n{res.stdout}\n{res.stderr}"
    assert "SETTINGS_RENDER_OK" in res.stdout


# ── 4. Delegated actions: iCal, Digest, AbsenceRisk, MoodleAPI, WebPush ─────


@requires_node
def test_delegated_feature_actions_and_rollback():
    js = FEATURES_NODE_HARNESS + r"""
const assert = require('assert');

(async () => {
  let failNextUpdate = false;
  const env = loadWebappFeatures({
    webPushServerEnabled: true,
    fetchHandler: async (url, init, defaultFetch) => {
      if (url === '/api/update-settings' && failNextUpdate) {
        return { ok: false, status: 500, clone() { return this; }, async json() { return { success: false, error: 'DB fail' }; } };
      }
      return defaultFetch(url, init);
    },
  });

  await env.ctx.fetchUserData();
  env.fetchCalls.length = 0;

  // 1. Delegated F4 rotate -> copy -> revoke
  env.dispatchClick({ action: 'rotate-ical-token' });
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const rotCall = env.fetchCalls.find(c => c.url === '/api/calendar-token/rotate');
  assert(rotCall, 'Expected POST /api/calendar-token/rotate');
  assert.strictEqual(rotCall.method, 'POST');
  assert.strictEqual(rotCall.credentials, 'same-origin');
  assert.strictEqual(rotCall.headers['x-csrf-token'], 'csrf_feat_token_99');
  assert(rotCall.headers['x-telegram-init-data'], 'Missing X-Telegram-Init-Data on rotate');
  assert.strictEqual(env.evalInCtx('icalState.feed_url'), '/api/calendar.ics?token=tok_rot_123');

  env.dispatchClick({ action: 'copy-ical-url' });
  await new Promise(r => setImmediate(r));
  assert.strictEqual(
    env.clipboardWrites[0],
    'https://app.mutah.example/api/calendar.ics?token=tok_rot_123',
    `Expected full copied iCal URL, got ${JSON.stringify(env.clipboardWrites)}`
  );

  env.dispatchClick({ action: 'revoke-ical-token' });
  await new Promise(r => setImmediate(r));
  const revCall = env.fetchCalls.find(c => c.url === '/api/calendar-token/revoke');
  assert(revCall, 'Expected POST /api/calendar-token/revoke');
  assert.strictEqual(revCall.headers['x-csrf-token'], 'csrf_feat_token_99');
  assert.strictEqual(env.evalInCtx('icalState.has_token'), false);

  // 2. Delegated F5 digest_time + F6 max_absence_pct + F1 use_moodle_api
  env.dispatchChange({ change: 'set-digest-time' }, { value: '07:30' });
  await new Promise(r => setImmediate(r));
  assert.strictEqual(env.evalInCtx('userProfile.digest_time'), '07:30');

  env.dispatchChange({ change: 'set-max-absence-pct' }, { value: '20' });
  await new Promise(r => setImmediate(r));
  assert.strictEqual(env.evalInCtx('userProfile.max_absence_pct'), 20);

  env.dispatchChange({ change: 'set-server-pref', key: 'use_moodle_api' }, { checked: true });
  await new Promise(r => setImmediate(r));
  assert.strictEqual(env.evalInCtx('userProfile.use_moodle_api'), true);

  const updateCalls = env.fetchCalls.filter(c => c.url === '/api/update-settings');
  assert.strictEqual(updateCalls.length, 3, `Expected 3 /api/update-settings calls, got ${updateCalls.length}`);
  assert.strictEqual(JSON.parse(updateCalls[0].body).digest_time, '07:30');
  assert.strictEqual(JSON.parse(updateCalls[1].body).max_absence_pct, 20);
  assert.strictEqual(JSON.parse(updateCalls[2].body).use_moodle_api, true);
  for (const uc of updateCalls) {
    assert.strictEqual(uc.headers['x-csrf-token'], 'csrf_feat_token_99');
    assert(uc.headers['x-telegram-init-data'], 'Missing X-Telegram-Init-Data');
  }

  // 3. Invalid digest_time / max_absence_pct rejected client-side without API call
  const beforeInvalidCount = env.fetchCalls.filter(c => c.url === '/api/update-settings').length;
  await env.ctx.setDigestTime('25:99');
  await env.ctx.setDigestTime('bad');
  await env.ctx.setMaxAbsencePct(0);
  await env.ctx.setMaxAbsencePct(80);
  assert.strictEqual(
    env.fetchCalls.filter(c => c.url === '/api/update-settings').length,
    beforeInvalidCount,
    'Invalid digest_time / max_absence_pct must NOT call /api/update-settings'
  );

  // 4. Rollback on HTTP 500 for digest_time & max_absence_pct & use_moodle_api
  failNextUpdate = true;
  await env.ctx.setDigestTime('09:15');
  assert.strictEqual(env.evalInCtx('userProfile.digest_time'), '07:30', 'digest_time must roll back on 500');
  await env.ctx.setMaxAbsencePct(10);
  assert.strictEqual(env.evalInCtx('userProfile.max_absence_pct'), 20, 'max_absence_pct must roll back on 500');
  await env.ctx.setServerPref('use_moodle_api', false);
  assert.strictEqual(env.evalInCtx('userProfile.use_moodle_api'), true, 'use_moodle_api must roll back on 500');
  failNextUpdate = false;

  // 5. Delegated F7 Web Push subscribe + unsubscribe
  env.dispatchClick({ action: 'subscribe-web-push' });
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));

  const vapidCall = env.fetchCalls.find(c => c.url === '/api/push/vapid-public-key');
  const subCall = env.fetchCalls.find(c => c.url === '/api/push/subscribe');
  assert(vapidCall, 'Expected GET /api/push/vapid-public-key');
  assert(subCall, 'Expected POST /api/push/subscribe');
  assert.strictEqual(subCall.method, 'POST');
  assert.strictEqual(subCall.headers['x-csrf-token'], 'csrf_feat_token_99');
  assert(env.pushSubscribeCalls.length === 1, 'Expected pushManager.subscribe to be called once');
  assert.strictEqual(env.pushSubscribeCalls[0].userVisibleOnly, true);
  assert(env.pushSubscribeCalls[0].applicationServerKey instanceof Uint8Array, 'applicationServerKey must be Uint8Array');
  assert.strictEqual(env.pushSubscribeCalls[0].applicationServerKey.length, 65, 'Uncompressed VAPID public key should decode to 65 bytes');
  const subBody = JSON.parse(subCall.body);
  assert.strictEqual(subBody.endpoint, 'https://fcm.googleapis.com/fcm/send/test-sub-endpoint-1');
  assert.strictEqual(subBody.keys.p256dh, 'p256dh_key_abc');
  assert.strictEqual(subBody.keys.auth, 'auth_secret_xyz');
  assert.strictEqual(env.evalInCtx('webPushState.subscribed'), true);

  env.dispatchClick({ action: 'unsubscribe-web-push' });
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const unsubCall = env.fetchCalls.find(c => c.url === '/api/push/unsubscribe');
  assert(unsubCall, 'Expected POST /api/push/unsubscribe');
  assert.strictEqual(JSON.parse(unsubCall.body).endpoint, 'https://fcm.googleapis.com/fcm/send/test-sub-endpoint-1');
  assert.strictEqual(env.isPushUnsubscribed(), true);
  assert.strictEqual(env.evalInCtx('webPushState.subscribed'), false);

  console.log('DELEGATED_FEATURES_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"Delegated feature actions test failed:\n{res.stdout}\n{res.stderr}"
    assert "DELEGATED_FEATURES_OK" in res.stdout


# ── 5. F6 Absence Risk badge rendering & XSS escaping on Attendance page ────


@requires_node
def test_attendance_page_renders_escaped_absence_risk_badges():
    js = FEATURES_NODE_HARNESS + r"""
const assert = require('assert');

(async () => {
  const env = loadWebappFeatures({
    courses: [
      { id: 101, name: 'Algorithms', is_monitored: true, credit_hours: 3 },
      { id: 102, name: 'Operating Systems', is_monitored: true, credit_hours: 3 },
    ],
    attendance: [
      { course_id: 101, present_count: 10, absent_count: 4, late_count: 0, excused_count: 0, taken_sessions: 14, sessions: [] },
      { course_id: 102, present_count: 12, absent_count: 1, late_count: 0, excused_count: 0, taken_sessions: 13, sessions: [] },
    ],
    absence_risks: [
      {
        course_id: 101,
        course_name: 'Algorithms',
        absent_count: 4,
        max_allowed: 6,
        remaining: 2,
        risk_level: 'critical',
        at_risk: true,
        message: '⚠️ بقي لك 2 غيابات في مادة <script>alert(1)</script>',
      },
      {
        course_id: 102,
        course_name: 'Operating Systems',
        absent_count: 1,
        max_allowed: 6,
        remaining: 5,
        risk_level: 'safe',
        at_risk: false,
        message: '',
      },
    ],
  });

  await env.ctx.fetchUserData();
  const attHtml = env.ctx.attendancePage();

  assert(attHtml.includes('بقي 2 غيابات'), `Expected 'بقي 2 غيابات' badge in attendance HTML:\n${attHtml}`);
  assert(attHtml.includes('بقي 5 غيابات'), `Expected 'بقي 5 غيابات' badge in attendance HTML:\n${attHtml}`);
  assert(attHtml.includes('خطر حرمان'), 'Expected critical risk status label');
  assert(attHtml.includes('data-risk-level="critical"'), 'Expected critical risk pill');
  assert(attHtml.includes('data-risk-level="safe"'), 'Expected safe risk pill');
  assert(!/<script\b/i.test(attHtml), 'Hostile <script> in absence_risks.message MUST be escaped via escapeHTML');
  assert(attHtml.includes('&lt;script&gt;alert(1)&lt;/script&gt;'), 'Expected escaped risk message');

  const tags = attHtml.match(/<[^>]+>/g) || [];
  for (const tag of tags) {
    const stripped = tag.replace(/"[^"]*"/g, '""').replace(/'[^']*'/g, "''");
    assert(!/\s+on[a-z]+\s*=/i.test(stripped), `Found inline on*= attribute in attendancePage: ${tag}`);
  }

  console.log('ABSENCE_RISK_UI_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"Absence Risk UI test failed:\n{res.stdout}\n{res.stderr}"
    assert "ABSENCE_RISK_UI_OK" in res.stdout


# ── 6. README.md completeness (F1–F8, toggles, VAPID, iCal, Migration, Render) ─


def test_readme_documents_all_features_toggles_migration_and_render_deploy():
    readme = README_MD.read_text(encoding="utf-8")

    # Zero stale AES-256 claims; Fernet documented accurately
    assert not re.search(r"AES[-\s]?256", readme, flags=re.IGNORECASE), "Stale AES-256 found in README.md"
    for crypto_term in ["Fernet", "AES-128-CBC", "HMAC-SHA256", "enc:v1:"]:
        assert crypto_term in readme, f"README.md missing crypto detail: {crypto_term}"

    # All F1-F8 features documented
    for feat in ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"]:
        assert re.search(rf"\b{feat}\b", readme), f"README.md missing documentation for feature {feat}"

    # Enable/disable toggles & env vars
    for env_var in [
        "ENABLE_MOODLE_API",
        "ENABLE_ICAL_FEED",
        "ENABLE_DAILY_DIGEST",
        "DEFAULT_DIGEST_TIME",
        "ENABLE_ABSENCE_RISK",
        "DEFAULT_MAX_ABSENCE_PCT",
        "ENABLE_WEB_PUSH",
        "VAPID_PUBLIC_KEY",
        "VAPID_PRIVATE_KEY",
        "VAPID_SUBJECT",
        "LOG_FORMAT",
        "ENABLE_STALE_ALERT",
        "STALE_MONITOR_SECONDS",
    ]:
        assert env_var in readme, f"README.md missing feature flag/env var: {env_var}"

    # Live iCal usage, /status, /health, Render Free + UptimeRobot, Migration guide
    for required_topic in [
        "/api/calendar.ics",
        "Google Calendar",
        "Apple Calendar",
        "/status",
        "/health",
        "Render Free",
        "UptimeRobot",
        "ENCRYPTION_KEY",
        "OWNER_TELEGRAM_ID",
        "WEBAPP_URL",
        "TELEGRAM_BOT_TOKEN",
        "psycopg_pool",
        "DIRECT_URL",
        "requirements.lock",
        ".github/workflows/ci.yml",
        "sw.js",
    ]:
        assert required_topic in readme, f"README.md missing required section/topic: {required_topic}"

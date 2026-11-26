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
APP_CSS = REPO_ROOT / "static" / "css" / "app.css"
STATIC_JS_DIR = REPO_ROOT / "static" / "js"

EXPECTED_JS_ORDER = [
    "/static/js/state.js",
    "/static/js/dates.js",
    "/static/js/api.js",
    "/static/js/render.js",
    "/static/js/actions.js",
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
        timeout=20,
    )


def test_webapp_html_has_zero_inline_script_and_style_blocks():
    html = WEBAPP_HTML.read_text(encoding="utf-8")

    inline_scripts = re.findall(
        r"<script(?![^>]*\bsrc=)[^>]*>.*?</script>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert not inline_scripts, f"Expected ZERO inline <script> blocks in webapp.html, found {len(inline_scripts)}"

    inline_styles = re.findall(
        r"<style\b[^>]*>.*?</style>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert not inline_styles, f"Expected ZERO inline <style> blocks in webapp.html, found {len(inline_styles)}"

    # External stylesheet link must be present and file must exist
    assert '<link rel="stylesheet" href="/static/css/app.css">' in html
    assert APP_CSS.is_file(), "static/css/app.css is missing"
    css_text = APP_CSS.read_text(encoding="utf-8")
    assert ":root{" in css_text and ".modal-back{" in css_text

    # All 5 deferred local JS modules must appear in exact DOM order
    script_tags = re.findall(r"<script\b[^>]*>", html, flags=re.IGNORECASE)
    local_tags = [t for t in script_tags if "/static/js/" in t]
    local_srcs = [
        re.search(r'\bsrc=["\']([^"\']+)["\']', t, flags=re.IGNORECASE).group(1)
        for t in local_tags
    ]
    assert local_srcs == EXPECTED_JS_ORDER, f"Unexpected local script order: {local_srcs}"
    for tag in local_tags:
        assert re.search(r"\bdefer\b", tag, flags=re.IGNORECASE), f"Local script must use defer: {tag}"


def test_webapp_html_is_slim_shell_under_200_lines():
    lines = WEBAPP_HTML.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 200, f"webapp.html should be a slim shell (<200 lines), got {len(lines)} lines"


def test_no_inline_event_handlers_in_webapp_and_render_js():
    for path in [WEBAPP_HTML, STATIC_JS_DIR / "render.js"]:
        text = path.read_text(encoding="utf-8")
        # Strip JS comments before scanning for HTML on*= attributes
        handlers = re.findall(r"""<\w[^>]*\s+on[a-z]+\s*=\s*["']""", text, flags=re.IGNORECASE)
        assert not handlers, f"Found inline on*= HTML attribute in {path.name}: {handlers[:5]}"


@requires_node
def test_every_static_js_and_sw_passes_node_check():
    js_files = sorted(STATIC_JS_DIR.glob("*.js"))
    expected_names = {Path(p).name for p in EXPECTED_JS_ORDER}
    actual_names = {p.name for p in js_files}
    assert expected_names.issubset(actual_names), f"Missing static/js modules: {expected_names - actual_names}"

    for target in [*js_files, SW_JS]:
        res = subprocess.run(
            [NODE_BIN, "--check", str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        assert res.returncode == 0, f"node --check failed on {target.relative_to(REPO_ROOT)}:\n{res.stderr}"


@requires_node
def test_sw_v4_precaches_and_serves_static_css_and_js():
    sw_text = SW_JS.read_text(encoding="utf-8")
    ver_match = re.search(r"const VERSION = 'v(\d+)';", sw_text)
    assert ver_match and int(ver_match.group(1)) >= 4, "sw.js VERSION must be >= 'v4'"
    for asset in ["/static/css/app.css", *EXPECTED_JS_ORDER]:
        assert asset in sw_text, f"sw.js must list {asset} in SHELL_URLS"

    js = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

(async () => {
  const swCode = fs.readFileSync('sw.js', 'utf8');
  const handlers = {};
  const cachesByName = new Map();
  let offline = false;
  let forceStatus = 200;

  function getCache(name) {
    if (!cachesByName.has(name)) cachesByName.set(name, new Map());
    const store = cachesByName.get(name);
    return {
      async put(k, res) { store.set(typeof k === 'string' ? k : k.url, res); },
      async match(k) {
        const key = typeof k === 'string' ? k : k.url;
        if (store.has(key)) return store.get(key);
        try {
          return store.get(new URL(key).pathname);
        } catch {
          return undefined;
        }
      },
      async delete(k) { return store.delete(typeof k === 'string' ? k : k.url); },
      async addAll() { throw new Error('Should use Promise.allSettled instead of addAll'); },
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
      if (u.includes('icon-192.png')) throw new Error('Simulated missing icon');
      const ok = forceStatus >= 200 && forceStatus < 300;
      return {
        ok,
        status: forceStatus,
        body: 'body:' + u,
        clone() { return { ok, status: forceStatus, body: 'body:' + u }; },
      };
    },
  };

  vm.createContext(ctx);
  vm.runInContext(swCode, ctx);

  // 1. Install precaches /static/css/app.css + all 5 /static/js/*.js even if one icon fails
  let installP = null;
  handlers.install({ waitUntil(p) { installP = p; } });
  await installP;

  const verMatch = swCode.match(/const VERSION = '(v\d+)';/);
  assert(verMatch, 'Missing VERSION constant in sw.js');
  const expectedShell = 'shell-' + verMatch[1];
  assert(cachesByName.has(expectedShell), `Expected ${expectedShell} cache, got ${[...cachesByName.keys()]}`);
  const shellStore = cachesByName.get(expectedShell);

  const staticAssets = [
    '/static/css/app.css',
    '/static/js/state.js',
    '/static/js/dates.js',
    '/static/js/api.js',
    '/static/js/render.js',
    '/static/js/actions.js',
  ];
  for (const asset of staticAssets) {
    assert(shellStore.has(asset), `Expected ${asset} to be precached in shell-v4`);
  }

  // 2. Offline cache-first serving of every static asset
  offline = true;
  for (const asset of staticAssets) {
    let fetchP = null;
    handlers.fetch({
      request: { method: 'GET', url: 'https://app.mutah.example' + asset },
      respondWith(p) { fetchP = p; },
    });
    assert(fetchP, `sw.js did not intercept GET ${asset}`);
    const res = await fetchP;
    assert(
      res && res.ok && res.body === 'body:' + asset,
      `Unexpected cached response for ${asset}: ${JSON.stringify(res)}`
    );
  }

  // 3. Non-OK response for an uncached /static/ asset must NOT be cached
  offline = false;
  forceStatus = 404;
  let missP = null;
  handlers.fetch({
    request: { method: 'GET', url: 'https://app.mutah.example/static/js/missing.js' },
    respondWith(p) { missP = p; },
  });
  assert(missP, 'sw.js should intercept /static/js/missing.js');
  const missRes = await missP;
  assert.strictEqual(missRes.status, 404);
  assert(!shellStore.has('/static/js/missing.js'), '404 static response must NOT be cached');

  console.log('SW_V4_STATIC_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    res = _run_node(js)
    assert res.returncode == 0, f"sw.js v4 static asset test failed:\n{res.stdout}\n{res.stderr}"
    assert "SW_V4_STATIC_OK" in res.stdout

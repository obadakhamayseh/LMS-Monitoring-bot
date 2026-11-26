/* Application constants, client state, safe localStorage JSON helpers, session cleanup & persistence */

const STORAGE_KEY = 'mutahStudentNotebook';
const CHECK_INTERVAL_MIN = 5;
const CHECK_INTERVAL_MAX = 1440;
const MAX_ABSENCE_PCT_MIN = 1;
const MAX_ABSENCE_PCT_MAX = 50;

const navItems = [
  ['home', '⌂', 'الرئيسية'],
  ['week', '▦', 'الأسبوع'],
  ['courses', '▤', 'المقررات'],
  ['tasks', '☷', 'المهام'],
  ['grades', '▥', 'العلامات'],
  ['attendance', '✓', 'الحضور'],
  ['announcements', '♧', 'الإعلانات'],
  ['files', '▧', 'الملفات'],
  ['notifications', '◉', 'الإشعارات'],
  ['settings', '⚙', 'الإعدادات'],
  ['help', '?', 'المساعدة']
];

const _ALLOWED_PREF_KEYS = new Set([
  'notify_assign', 'notify_quiz', 'notify_resource',
  'notify_grade', 'notify_announcement', 'notify_attendance',
  'use_moodle_api', 'enable_ical_feed', 'digest_enabled',
  'notify_absence_risk', 'enable_web_push'
]);

function _defaultUserProfile() {
  return {
    full_name: 'طالب جامعة مؤتة',
    lms_username: '',
    is_monitoring: true,
    check_interval: 10,
    last_check: 'لم يتم بعد',
    notify_assign: true,
    notify_quiz: true,
    notify_resource: true,
    notify_grade: true,
    notify_announcement: true,
    notify_attendance: true,
    use_moodle_api: false,
    enable_ical_feed: true,
    digest_enabled: true,
    digest_time: '08:00',
    notify_absence_risk: true,
    max_absence_pct: 15,
    enable_web_push: true
  };
}

function _defaultFeatureFlags() {
  return {
    moodle_api_global: false,
    ical_global: true,
    digest_global: true,
    absence_risk_global: true,
    web_push_global: false
  };
}

function safeReadLocalJson(key, fallback = {}) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw);
    return (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) ? parsed : fallback;
  } catch (err) {
    console.warn(`Corrupted localStorage JSON for ${key}, resetting:`, err);
    try { localStorage.removeItem(key); } catch (_) {}
    return fallback;
  }
}

// Runtime & navigation state
let isDeviceLinked = false;
let deferredInstall = null;
let active = 'home';
let selectedCourse = null;
let weekOffset = 0;
let isSyncing = false;
let isLoggedIn = false;
let userProfile = _defaultUserProfile();

let courses = [];
let tasks = [];
let grades = [];
let attendanceList = [];
let announcements = [];
let files = [];
let absenceRisks = [];
let featureFlags = _defaultFeatureFlags();
let icalState = { loaded: false, enabled: true, has_token: false, feed_url: '' };
let webPushState = { supported: false, serverEnabled: false, subscribed: false, busy: false, publicKey: '' };

// Local + server synced personal workspace state
const savedLocal = safeReadLocalJson(STORAGE_KEY, {});
let doneSet = new Set(Array.isArray(savedLocal.done) ? savedLocal.done : []);
let readSet = new Set(Array.isArray(savedLocal.read) ? savedLocal.read : []);
let notes = (savedLocal.notes && typeof savedLocal.notes === 'object' && !Array.isArray(savedLocal.notes)) ? savedLocal.notes : {};
let reminders = Array.isArray(savedLocal.reminders) ? savedLocal.reminders : [];
let courseMeta = (savedLocal.courseMeta && typeof savedLocal.courseMeta === 'object' && !Array.isArray(savedLocal.courseMeta)) ? savedLocal.courseMeta : {};
let limit = Number(savedLocal.limit) || 20;
let syncLogs = [];

// Task / Grade / Announcement / File filter state
let taskFilterState = { state: 'all', course: 'all', type: 'all' };
let gradeFilterState = { course: 'all', filter: 'all' };
let newsFilterState = { filter: 'all', course: 'all' };
let fileFilterState = { course: 'all', q: '' };
let courseSearchQuery = '';
let _lastFocusedEl = null;

function extractTeacher(name) {
  const m = String(name || '').match(/(د\.\s*[^\-\(\)|]+|أ\.\s*[^\-\(\)|]+)/);
  return m ? m[1].trim() : 'هيئة التدريس';
}

function course(id) {
  return courses.find(c => String(c.id) === String(id)) || {
    id: id,
    name: 'مقرر دراسي',
    code: `ID ${id}`,
    teacher: 'هيئة التدريس',
    room: 'LMS',
    time: '',
    attendance: 0,
    absent: 0,
    total: 0,
    grade: null
  };
}

function isTaskDone(t) {
  return Boolean(t.done || doneSet.has(String(t.id)));
}

function unreadCount() {
  return announcements.filter(a => !readSet.has(String(a.id))).length;
}

async function clearClientSession() {
  try { localStorage.removeItem(STORAGE_KEY); } catch (e) { console.warn('Failed to clear localStorage:', e); }
  try {
    const cacheApi = (typeof window !== 'undefined' && window.caches) ? window.caches : (typeof caches !== 'undefined' ? caches : null);
    if (cacheApi && typeof cacheApi.keys === 'function') {
      const keys = await cacheApi.keys();
      await Promise.all(keys.filter(k => String(k).startsWith('data-')).map(k => cacheApi.delete(k)));
    }
  } catch (e) {
    console.warn('Failed to clear data caches:', e);
  }
  try { navigator.serviceWorker?.controller?.postMessage('clear-data'); } catch (e) {}

  isLoggedIn = false;
  isDeviceLinked = false;
  selectedCourse = null;
  userProfile = _defaultUserProfile();
  courses = [];
  tasks = [];
  grades = [];
  attendanceList = [];
  announcements = [];
  files = [];
  absenceRisks = [];
  featureFlags = _defaultFeatureFlags();
  icalState = { loaded: false, enabled: true, has_token: false, feed_url: '' };
  webPushState = { supported: false, serverEnabled: false, subscribed: false, busy: false, publicKey: '' };
  doneSet = new Set();
  readSet = new Set();
  notes = {};
  reminders = [];
  courseMeta = {};
  limit = 20;
  syncLogs = [];
}

function persist(syncServer = true) {
  const payload = {
    done: [...doneSet],
    read: [...readSet],
    notes,
    reminders,
    courseMeta,
    limit
  };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch (err) {
    console.warn('Failed to write localStorage:', err);
  }
  if (syncServer && isLoggedIn) {
    apiFetch('/api/user-meta', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tid: getTelegramId(),
        meta: {
          notes,
          personal_reminders: reminders,
          read_announcements: [...readSet],
          absence_limit: limit,
          course_meta: courseMeta
        }
      })
    }).then((res) => {
      if (!res || !res.ok) console.warn('Failed to sync user-meta: HTTP', res && res.status);
    }).catch((err) => { console.warn('Failed to sync user-meta:', err); });
  }
}

/* User actions, modal ARIA/Escape/focus management, event delegation allowlists, and app boot */

function isModalOpen() {
  const back = document.getElementById('modalBack');
  return Boolean(back && back.style.display === 'flex');
}

function openModal(html) {
  const back = document.getElementById('modalBack');
  const modal = document.getElementById('modal');
  if (!back || !modal) return;
  if (!isModalOpen() && document.activeElement && typeof document.activeElement.focus === 'function') {
    _lastFocusedEl = document.activeElement;
  }
  modal.innerHTML = html;
  back.style.display = 'flex';
  back.setAttribute && back.setAttribute('aria-hidden', 'false');
  const focusTarget = (typeof modal.querySelector === 'function'
    ? modal.querySelector('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
    : null) || modal;
  if (focusTarget && typeof focusTarget.focus === 'function') {
    try { focusTarget.focus(); } catch (_) {}
  }
}

function closeModal() {
  const back = document.getElementById('modalBack');
  if (back) {
    back.style.display = 'none';
    back.setAttribute && back.setAttribute('aria-hidden', 'true');
  }
  const prev = _lastFocusedEl;
  _lastFocusedEl = null;
  if (prev && typeof prev.focus === 'function') {
    try { prev.focus(); } catch (_) {}
  }
}

async function submitPairCode() {
  const input = document.getElementById('pairCode');
  const btn = document.getElementById('pairBtn');
  const err = document.getElementById('pairErr');
  const code = input.value.trim();
  if (!code) { err.textContent = 'أدخل الرمز المرسل من البوت'; return; }
  btn.disabled = true; btn.textContent = 'جارٍ الربط...'; err.textContent = '';
  try {
    await pairWithCode(code);
    input.value = '';
    hidePairing();
    toast('تم ربط الجهاز بنجاح');
    await fetchUserData();
  } catch (e) {
    err.textContent = e.message || 'تعذر ربط الجهاز';
  } finally {
    btn.disabled = false; btn.textContent = 'ربط الجهاز';
  }
}

async function unlinkThisDevice() {
  if (!confirm('فصل هذا الجهاز عن التطبيق؟ ستحتاج رمزاً جديداً من البوت لإعادة الربط. تنبيهات تيليجرام لن تتأثر.')) return;
  try { await apiFetch('/api/app-logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); } catch (e) {}
  await clearClientSession();
  location.reload();
}

async function installApp() {
  if (!deferredInstall) { toast('استخدم قائمة المتصفح ← «إضافة إلى الشاشة الرئيسية»'); return; }
  deferredInstall.prompt();
  try { await deferredInstall.userChoice; } catch (e) {}
  deferredInstall = null;
  render();
}

async function submitLogin() {
  const u = document.getElementById('loginUser')?.value?.trim() || '';
  const pRaw = document.getElementById('loginPass')?.value;
  const p = typeof pRaw === 'string' ? pRaw : '';
  if (!u || p.length === 0) {
    toast('يرجى إدخال الرقم الجامعي وكلمة المرور');
    return;
  }
  const btn = document.getElementById('btnDoLogin');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'جارٍ التحقق والاتصال بجامعة مؤتة...';
  }
  try {
    const res = await apiFetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tid: getTelegramId(), username: u, password: p })
    });
    const d = await res.json().catch(() => ({}));
    if (res.ok && d.success) {
      toast('تم تسجيل الدخول بنجاح! يمكنك الآن إضافة أو استيراد مقرراتك.');
      await fetchUserData();
    } else {
      toast(d.error || 'فشل تسجيل الدخول إلى LMS');
    }
  } catch (e) {
    toast('حدث خطأ في الاتصال بالخادم');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'تسجيل الدخول وربط الحساب';
    }
  }
}

function changeWeek(delta, reset = false) {
  if (reset) weekOffset = 0;
  else weekOffset += delta;
  render();
}

function saveAllSchedules() {
  courses.forEach(c => {
    const tVal = document.getElementById(`sched_time_${c.id}`)?.value?.trim() || '';
    const rVal = document.getElementById(`sched_room_${c.id}`)?.value?.trim() || '';
    if (!courseMeta[c.id]) courseMeta[c.id] = {};
    courseMeta[c.id].time = tVal;
    if (rVal) courseMeta[c.id].room = rVal;
    c.time = tVal;
    if (rVal) c.room = rVal;
  });
  persist(true);
  closeModal();
  render();
  toast('تم حفظ مواعيد المحاضرات في المخطط الأسبوعي');
}

function openCourse(id) {
  selectedCourse = String(id);
  active = 'courseDetail';
  render();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function saveSingleCourseMeta(cid) {
  const c = course(cid);
  const meta = {
    code: document.getElementById('metaCode')?.value?.trim() || `ID ${cid}`,
    teacher: document.getElementById('metaTeacher')?.value?.trim() || 'هيئة التدريس',
    room: document.getElementById('metaRoom')?.value?.trim() || 'LMS',
    time: document.getElementById('metaTime')?.value?.trim() || ''
  };
  courseMeta[cid] = meta;
  Object.assign(c, meta);
  persist(true);
  closeModal();
  render();
  toast('تم حفظ بيانات المقرر');
}

function setLimit(v) {
  limit = Math.min(50, Math.max(5, Number(v) || 20));
  persist(true);
  render();
  toast('تم حفظ حد تنبيه الغياب');
}

function readAnnouncement(id) {
  const sid = String(id);
  if (readSet.has(sid)) readSet.delete(sid);
  else readSet.add(sid);
  persist(true);
  render();
  toast('تم تحديث حالة القراءة');
}

function markAllRead() {
  announcements.forEach(a => readSet.add(String(a.id)));
  persist(true);
  render();
  toast('تم تعليم كافة الإعلانات كمقروءة');
}

function saveCustomReminder(taskId, courseName) {
  const title = document.getElementById('remTitle')?.value?.trim();
  const when = document.getElementById('remWhen')?.value?.trim();
  if (!title || !when) {
    toast('يرجى كتابة عنوان التذكير وموعده');
    return;
  }
  reminders.unshift({
    task_id: taskId || '',
    title,
    course_name: courseName || '',
    when,
    created_at: new Date().toISOString()
  });
  persist(true);
  closeModal();
  render();
  toast('تم حفظ التذكير الشخصي في مركز الإشعارات');
}

function removePersonalReminder(idx) {
  reminders.splice(idx, 1);
  persist(true);
  render();
  toast('تم حذف التذكير الشخصي');
}

async function toggleDone(id, reopenModal = false) {
  const sid = String(id);
  const t = tasks.find(x => String(x.id) === sid);
  const currentlyDone = t ? isTaskDone(t) : doneSet.has(sid);
  const nextDone = !currentlyDone;

  if (nextDone) doneSet.add(sid);
  else doneSet.delete(sid);
  if (t) t.done = nextDone;

  persist(false);
  render();
  if (reopenModal) taskDetail(sid);

  const rawCourse = t ? t.course : '';
  const courseIdVal = (rawCourse !== '' && !isNaN(Number(rawCourse))) ? Number(rawCourse) : rawCourse;
  const itemType = t ? (t.raw_type || (sid.startsWith('quiz_') ? 'quiz' : 'assign')) : (sid.startsWith('quiz_') ? 'quiz' : 'assign');

  try {
    const res = await apiFetch('/api/mark-submitted', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tid: getTelegramId(),
        content_id: sid,
        item_key: sid,
        course_id: courseIdVal,
        title: t ? t.title : '',
        url: t ? t.url : '',
        item_type: itemType,
        is_submitted: nextDone ? 1 : 0
      })
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || d.success === false) {
      throw new Error(d.error || 'تعذر تحديث حالة المهمة');
    }
    toast(nextDone ? 'تم تسجيل الإنجاز وإيقاف تذكيرات المهمة' : 'أعيدت المهمة إلى قائمة قيد الحل');
  } catch (e) {
    // H10 rollback optimistic update on HTTP error or network failure
    if (currentlyDone) doneSet.add(sid);
    else doneSet.delete(sid);
    if (t) t.done = currentlyDone;
    persist(false);
    render();
    if (reopenModal) taskDetail(sid);
    toast('تعذر تحديث حالة المهمة على الخادم');
  }
}

function saveNote(id) {
  const val = document.getElementById('taskNote')?.value || '';
  notes[String(id)] = val;
  persist(true);
  render();
  toast('حُفظت ملاحظتك بنجاح');
}

function _resolveTaskDueDate(t) {
  if (!t) return null;
  if (t.dueDateObj && typeof t.dueDateObj.getTime === 'function') {
    return isNaN(t.dueDateObj.getTime()) ? null : t.dueDateObj;
  }
  if (t.due) {
    const d = parseAnyDate(t.due);
    if (d && !isNaN(d.getTime())) return d;
  }
  if (t.due_str) {
    const d = parseAnyDate(t.due_str);
    if (d && !isNaN(d.getTime())) return d;
  }
  return null;
}

function downloadICS(id) {
  let targets;
  if (Array.isArray(id)) {
    targets = id;
  } else if (id !== undefined && id !== null && id !== '') {
    const found = tasks.find(x => String(x.id) === String(id));
    targets = found ? [found] : [];
  } else {
    targets = tasks;
  }

  const datedItems = [];
  for (const t of targets) {
    const dueDate = _resolveTaskDueDate(t);
    if (dueDate) datedItems.push({ task: t, dueDate });
  }

  if (datedItems.length === 0) {
    toast('لا توجد مهام بموعد استحقاق صالح لتصديرها إلى التقويم');
    return false;
  }

  const nowStamp = formatICSDateUTC(new Date());
  const lines = [
    'BEGIN:VCALENDAR',
    'VERSION:2.0',
    'PRODID:-//Mutah Student Notebook//AR',
    'CALSCALE:GREGORIAN'
  ];

  for (const { task: t, dueDate } of datedItems) {
    const dtStart = formatICSDateUTC(dueDate);
    const dtEnd = formatICSDateUTC(new Date(dueDate.getTime() + 3600000));
    const safeUid = String(t.id ?? '').replace(/[\r\n\s]+/g, '_') || 'task';
    const safeUrl = safeHttpUrl(t.url) || '';
    const summary = escapeICSText(`${t.title || ''} (${t.course_name || ''})`);
    const description = escapeICSText(`${t.course_name || ''} - ${t.type || ''}${safeUrl ? ' - ' + safeUrl : ''}`);
    lines.push(
      'BEGIN:VEVENT',
      `UID:${safeUid}@mutah-lms-monitor`,
      `DTSTAMP:${nowStamp}`,
      `DTSTART:${dtStart}`,
      `DTEND:${dtEnd}`,
      `SUMMARY:${summary}`,
      `DESCRIPTION:${description}`,
      ...(safeUrl ? [`URL:${safeUrl}`] : []),
      'END:VEVENT'
    );
  }

  lines.push('END:VCALENDAR');
  const ics = lines.join('\r\n') + '\r\n';

  const url = URL.createObjectURL(new Blob([ics], { type: 'text/calendar;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  const baseName = datedItems.length === 1
    ? String(datedItems[0].task.id || 'task').replace(/[^a-zA-Z0-9._-]+/g, '_')
    : 'mutah-deadlines';
  a.download = `${baseName}.ics`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  toast('تم تنزيل ملف الموعد لإضافته إلى التقويم');
  return true;
}

// PWA install & connectivity listeners
window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  deferredInstall = e;
  if (active === 'settings') render();
});
window.addEventListener('appinstalled', () => {
  deferredInstall = null;
  toast('تم تثبيت التطبيق على جهازك');
  if (active === 'settings') render();
});
window.addEventListener('online', () => { updateOnlineBar(); if (isLoggedIn) fetchUserData(); });
window.addEventListener('offline', updateOnlineBar);

// C1: Document-level event delegation with explicit null-prototype allowlists (no inline handlers)
const _CLICK_ACTIONS = Object.assign(Object.create(null), {
  'go': (el) => go(el.dataset.page),
  'close-and-go': (el) => { closeModal(); go(el.dataset.page); },
  'close-modal': () => closeModal(),
  'close-modal-backdrop': (el, e) => { if (e.target === el) closeModal(); },
  'submit-pair-code': () => submitPairCode(),
  'sync-now': () => syncNow(),
  'open-more-menu': () => openMoreMenuModal(),
  'submit-login': () => submitLogin(),
  'open-course': (el) => openCourse(el.dataset.id),
  'task-detail': (el) => taskDetail(el.dataset.id),
  'toggle-done': (el) => toggleDone(el.dataset.id, el.dataset.reopen === '1'),
  'open-announcement': (el) => openAnnouncementModal(el.dataset.id),
  'read-announcement': (el) => readAnnouncement(el.dataset.id),
  'open-link': (el) => openExternalLink(el.dataset.url),
  'change-week': (el) => changeWeek(Number(el.dataset.delta || 0), el.dataset.reset === '1'),
  'open-schedule-modal': () => openScheduleModal(),
  'save-all-schedules': () => saveAllSchedules(),
  'open-add-course-modal': () => openAddCourseModal(),
  'submit-add-course': () => submitAddCourse(),
  'sync-all-courses': () => syncAllCoursesFromLms(),
  'close-and-sync-courses': () => { closeModal(); syncAllCoursesFromLms(); },
  'open-edit-course-meta': (el) => openEditCourseMetaModal(el.dataset.id),
  'save-course-meta': (el) => saveSingleCourseMeta(el.dataset.id),
  'toggle-course-monitoring': (el) => toggleCourseMonitoring(el.dataset.id),
  'remove-course': (el) => removeCourseById(el.dataset.id),
  'open-attendance-sessions': (el) => openAttendanceSessionsModal(el.dataset.id),
  'mark-all-read': () => markAllRead(),
  'open-custom-reminder': (el) => openCustomReminderModal(el.dataset.id || ''),
  'save-custom-reminder': (el) => saveCustomReminder(el.dataset.taskId || '', el.dataset.courseName || ''),
  'remove-personal-reminder': (el) => {
    const idx = Number(el.dataset.index);
    if (Number.isInteger(idx) && idx >= 0) removePersonalReminder(idx);
  },
  'show-toast': (el) => toast(el.dataset.msg || ''),
  'logout-account': () => logoutAccount(),
  'install-app': () => installApp(),
  'unlink-device': () => unlinkThisDevice(),
  'download-ics': (el) => downloadICS(el.dataset.id),
  'save-note': (el) => saveNote(el.dataset.id)
});

const _CHANGE_ACTIONS = Object.assign(Object.create(null), {
  'filter-tasks': () => filterTasks(),
  'filter-grades': () => filterGrades(),
  'filter-news': () => filterNews(),
  'filter-files': () => filterFiles(),
  'set-limit': (el) => setLimit(el.value),
  'set-server-pref': (el) => setServerPref(el.dataset.key, el.checked),
  'set-server-interval': (el) => setServerInterval(el.value),
  'set-server-monitoring': (el) => setServerMonitoring(el.checked)
});

const _INPUT_ACTIONS = Object.assign(Object.create(null), {
  'filter-courses': (el) => filterCourses(el.value),
  'filter-files': () => filterFiles()
});

const _KEYDOWN_ACTIONS = Object.assign(Object.create(null), {
  'submit-pair-code': (el, e) => { if (e.key === 'Enter') submitPairCode(); }
});

document.addEventListener('click', (e) => {
  const el = e.target && typeof e.target.closest === 'function' ? e.target.closest('[data-action]') : null;
  if (!el) return;
  const action = el.dataset && el.dataset.action;
  if (action && Object.prototype.hasOwnProperty.call(_CLICK_ACTIONS, action)) {
    _CLICK_ACTIONS[action](el, e);
  }
});

document.addEventListener('change', (e) => {
  const el = e.target && typeof e.target.closest === 'function' ? e.target.closest('[data-change]') : null;
  if (!el) return;
  const action = el.dataset && el.dataset.change;
  if (action && Object.prototype.hasOwnProperty.call(_CHANGE_ACTIONS, action)) {
    _CHANGE_ACTIONS[action](el, e);
  }
});

document.addEventListener('input', (e) => {
  const el = e.target && typeof e.target.closest === 'function' ? e.target.closest('[data-input]') : null;
  if (!el) return;
  const action = el.dataset && el.dataset.input;
  if (action && Object.prototype.hasOwnProperty.call(_INPUT_ACTIONS, action)) {
    _INPUT_ACTIONS[action](el, e);
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && isModalOpen()) {
    if (typeof e.preventDefault === 'function') e.preventDefault();
    closeModal();
    return;
  }
  const el = e.target && typeof e.target.closest === 'function' ? e.target.closest('[data-keydown]') : null;
  if (!el) return;
  const action = el.dataset && el.dataset.keydown;
  if (action && Object.prototype.hasOwnProperty.call(_KEYDOWN_ACTIONS, action)) {
    _KEYDOWN_ACTIONS[action](el, e);
  }
});

document.addEventListener('error', (e) => {
  const el = e.target;
  const mode = el && el.dataset && el.dataset.imgFallback;
  if (!mode) return;
  el.style.display = 'none';
  if (mode === 'next' && el.nextElementSibling) {
    el.nextElementSibling.style.display = 'grid';
  }
}, true);

// Initial render & live fetch (waits for DOMContentLoaded when deferred scripts load)
async function boot() {
  initTelegramWebApp();
  if ('serviceWorker' in navigator && !isInTelegram() && location.protocol.startsWith('http')) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
  updateOnlineBar();
  const params = new URLSearchParams(location.search);
  const go0 = params.get('go');
  if (go0 && navItems.some(n => n[0] === go0)) active = go0;
  const urlCode = params.get('code');
  if (params.has('code')) {
    params.delete('code');
    const cleanQs = params.toString();
    const cleanUrl = location.pathname + (cleanQs ? '?' + cleanQs : '') + (location.hash || '');
    const hist = (typeof window !== 'undefined' && window.history && typeof window.history.replaceState === 'function')
      ? window.history
      : ((typeof history !== 'undefined' && typeof history.replaceState === 'function') ? history : null);
    if (hist) hist.replaceState(null, '', cleanUrl);
  }
  render();

  if (urlCode && !isInTelegram()) {
    // Link from the bot: code already stripped from URL before network request
    try { await pairWithCode(urlCode); toast('تم ربط الجهاز بنجاح'); }
    catch (e) { showPairing(e.message); }
  }
  fetchUserData();
  // Live refresh of relative time in header every 30s
  setInterval(updateTopBar, 30000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot, { once: true });
} else {
  boot();
}

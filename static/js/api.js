/* Telegram WebApp helpers, security utilities, API fetch wrapper, data sync & server mutations */

let _tgInited = false;

function getTg() {
  return (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
}

function isInTelegram() {
  const curTg = getTg();
  return Boolean(curTg && curTg.initData);
}

function initTelegramWebApp() {
  if (_tgInited) return;
  const curTg = getTg();
  if (!curTg) return;
  _tgInited = true;
  try {
    curTg.ready();
    curTg.expand();
    if (curTg.setHeaderColor) curTg.setHeaderColor('#ffffff');
    if (curTg.setBackgroundColor) curTg.setBackgroundColor('#f4f7fb');
  } catch (e) {}
}
initTelegramWebApp();

function getTelegramId() {
  const curTg = getTg();
  if (curTg && curTg.initDataUnsafe && curTg.initDataUnsafe.user && curTg.initDataUnsafe.user.id) {
    return String(curTg.initDataUnsafe.user.id);
  }
  const p = new URLSearchParams(window.location.search);
  return p.get('tid') || '';
}

const isStandalone = () => window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;

function getCookie(name) {
  try {
    const raw = (typeof document !== 'undefined' && typeof document.cookie === 'string') ? document.cookie : '';
    if (!raw) return '';
    const prefix = name + '=';
    for (const part of raw.split(';')) {
      const trimmed = part.trim();
      if (trimmed.startsWith(prefix)) {
        return decodeURIComponent(trimmed.slice(prefix.length));
      }
    }
  } catch (e) {
    console.warn('Failed to read cookie:', e);
  }
  return '';
}

function isLinkedDevice() {
  return Boolean(isDeviceLinked || getCookie('mutah_csrf'));
}

const _HTML_ESC_MAP = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
  '`': '&#96;',
  '\\': '&#92;'
};

function escapeHTML(s) {
  return String(s ?? '').replace(/[&<>"'`\\]/g, m => _HTML_ESC_MAP[m]);
}

function safeHttpUrl(raw) {
  if (typeof raw !== 'string') return null;
  const s = raw.trim();
  if (!s || /[\u0000-\u001F\u007F]/.test(s)) return null;
  // Reject protocol-relative URLs (//evil) and require explicit http(s):// scheme
  if (s.startsWith('//') || !/^https?:\/\//i.test(s)) return null;
  try {
    const u = new URL(s);
    return (u.protocol === 'https:' || u.protocol === 'http:') ? u.href : null;
  } catch (e) {
    return null;
  }
}

function openExternalLink(url) {
  const safeUrl = safeHttpUrl(url);
  if (!safeUrl) {
    toast('الرابط غير صالح أو غير متوفر');
    return false;
  }
  const curTg = getTg();
  if (curTg && typeof curTg.openLink === 'function') {
    try {
      curTg.openLink(safeUrl);
      return true;
    } catch (e) {
      console.warn('tg.openLink failed, falling back to window.open:', e);
    }
  }
  window.open(safeUrl, '_blank', 'noopener,noreferrer');
  return true;
}

async function apiFetch(url, options = {}) {
  const opts = Object.assign({}, options);
  const method = String(opts.method || 'GET').toUpperCase();
  opts.method = method;
  opts.credentials = 'same-origin';

  const headers = new Headers(opts.headers || {});
  // H1: pass Telegram initData exclusively via header, never in URL or JSON body
  const curTg = getTg();
  const initData = (curTg && curTg.initData) ? String(curTg.initData) : '';
  if (initData) {
    headers.set('X-Telegram-Init-Data', initData);
  }
  // H16: attach CSRF token from mutah_csrf cookie on state-changing requests
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = getCookie('mutah_csrf');
    if (csrf) {
      headers.set('X-CSRF-Token', csrf);
    }
  }
  opts.headers = headers;

  const res = await window.fetch(url, opts);
  if (res && res.status === 401) {
    const urlStr = typeof url === 'string' ? url : (url && url.url) || '';
    await clearClientSession();
    if (!urlStr.startsWith('/api/pair')) {
      try {
        const d = await res.clone().json();
        if (d && d.auth_required) showPairing();
      } catch (err) {
        console.warn('Failed to parse 401 response:', err);
      }
    }
  }
  return res;
}

async function pairWithCode(code) {
  const res = await apiFetch('/api/pair', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, device: (isStandalone() ? 'تطبيق مثبّت · ' : 'متصفح · ') + navigator.userAgent.slice(0, 80) })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok || !d.success) throw new Error(d.error || 'تعذر ربط الجهاز');
  isDeviceLinked = true;
  return true;
}

async function fetchUserData() {
  try {
    const tid = getTelegramId();
    const qs = new URLSearchParams({
      tid: tid,
      _t: Date.now()
    });
    const res = await apiFetch('/api/user-data?' + qs.toString());
    const data = await res.json().catch(() => ({}));

    if (!res.ok || !data.success || data.not_registered) {
      isLoggedIn = false;
      updateTopBar();
      render();
      return;
    }

    isLoggedIn = true;
    if (!isInTelegram()) isDeviceLinked = true;
    userProfile = data.user || userProfile;

    // Merge server user_meta if present
    if (data.user_meta && typeof data.user_meta === 'object') {
      const m = data.user_meta;
      if (m.notes && typeof m.notes === 'object') notes = { ...notes, ...m.notes };
      if (Array.isArray(m.personal_reminders)) reminders = m.personal_reminders;
      if (Array.isArray(m.read_announcements)) m.read_announcements.forEach(id => readSet.add(String(id)));
      if (m.absence_limit) limit = Number(m.absence_limit) || 20;
      if (m.course_meta && typeof m.course_meta === 'object') courseMeta = { ...courseMeta, ...m.course_meta };
      if (Array.isArray(m.sync_logs)) syncLogs = m.sync_logs;
      persist(false);
    }

    attendanceList = data.attendance || [];
    const attByCourse = {};
    attendanceList.forEach(a => {
      attByCourse[String(a.course_id)] = a;
    });

    grades = (data.grades || []).map(g => {
      const itemUrl = g.item_url || g.url || '';
      return {
        id: g.item_id,
        course_id: String(g.course_id),
        course_name: g.course_name || 'مقرر',
        item_name: g.item_name || 'تقييم',
        grade_val: g.grade_val || '',
        grade_max: g.grade_max || '',
        percentage: g.percentage || '',
        feedback: g.feedback || '',
        item_url: itemUrl,
        url: itemUrl,
        updated_at: g.updated_at ? String(g.updated_at).slice(0, 10) : 'مرصود'
      };
    });

    const gradeByItem = {};
    grades.forEach(g => {
      if (g.id && g.grade_val) {
        gradeByItem[String(g.id).replace('mod_', '')] = g;
      }
    });

    courses = (data.courses || []).map(c => {
      const cid = String(c.id);
      const meta = courseMeta[cid] || {};
      const att = attByCourse[cid];
      const present = att ? (Number(att.present_count) || 0) + (Number(att.late_count) || 0) + (Number(att.excused_count) || 0) : 0;
      const absent = att ? (Number(att.absent_count) || 0) : 0;
      const taken = att ? (Number(att.taken_sessions) || (present + absent)) : 0;

      // Calculate average grade if numeric percentages exist for this course
      const cGrades = grades.filter(g => g.course_id === cid && g.grade_val);
      let avgPct = null;
      const pctNums = [];
      cGrades.forEach(g => {
        const pm = String(g.percentage || '').match(/(\d+(?:\.\d+)?)/);
        if (pm) pctNums.push(parseFloat(pm[1]));
      });
      if (pctNums.length > 0) {
        avgPct = Math.round(pctNums.reduce((a, b) => a + b, 0) / pctNums.length);
      }

      return {
        id: cid,
        name: c.name,
        url: c.url || `https://mulms.mutah.edu.jo/course/view.php?id=${encodeURIComponent(cid)}`,
        is_monitored: Boolean(c.is_monitored),
        code: meta.code || `ID ${cid}`,
        teacher: meta.teacher || extractTeacher(c.name),
        room: meta.room || 'بوابة التعليم الإلكتروني',
        time: meta.time || '',
        attendance: present,
        absent: absent,
        total: taken,
        att_points: att ? (att.points_str || '') : '',
        att_pct_str: att ? (att.percentage_str || '') : '',
        att_sessions: att ? (att.sessions || []) : [],
        att_url: att ? (att.att_url || '') : '',
        grade: avgPct,
        graded_count: cGrades.length,
        stats: c.stats || { assignments: 0, quizzes: 0, resources: 0, tasks_total: 0, tasks_done: 0 }
      };
    });

    if (!selectedCourse && courses.length > 0) {
      selectedCourse = courses[0].id;
    }

    tasks = (data.deadlines || []).map(d => {
      const cidStr = String(d.content_id || d.id);
      const rawCmid = cidStr.replace(/^(assign_|quiz_)/, '');
      const isQuiz = d.content_type === 'quiz' || cidStr.startsWith('quiz_');
      const dueObj = parseLmsDate(d.due_str, d.due_ts);
      const matchedGrade = gradeByItem[rawCmid] || null;
      const isSub = Boolean(d.is_submitted);
      if (isSub) doneSet.add(cidStr);
      else doneSet.delete(cidStr);

      return {
        id: cidStr,
        title: d.content_name || 'مهمة دراسية',
        course: String(d.course_id || ''),
        course_name: d.course_name || course(d.course_id).name,
        type: isQuiz ? 'اختبار قصير' : 'واجب',
        raw_type: isQuiz ? 'quiz' : 'assign',
        due: dueObj ? dueObj.toISOString() : '',
        dueDateObj: dueObj,
        due_str: d.due_str || '',
        time_remaining: d.time_remaining || '',
        desc: isQuiz
          ? 'اختبار إلكتروني عبر منصة جامعة مؤتة (Moodle LMS).'
          : 'واجب تسليم إلكتروني عبر منصة جامعة مؤتة (Moodle LMS).',
        url: d.content_url || `https://mulms.mutah.edu.jo/course/view.php?id=${encodeURIComponent(d.course_id)}`,
        done: isSub,
        grade: matchedGrade
      };
    });

    announcements = (data.announcements || []).map(a => ({
      id: String(a.discuss_id || a.id),
      course: String(a.course_id || ''),
      course_name: a.course_name || course(a.course_id).name,
      title: a.title || 'إعلان دراسي',
      author: a.author || '',
      date: a.posted_date || (a.first_seen ? formatFriendlyDateTime(a.first_seen) : 'حديثاً'),
      body: a.body_text || a.forum_name || 'إعلان منشور في منتدى المقرر على نظام LMS.',
      url: a.url || ''
    }));

    files = (data.files || []).map(f => ({
      id: String(f.id || ''),
      name: f.name || 'ملف دراسي',
      course_id: String(f.course_id || ''),
      course: f.course || 'مقرر دراسي',
      kind: f.kind || 'ملف',
      date: f.date ? formatDateOnly(f.date) : 'مرصود',
      url: f.url || ''
    }));

    updateTopBar();
    render();
  } catch (err) {
    console.error('Error fetching data:', err);
    toast('تعذر تحديث البيانات من الخادم');
  }
}

async function submitAddCourse() {
  const val = document.getElementById('newCourseInput')?.value?.trim();
  if (!val) {
    toast('يرجى إدخال رقم المقرر أو رابطه');
    return;
  }
  const btn = document.getElementById('btnAddCourseSubmit');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'جارٍ جلب المقرر ومزامنته من LMS...';
  }
  try {
    const res = await apiFetch('/api/add-course', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tid: getTelegramId(), course_id: val })
    });
    const d = await res.json().catch(() => ({}));
    if (res.ok && d.success) {
      closeModal();
      toast(`تمت إضافة المقرر: ${d.course_name}`);
      await fetchUserData();
    } else {
      toast(d.error || 'تعذر إضافة المقرر');
    }
  } catch (e) {
    toast('حدث خطأ أثناء الاتصال بالخادم');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '＋ ربط المقرر وفحص محتواه';
    }
  }
}

async function syncAllCoursesFromLms() {
  if (!isLoggedIn) {
    toast('سجّل دخولك أولاً من الإعدادات');
    go('settings');
    return;
  }
  toast('جارٍ استيراد وفحص مقرراتك المسجلة من LMS...');
  try {
    const res = await apiFetch('/api/sync-courses', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tid: getTelegramId() })
    });
    const d = await res.json().catch(() => ({}));
    if (res.ok && d.success) {
      await fetchUserData();
      toast(`تم استيراد ومزامنة ${d.synced_count} مقررات بنجاح`);
    } else {
      toast(d.error || 'تعذر استيراد المقررات');
    }
  } catch (e) {
    toast('حدث خطأ أثناء الاتصال بالخادم');
  }
}

async function toggleCourseMonitoring(cid) {
  const targetCourse = courses.find(x => String(x.id) === String(cid));
  const prevMonitored = targetCourse ? targetCourse.is_monitored : undefined;
  if (targetCourse) {
    targetCourse.is_monitored = !prevMonitored;
    render();
  }
  try {
    const res = await apiFetch('/api/toggle-course', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tid: getTelegramId(), course_id: cid })
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || !d.success) {
      throw new Error(d.error || 'تعذر تحديث حالة مراقبة المقرر');
    }
    await fetchUserData();
    toast(d.is_monitored ? 'تم تفعيل مراقبة المقرر' : 'تم إيقاف مراقبة المقرر مؤقتاً');
  } catch (e) {
    if (targetCourse && prevMonitored !== undefined) {
      targetCourse.is_monitored = prevMonitored;
      render();
    }
    toast('تعذر تحديث حالة مراقبة المقرر');
  }
}

async function removeCourseById(cid) {
  if (!confirm('هل أنت متأكد من إزالة هذا المقرر من قائمة المراقبة؟')) return;
  try {
    const res = await apiFetch('/api/delete-course', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tid: getTelegramId(), course_id: cid })
    });
    const d = await res.json().catch(() => ({}));
    if (res.ok && d.success) {
      selectedCourse = null;
      await fetchUserData();
      go('courses');
      toast('تم حذف المقرر من قائمة المراقبة');
    } else {
      toast(d.error || 'تعذر حذف المقرر');
    }
  } catch (e) {
    toast('حدث خطأ أثناء حذف المقرر');
  }
}

async function setServerPref(key, val) {
  if (!_ALLOWED_PREF_KEYS.has(key)) return;
  const prev = userProfile[key];
  const next = Boolean(val);
  userProfile[key] = next;
  try {
    const res = await apiFetch('/api/update-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tid: getTelegramId(), [key]: next })
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || d.success === false) {
      throw new Error(d.error || 'تعذر حفظ الإعدادات');
    }
    toast('تم حفظ تفضيل الإشعارات في النظام');
  } catch (e) {
    userProfile[key] = prev;
    render();
    toast('تعذر حفظ الإعدادات');
  }
}

async function setServerInterval(val) {
  const num = Number(val);
  if (!Number.isInteger(num) || num < CHECK_INTERVAL_MIN || num > CHECK_INTERVAL_MAX) {
    toast(`فترة الفحص يجب أن تكون بين ${CHECK_INTERVAL_MIN} و${CHECK_INTERVAL_MAX} دقيقة`);
    render();
    return false;
  }
  const prev = userProfile.check_interval;
  userProfile.check_interval = num;
  try {
    const res = await apiFetch('/api/update-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tid: getTelegramId(), check_interval: num })
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || d.success === false) {
      throw new Error(d.error || 'تعذر حفظ فترة الفحص');
    }
    toast(`تم ضبط فترة الفحص التلقائي: كل ${num} دقائق`);
    return true;
  } catch (e) {
    userProfile.check_interval = prev;
    render();
    toast('تعذر حفظ فترة الفحص');
    return false;
  }
}

async function setServerMonitoring(val) {
  const prev = userProfile.is_monitoring;
  const next = Boolean(val);
  userProfile.is_monitoring = next;
  updateTopBar();
  try {
    const res = await apiFetch('/api/update-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tid: getTelegramId(), is_monitoring: next })
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || d.success === false) {
      throw new Error(d.error || 'تعذر تغيير حالة المراقبة');
    }
    toast(next ? 'تم تفعيل المراقبة التلقائية' : 'تم إيقاف المراقبة التلقائية');
  } catch (e) {
    userProfile.is_monitoring = prev;
    updateTopBar();
    render();
    toast('تعذر تغيير حالة المراقبة');
  }
}

async function logoutAccount() {
  if (!confirm('هل تريد بالتأكيد تسجيل الخروج من حسابك الأكاديمي وإيقاف المراقبة؟')) return;
  try {
    const res = await apiFetch('/api/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tid: getTelegramId() })
    });
    const d = await res.json().catch(() => ({}));
    if (res.ok && d.success) {
      await clearClientSession();
      updateTopBar();
      render();
      toast('تم تسجيل الخروج من الحساب بنجاح');
    } else {
      toast(d.error || 'حدث خطأ أثناء تسجيل الخروج');
    }
  } catch (e) {
    toast('حدث خطأ أثناء تسجيل الخروج');
  }
}

async function syncNow() {
  if (!isLoggedIn) {
    toast('سجّل دخولك أولاً لمزامنة مقرراتك من LMS');
    go('settings');
    return;
  }
  if (isSyncing) return;
  isSyncing = true;

  const stEl = document.getElementById('syncState');
  const topBtn = document.getElementById('btnSyncTop');
  if (stEl) stEl.textContent = 'جارٍ الفحص…';
  if (topBtn) topBtn.innerHTML = '<span class="spin">↻</span>';
  toast('جارٍ فحص كافة المقررات والواجبات في LMS...');

  try {
    const res = await apiFetch('/api/force-check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tid: getTelegramId() })
    });
    const d = await res.json().catch(() => ({}));
    if (res.ok && d.success) {
      await fetchUserData();
      toast(d.new_items > 0 ? `اكتمل الفحص: تم رصد ${d.new_items} تحديث جديد` : 'اكتمل الفحص الفوري — جميع البيانات محدّثة');
    } else {
      toast(d.error || 'تعذر إكمال الفحص الفوري');
    }
  } catch (e) {
    toast('حدث خطأ أثناء الاتصال بالخادم');
  } finally {
    isSyncing = false;
    if (topBtn) topBtn.innerHTML = '↻';
    updateTopBar();
    if (active === 'help') render();
  }
}

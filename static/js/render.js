/* Page, card, and modal renderers (strictly event-delegated via data-*, zero inline on*= attributes) */

function showPairing(msg) {
  const pv = document.getElementById('pairView');
  if (pv) {
    pv.style.display = 'flex';
    pv.setAttribute && pv.setAttribute('aria-hidden', 'false');
  }
  if (msg) document.getElementById('pairErr').textContent = msg;
}

function hidePairing() {
  const pv = document.getElementById('pairView');
  if (pv) {
    pv.style.display = 'none';
    pv.setAttribute && pv.setAttribute('aria-hidden', 'true');
  }
}

function updateOnlineBar() {
  document.getElementById('offlineBar').style.display = navigator.onLine ? 'none' : 'block';
}

function toast(s) {
  const e = document.getElementById('toast');
  if (!e) return;
  e.textContent = s;
  e.style.display = 'block';
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => { e.style.display = 'none'; }, 3000);
}

function updateTopBar() {
  const stEl = document.getElementById('syncState');
  const whEl = document.getElementById('syncWhen');
  const boxEl = document.getElementById('syncBox');
  const avEl = document.getElementById('userAvatar');

  if (!isLoggedIn) {
    stEl.textContent = 'غير مربوط';
    whEl.textContent = 'سجل دخولك';
    boxEl.classList.add('paused');
    avEl.textContent = '؟';
    return;
  }

  if (userProfile.is_monitoring) {
    stEl.textContent = 'مزامنة نشطة';
    boxEl.classList.remove('paused');
  } else {
    stEl.textContent = 'مراقبة متوقفة';
    boxEl.classList.add('paused');
  }
  const rel = formatRelativeTime(userProfile.last_check);
  const full = formatFriendlyDateTime(userProfile.last_check, false);
  whEl.textContent = rel;
  whEl.title = 'آخر فحص: ' + full;
  if (boxEl) boxEl.title = 'آخر فحص: ' + full;

  const cleanName = String(userProfile.full_name || 'طالب').trim();
  avEl.textContent = cleanName ? cleanName.charAt(0) : 'ط';
}

function header(title, sub) {
  return `<div class="page-title"><h1>${escapeHTML(title)}</h1><p>${escapeHTML(sub)}</p></div>`;
}

function courseCard(c) {
  const missRate = c.total ? Math.round((c.absent / c.total) * 100) : 0;
  const alert = c.total > 0 && missRate >= limit;
  const hasUrgentTask = tasks.some(t => t.course === c.id && !isTaskDone(t) && t.dueDateObj && (t.dueDateObj.getTime() - Date.now() <= 172800000) && (t.dueDateObj.getTime() >= Date.now()));
  let pillClass = '';
  let pillText = 'محدّث';
  if (!c.is_monitored) {
    pillClass = 'neutral';
    pillText = 'متوقف مؤقتاً';
  } else if (alert) {
    pillClass = 'red';
    pillText = 'اقتراب من حد الغياب';
  } else if (hasUrgentTask) {
    pillClass = 'warn';
    pillText = 'واجب قريب';
  }

  const tasksInfo = c.stats && c.stats.tasks_total > 0
    ? ` · المهام: ${c.stats.tasks_done}/${c.stats.tasks_total} منجز`
    : '';
  const filesInfo = c.stats && c.stats.resources > 0
    ? ` · ${c.stats.resources} ملف`
    : '';

  return `<article class="course">
    <div>
      <button class="text-link course-title" data-action="open-course" data-id="${escapeHTML(c.id)}">${escapeHTML(c.name)}</button>
      <div class="course-meta">${escapeHTML(c.code)} · ${escapeHTML(c.teacher)}${escapeHTML(tasksInfo)}${escapeHTML(filesInfo)}${c.time ? ' · ' + escapeHTML(c.time) : ''}</div>
    </div>
    <div class="course-end">
      <span class="pill ${pillClass}">${pillText}</span>
      <button class="text-link" data-action="open-course" data-id="${escapeHTML(c.id)}">تفاصيل ‹</button>
    </div>
  </article>`;
}

function taskCard(t) {
  const [label, cl] = dueInfo(t);
  const done = isTaskDone(t);
  const dueDisplay = fmtDate(t.dueDateObj, t.due_str);
  const hasNote = Boolean(notes[t.id] && notes[t.id].trim());

  return `<article class="task">
    <div>
      <button class="text-link item-title" data-action="task-detail" data-id="${escapeHTML(t.id)}">${escapeHTML(t.title)}</button>
      <div class="item-meta">${escapeHTML(t.course_name)} · ${escapeHTML(t.type)} · ${escapeHTML(dueDisplay)}</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
        ${!done && t.time_remaining && !label.includes(t.time_remaining) ? `<span class="due ${cl === 'pill red' ? 'late' : ''}">◷ متبقي ${escapeHTML(t.time_remaining)}</span>` : ''}
        ${t.grade ? `<span class="due" style="background:#e8f5ed;color:#28724b">الدرجة: ${escapeHTML(t.grade.grade_val)}${t.grade.grade_max ? ' / ' + escapeHTML(t.grade.grade_max) : ''}</span>` : ''}
        ${hasNote ? `<span class="tag" style="margin-top:9px">✎ ملاحظة مسجلة</span>` : ''}
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end">
      <span class="${cl}">${escapeHTML(label)}</span>
      <button class="btn" data-action="toggle-done" data-id="${escapeHTML(t.id)}">${done ? 'إلغاء الإنجاز' : 'إنجاز ✓'}</button>
    </div>
  </article>`;
}

function announceCard(a) {
  const read = readSet.has(String(a.id));
  return `<article class="notice ${read ? '' : 'unread'}">
    <div class="notice-mark">${read ? '✓' : '!'}</div>
    <div>
      <button class="text-link item-title" data-action="open-announcement" data-id="${escapeHTML(a.id)}">${escapeHTML(a.title)}</button>
      <div class="item-meta">${escapeHTML(a.course_name)}${a.author ? ' · ' + escapeHTML(a.author) : ''} · ${escapeHTML(a.date)}</div>
      <p class="item-meta">${escapeHTML(a.body)}</p>
    </div>
    <button class="btn" data-action="read-announcement" data-id="${escapeHTML(a.id)}">${read ? 'مقروء' : 'تمييز كمقروء'}</button>
  </article>`;
}

function fileCard(f) {
  return `<article class="file-row">
    <div>
      <strong class="item-title">▧ ${escapeHTML(f.name)}</strong>
      <div class="item-meta">${escapeHTML(f.course)} · ${escapeHTML(f.kind)} · رُصد: ${escapeHTML(f.date)}</div>
    </div>
    <button class="btn" data-action="open-link" data-url="${escapeHTML(f.url)}">فتح / تنزيل ↗</button>
  </article>`;
}

function render() {
  document.getElementById('sideNav').innerHTML = navItems.map(n => `
    <button class="${active === n[0] ? 'active' : ''}" data-action="go" data-page="${escapeHTML(n[0])}">
      <span class="ico">${n[1]}</span>${n[2]}
      ${n[0] === 'notifications' && unreadCount() > 0 ? `<span class="count">${unreadCount()}</span>` : ''}
      ${n[0] === 'tasks' && tasks.filter(t => !isTaskDone(t)).length > 0 ? `<span class="count">${tasks.filter(t => !isTaskDone(t)).length}</span>` : ''}
    </button>
  `).join('');

  const mobilePrimary = [navItems[0], navItems[2], navItems[3], navItems[4], navItems[8]];
  document.getElementById('bottomNav').innerHTML = mobilePrimary.map(n => `
    <button class="${active === n[0] ? 'active' : ''}" data-action="go" data-page="${escapeHTML(n[0])}">
      <span>${n[1]}</span>${n[2]}
    </button>
  `).join('') + `
    <button class="${!mobilePrimary.some(m => m[0] === active) ? 'active' : ''}" data-action="open-more-menu">
      <span>☰</span>المزيد
    </button>
  `;

  document.getElementById('bellCount').textContent = unreadCount() ? ` ${unreadCount()}` : '';
  const semEl = document.getElementById('semesterLabel');
  if (semEl) semEl.textContent = getCurrentSemesterLabel();

  const el = document.getElementById('content');
  const pages = {
    home: homePage,
    week: weekPage,
    courses: coursesPage,
    tasks: tasksPage,
    grades: gradesPage,
    attendance: attendancePage,
    announcements: announcementsPage,
    files: filesPage,
    notifications: notificationsPage,
    settings: settingsPage,
    help: helpPage,
    courseDetail: courseDetailPage
  };
  el.innerHTML = (pages[active] || homePage)();
  if (active === 'grades') filterGrades();
}

function go(p) {
  active = p;
  render();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function openMoreMenuModal() {
  openModal(`
    <div class="modal-head">
      <h2 id="modalTitle">أقسام دفتر الطالب</h2>
      <button type="button" class="close" aria-label="إغلاق النافذة" data-action="close-modal">×</button>
    </div>
    <p class="item-meta">انتقل لأي قسم من أقسام المتابعة الأكاديمية:</p>
    <div class="help-grid" style="margin-top:12px">
      ${navItems.map(n => `
        <button class="btn ${active === n[0] ? 'btn-primary' : ''}" style="display:flex;align-items:center;gap:8px;justify-content:flex-start;padding:11px" data-action="close-and-go" data-page="${escapeHTML(n[0])}">
          <span style="font-size:16px">${n[1]}</span>
          <span>${n[2]}</span>
        </button>
      `).join('')}
    </div>
  `);
}

function loginBannerCard() {
  return `<section class="card" style="margin-bottom:18px;border-color:#c6d5ec">
    <div class="eyebrow" style="color:var(--teal)">ربط الحساب الأكاديمي · جامعة مؤتة</div>
    <h2 style="font-size:16px;margin:0 0 4px">سجّل دخولك إلى نظام التعليم الإلكتروني (LMS)</h2>
    <p class="item-meta">أدخل رقمك الجامعي وكلمة مرور المودل لمزامنة مقرراتك وواجباتك وعلاماتك وحضورك.</p>
    <div class="form-grid" style="max-width:420px">
      <div>
        <label>الرقم الجامعي (Student ID)</label>
        <input id="loginUser" class="field" style="width:100%" placeholder="مثال: 120242222083" dir="ltr">
      </div>
      <div>
        <label>كلمة المرور (Password)</label>
        <input id="loginPass" type="password" class="field" style="width:100%" placeholder="••••••••" dir="ltr">
      </div>
      <div class="actions">
        <button class="btn btn-primary" id="btnDoLogin" data-action="submit-login">تسجيل الدخول وربط الحساب</button>
      </div>
    </div>
  </section>`;
}

function homePage() {
  const hour = new Date().getHours();
  const greet = hour < 12 ? 'صباح الخير' : 'مساء الخير';
  const firstName = String(userProfile.full_name || 'طالبنا').trim().split(/\s+/)[0];
  const todayStr = new Date().toLocaleDateString('ar-JO', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });

  const remaining = tasks
    .filter(t => !isTaskDone(t))
    .sort((a, b) => {
      if (a.dueDateObj && b.dueDateObj) return a.dueDateObj - b.dueDateObj;
      if (a.dueDateObj) return -1;
      if (b.dueDateObj) return 1;
      return 0;
    });

  const upcoming = remaining.find(t => !t.dueDateObj || t.dueDateObj.getTime() >= Date.now() - 86400000) || remaining[0];

  const urgent = upcoming
    ? `<section class="card urgent">
        <div>
          <div class="eyebrow">يستحق انتباهك · ${escapeHTML(upcoming.type)}</div>
          <h2>${escapeHTML(upcoming.title)}</h2>
          <p>${escapeHTML(upcoming.course_name)} · الموعد: ${escapeHTML(fmtDate(upcoming.dueDateObj, upcoming.due_str))}</p>
          <span class="due ${dueInfo(upcoming)[1] === 'pill red' ? 'late' : ''}">◷ ${escapeHTML(dueInfo(upcoming)[0])}</span>
        </div>
        <button class="btn" data-action="task-detail" data-id="${escapeHTML(upcoming.id)}">التفاصيل ←</button>
      </section>`
    : `<section class="card callout info">لا توجد مهام غير منجزة حالياً. أحسنت، جميع واجباتك واختباراتك مكتملة.</section>`;

  const attCourses = courses.filter(c => c.total > 0);
  let attAvgText = '—';
  if (attCourses.length > 0) {
    const sumRate = attCourses.reduce((acc, c) => acc + Math.round((c.attendance / c.total) * 100), 0);
    attAvgText = Math.round(sumRate / attCourses.length) + '٪';
  }

  return `
    <div class="welcome">
      <div>
        <h1>${greet}، ${escapeHTML(firstName)}</h1>
        <p>موجزك الأكاديمي لليوم · ${escapeHTML(todayStr)}</p>
      </div>
      <span class="tag">${escapeHTML(getCurrentSemesterLabel())}</span>
    </div>
    ${!isLoggedIn ? loginBannerCard() : urgent}
    <div class="section-head">
      <h2>ملخص اليوم</h2>
      <button data-action="go" data-page="week">عرض الأسبوع ←</button>
    </div>
    <div class="stats">
      <article class="stat" data-action="go" data-page="courses">
        <div class="stat-top">مقرراتي <span class="stat-icon">▤</span></div>
        <strong>${courses.length}</strong>
        <small>${courses.filter(c => c.is_monitored).length} قيد المتابعة</small>
      </article>
      <article class="stat" data-action="go" data-page="tasks">
        <div class="stat-top">مهام مفتوحة <span class="stat-icon">☷</span></div>
        <strong>${remaining.length}</strong>
        <small>من أصل ${tasks.length} مهام</small>
      </article>
      <article class="stat" data-action="go" data-page="attendance">
        <div class="stat-top">الحضور <span class="stat-icon">✓</span></div>
        <strong>${attAvgText}</strong>
        <small>${attCourses.length ? `عبر ${attCourses.length} مقررات مسجلة` : 'لا يوجد سجل حضور بعد'}</small>
      </article>
      <article class="stat" data-action="go" data-page="announcements">
        <div class="stat-top">جديد الإعلانات <span class="stat-icon">♧</span></div>
        <strong>${unreadCount()}</strong>
        <small>${files.length} ملفات دراسية مرصودة</small>
      </article>
    </div>
    <div class="grid2">
      <div>
        <div class="section-head">
          <h2>المهام القادمة</h2>
          <button data-action="go" data-page="tasks">كل المهام (${tasks.length}) ←</button>
        </div>
        <div class="task-list">
          ${remaining.slice(0, 4).map(taskCard).join('') || '<div class="empty">لا توجد مهام معلقة حالياً.</div>'}
        </div>
      </div>
      <div>
        <div class="section-head">
          <h2>إعلانات المقررات</h2>
          <button data-action="go" data-page="announcements">كل الإعلانات (${announcements.length}) ←</button>
        </div>
        <div class="notice-list">
          ${announcements.slice(0, 3).map(announceCard).join('') || '<div class="empty">لا توجد إعلانات منشورة من المدرسين حتى الآن.</div>'}
        </div>
      </div>
    </div>
    <div class="section-head">
      <h2>مقرراتك الدراسية</h2>
      <button data-action="go" data-page="courses">إدارة المقررات (${courses.length}) ←</button>
    </div>
    <div class="course-list">
      ${courses.slice(0, 4).map(courseCard).join('') || '<div class="empty">لم تقم بإضافة أي مقررات للمراقبة بعد. اضغط على «المقررات» لإضافة أو استيراد موادك.</div>'}
    </div>
    <p class="footnote">تتم مزامنة الواجبات والكويزات والعلامات والحضور والملفات تلقائياً مع منصة جامعة مؤتة (mulms.mutah.edu.jo).</p>
  `;
}

function weekCourseEvents(dayName) {
  return courses
    .filter(c => c.time && c.time.includes(dayName))
    .map(c => `<div class="event" data-action="open-course" data-id="${escapeHTML(c.id)}" style="cursor:pointer">${escapeHTML(c.name)}<small>${escapeHTML(c.time.split('·').slice(-1)[0] || 'محاضرة')}</small></div>`)
    .join('');
}

function weekPage() {
  const days = ['الأحد', 'الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت'];
  const { sunday, saturday } = getWeekBounds(weekOffset);
  const rangeStr = `${sunday.toLocaleDateString('ar-JO', { day: 'numeric', month: 'short' })} – ${saturday.toLocaleDateString('ar-JO', { day: 'numeric', month: 'short', year: 'numeric' })}`;
  const todayStr = new Date().toDateString();

  return `
    ${header('المخطط الأسبوعي', 'نظرة واحدة على المحاضرات ومواعيد تسليم الواجبات والاختبارات خلال الأسبوع. انقر على أي مهمة لعرض تفاصيلها.')}
    <div class="toolbar">
      <button class="btn" data-action="change-week" data-delta="-1">‹ الأسبوع السابق</button>
      <span class="tag">${escapeHTML(rangeStr)}</span>
      ${weekOffset !== 0 ? `<button class="btn" data-action="change-week" data-delta="0" data-reset="1">الأسبوع الحالي</button>` : ''}
      <button class="btn" data-action="change-week" data-delta="1">الأسبوع التالي ›</button>
      <button class="btn" style="margin-right:auto" data-action="open-schedule-modal">✎ ضبط مواعيد المحاضرات</button>
    </div>
    <div class="calendar">
      ${days.map((dName, i) => {
        const curDate = new Date(sunday.getFullYear(), sunday.getMonth(), sunday.getDate() + i);
        const isToday = curDate.toDateString() === todayStr;
        const ev = weekCourseEvents(dName);
        const dayTasks = tasks.filter(t => {
          if (!t.dueDateObj) return false;
          return t.dueDateObj.getFullYear() === curDate.getFullYear() &&
                 t.dueDateObj.getMonth() === curDate.getMonth() &&
                 t.dueDateObj.getDate() === curDate.getDate();
        });
        const tev = dayTasks.map(t => {
          const done = isTaskDone(t);
          return `<button class="event ${done ? 'done-ev' : 'assignment'}" data-action="task-detail" data-id="${escapeHTML(t.id)}">
            ${done ? '✓ ' : '◷ '}${escapeHTML(t.title)}
            <small>${escapeHTML(t.course_name)} · ${done ? 'مكتمل' : 'موعد تسليم'}</small>
          </button>`;
        }).join('');
        return `<div class="day ${isToday ? 'today-col' : ''}">
          <h3><span>${dName} · ${curDate.getDate()}</span>${isToday ? '<span class="pill">اليوم</span>' : ''}</h3>
          ${ev}${tev || ''}
        </div>`;
      }).join('')}
    </div>
    <p class="footnote">تظهر مواعيد تسليم الواجبات والكويزات تلقائياً في أيام استحقاقها المرصودة من LMS. يمكنك أيضاً إضافة أوقات محاضراتك من زر «ضبط مواعيد المحاضرات».</p>
  `;
}

function openScheduleModal() {
  if (!courses.length) {
    toast('أضف مقرراتك أولاً لضبط مواعيد المحاضرات');
    return;
  }
  openModal(`
    <div class="modal-head">
      <h2 id="modalTitle">ضبط أوقات وقاعات المحاضرات</h2>
      <button type="button" class="close" aria-label="إغلاق النافذة" data-action="close-modal">×</button>
    </div>
    <p class="item-meta">حدّد أيام وأوقات المحاضرات لكل مقرر ليتم عرضها تلقائياً في المخطط الأسبوعي (مثال: الأحد والثلاثاء · ١٠:٠٠):</p>
    <div class="form-grid" style="margin-top:12px">
      ${courses.map(c => `
        <div class="card" style="padding:10px">
          <strong style="font-size:12px;display:block;margin-bottom:6px">${escapeHTML(c.name)}</strong>
          <div class="grid2" style="gap:7px">
            <input class="field" id="sched_time_${escapeHTML(c.id)}" value="${escapeHTML(c.time)}" placeholder="الأيام والوقت (الأحد والثلاثاء · ١٠:٠٠)">
            <input class="field" id="sched_room_${escapeHTML(c.id)}" value="${escapeHTML(c.room === 'بوابة التعليم الإلكتروني' ? '' : c.room)}" placeholder="القاعة / المختبر (مثال: قاعة ٢٠١)">
          </div>
        </div>
      `).join('')}
    </div>
    <div class="actions" style="margin-top:14px">
      <button class="btn btn-primary" data-action="save-all-schedules">حفظ الجدول الدراسي</button>
      <button class="btn" data-action="close-modal">إلغاء</button>
    </div>
  `);
}

function coursesPage() {
  const filtered = courses.filter(c =>
    (c.name + ' ' + c.code + ' ' + c.teacher).toLowerCase().includes(courseSearchQuery.toLowerCase())
  );
  return `
    ${header('المقررات الدراسية', 'افتح أي مقرر لعرض مهامه وعلاماته وحضوره وإعلاناته وملفاته بالتفصيل، أو أضف مقررات جديدة للمراقبة.')}
    <div class="toolbar">
      <input class="field search" id="courseSearch" value="${escapeHTML(courseSearchQuery)}" placeholder="ابحث باسم المقرر أو رمزه أو المدرس..." data-input="filter-courses">
      <button class="btn btn-primary" data-action="open-add-course-modal">＋ إضافة مقرر</button>
      <button class="btn" data-action="sync-all-courses">استيراد من LMS</button>
    </div>
    <div class="course-list" id="courseList">
      ${filtered.map(courseCard).join('') || '<div class="empty">لا توجد مقررات مطابقة أو مضافة حالياً. اضغط على «＋ إضافة مقرر» لإضافة موادك.</div>'}
    </div>
  `;
}

function filterCourses(val) {
  courseSearchQuery = val || '';
  const listEl = document.getElementById('courseList');
  if (!listEl) return;
  const filtered = courses.filter(c =>
    (c.name + ' ' + c.code + ' ' + c.teacher).toLowerCase().includes(courseSearchQuery.toLowerCase())
  );
  listEl.innerHTML = filtered.map(courseCard).join('') || '<div class="empty">لا توجد مقررات مطابقة لبحثك.</div>';
}

function openAddCourseModal() {
  if (!isLoggedIn) {
    toast('يرجى تسجيل الدخول بحساب الجامعة أولاً');
    go('settings');
    return;
  }
  openModal(`
    <div class="modal-head">
      <h2 id="modalTitle">إضافة مقرر دراسي للمراقبة</h2>
      <button type="button" class="close" aria-label="إغلاق النافذة" data-action="close-modal">×</button>
    </div>
    <p class="item-meta">أدخل رقم المقرر (Course ID) أو الصق رابط صفحة المقرر من موقع جامعة مؤتة LMS:</p>
    <div class="form-grid">
      <div>
        <label>رقم المقرر أو الرابط (Course ID / URL)</label>
        <input id="newCourseInput" class="field" style="width:100%" placeholder="مثال: 71177 أو https://mulms.mutah.edu.jo/course/view.php?id=71177" dir="ltr">
      </div>
      <div class="actions">
        <button class="btn btn-primary" id="btnAddCourseSubmit" data-action="submit-add-course">＋ ربط المقرر وفحص محتواه</button>
        <button class="btn" data-action="close-and-sync-courses">استيراد كافة مقرراتي تلقائياً</button>
      </div>
    </div>
  `);
}

function openEditCourseMetaModal(cid) {
  const c = course(cid);
  openModal(`
    <div class="modal-head">
      <h2 id="modalTitle">بيانات المقرر: ${escapeHTML(c.name)}</h2>
      <button type="button" class="close" aria-label="إغلاق النافذة" data-action="close-modal">×</button>
    </div>
    <div class="form-grid" style="margin-top:12px">
      <div>
        <label>رمز المقرر (Course Code)</label>
        <input id="metaCode" class="field" style="width:100%" value="${escapeHTML(c.code)}">
      </div>
      <div>
        <label>اسم المدرس</label>
        <input id="metaTeacher" class="field" style="width:100%" value="${escapeHTML(c.teacher)}">
      </div>
      <div>
        <label>القاعة / المختبر</label>
        <input id="metaRoom" class="field" style="width:100%" value="${escapeHTML(c.room)}">
      </div>
      <div>
        <label>أيام ووقت المحاضرة (يظهر في المخطط الأسبوعي)</label>
        <input id="metaTime" class="field" style="width:100%" placeholder="مثال: الأحد والثلاثاء · ١٠:٠٠" value="${escapeHTML(c.time)}">
      </div>
      <div class="actions">
        <button class="btn btn-primary" data-action="save-course-meta" data-id="${escapeHTML(c.id)}">حفظ التعديلات</button>
        <button class="btn" data-action="close-modal">إلغاء</button>
      </div>
    </div>
  `);
}

function courseDetailPage() {
  const c = course(selectedCourse);
  if (!c || !courses.some(x => x.id === c.id)) return coursesPage();

  const ts = tasks.filter(t => t.course === c.id);
  const as = announcements.filter(a => a.course === c.id);
  const gs = grades.filter(g => g.course_id === c.id);
  const fs = files.filter(f => String(f.course_id) === c.id || f.course === c.name);
  const rate = Math.max(0, Math.min(100, c.total ? Math.round((c.attendance / c.total) * 100) : 0));

  return `
    ${header(c.name, `${c.code} · ${c.teacher} · ${c.room}${c.time ? ' · ' + c.time : ''}`)}
    <div class="toolbar">
      <button class="btn" data-action="go" data-page="courses">← كل المقررات</button>
      <span class="tag" title="التاريخ الكامل: ${escapeHTML(formatFriendlyDateTime(userProfile.last_check, false))}">آخر فحص: ${escapeHTML(formatFriendlyDateTime(userProfile.last_check, true))}</span>
      <button class="btn" data-action="open-edit-course-meta" data-id="${escapeHTML(c.id)}">✎ تعديل بيانات المحاضرة</button>
      <button class="btn" data-action="open-link" data-url="${escapeHTML(c.url)}">فتح المقرر في LMS ↗</button>
      <button class="btn" data-action="toggle-course-monitoring" data-id="${escapeHTML(c.id)}">${c.is_monitored ? 'إيقاف المراقبة' : 'تفعيل المراقبة'}</button>
      <button class="btn btn-danger" data-action="remove-course" data-id="${escapeHTML(c.id)}">حذف المقرر</button>
    </div>
    <div class="stats">
      <article class="stat" data-action="go" data-page="attendance">
        <div class="stat-top">الحضور <span class="stat-icon">✓</span></div>
        <strong>${c.total ? rate + '٪' : '—'}</strong>
        <small>${c.attendance} حضور · ${c.absent} غياب</small>
      </article>
      <article class="stat" data-action="go" data-page="grades">
        <div class="stat-top">العلامات المرصودة <span class="stat-icon">▥</span></div>
        <strong>${c.grade !== null ? c.grade + '٪' : gs.filter(g => g.grade_val).length}</strong>
        <small>${gs.length} بند تقييم مسجل</small>
      </article>
      <article class="stat" data-action="go" data-page="tasks">
        <div class="stat-top">المهام <span class="stat-icon">☷</span></div>
        <strong>${ts.length}</strong>
        <small>${ts.filter(isTaskDone).length} مكتمل · ${ts.filter(t => !isTaskDone(t)).length} قيد الحل</small>
      </article>
      <article class="stat" data-action="go" data-page="files">
        <div class="stat-top">الملفات والإعلانات <span class="stat-icon">▧</span></div>
        <strong>${fs.length}</strong>
        <small>${as.length} إعلانات منشورة</small>
      </article>
    </div>

    <div class="section-head">
      <h2>مهام المقرر (${ts.length})</h2>
      <button data-action="go" data-page="tasks">كل المهام</button>
    </div>
    <div class="task-list">
      ${ts.map(taskCard).join('') || '<div class="empty">لا توجد واجبات أو اختبارات مسجلة لهذا المقرر.</div>'}
    </div>

    <div class="section-head">
      <h2>علامات المقرر (${gs.length})</h2>
      <button data-action="go" data-page="grades">السجل الكامل</button>
    </div>
    ${gs.length ? `
      <div class="table-wrap">
        <table class="table">
          <thead><tr><th>بند التقييم</th><th>الدرجة</th><th>العظمى</th><th>النسبة / ملاحظات</th></tr></thead>
          <tbody>
            ${gs.map(g => `
              <tr>
                <td><b>${escapeHTML(g.item_name)}</b></td>
                <td><b>${escapeHTML(g.grade_val || '—')}</b></td>
                <td>${escapeHTML(g.grade_max || '—')}</td>
                <td>${escapeHTML(g.percentage || g.feedback || '—')}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    ` : '<div class="empty">لا توجد علامات مرصودة لهذا المقرر حتى الآن.</div>'}

    <div class="section-head">
      <h2>حضور المحاضرات</h2>
      <button data-action="go" data-page="attendance">السجل الكامل</button>
    </div>
    <div class="card">
      <div class="item-meta">
        ${c.total ? `${c.attendance} حضور · ${c.absent} غياب · من إجمالي ${c.total} محاضرة مسجلة` : 'لا يوجد سجل حضور مسجل لهذا المقرر في LMS'}
        ${c.att_points ? ` · النقاط: ${escapeHTML(c.att_points)}` : ''}
      </div>
      <div class="progress"><i style="width:${rate}%"></i></div>
    </div>

    <div class="section-head">
      <h2>إعلانات المقرر (${as.length})</h2>
      <button data-action="go" data-page="announcements">كل الإعلانات</button>
    </div>
    <div class="notice-list">
      ${as.map(announceCard).join('') || '<div class="empty">لا توجد إعلانات مسجلة لهذا المقرر.</div>'}
    </div>

    <div class="section-head">
      <h2>ملفات وموارد المقرر (${fs.length})</h2>
      <button data-action="go" data-page="files">مكتبة الملفات</button>
    </div>
    <div class="file-list">
      ${fs.map(fileCard).join('') || '<div class="empty">لا توجد ملفات مسجلة لهذا المقرر.</div>'}
    </div>
  `;
}

function getFilteredTasks() {
  const s = taskFilterState.state;
  const c = taskFilterState.course;
  const y = taskFilterState.type;

  return tasks
    .filter(t => {
      const done = isTaskDone(t);
      const late = t.dueDateObj && t.dueDateObj.getTime() < Date.now() && !done;
      const matchCourse = c === 'all' || String(t.course) === String(c);
      const matchType = y === 'all' || t.type === y;
      const matchState =
        s === 'all' ||
        (s === 'done' && done) ||
        (s === 'pending' && !done) ||
        (s === 'late' && late);
      return matchCourse && matchType && matchState;
    })
    .sort((a, b) => {
      const aDone = isTaskDone(a) ? 1 : 0;
      const bDone = isTaskDone(b) ? 1 : 0;
      if (aDone !== bDone) return aDone - bDone;
      if (a.dueDateObj && b.dueDateObj) return a.dueDateObj - b.dueDateObj;
      if (a.dueDateObj) return -1;
      if (b.dueDateObj) return 1;
      return 0;
    });
}

function tasksPage() {
  const list = getFilteredTasks();
  const doneCount = tasks.filter(isTaskDone).length;
  return `
    ${header('المهام والمواعيد', 'تابع الواجبات والاختبارات القصيرة وحالة تسليمها في LMS، وأضف ملاحظاتك وتذكيراتك أو صدّر الموعد إلى التقويم.')}
    <div class="toolbar">
      <select class="field" id="taskState" data-change="filter-tasks">
        <option value="all" ${taskFilterState.state === 'all' ? 'selected' : ''}>كل الحالات (${tasks.length})</option>
        <option value="pending" ${taskFilterState.state === 'pending' ? 'selected' : ''}>قيد الحل (${tasks.length - doneCount})</option>
        <option value="done" ${taskFilterState.state === 'done' ? 'selected' : ''}>مكتملة (${doneCount})</option>
        <option value="late" ${taskFilterState.state === 'late' ? 'selected' : ''}>متأخرة</option>
      </select>
      <select class="field" id="taskCourse" data-change="filter-tasks">
        <option value="all">كل المقررات</option>
        ${courses.map(c => `<option value="${escapeHTML(c.id)}" ${String(taskFilterState.course) === String(c.id) ? 'selected' : ''}>${escapeHTML(c.name)}</option>`).join('')}
      </select>
      <select class="field" id="taskType" data-change="filter-tasks">
        <option value="all" ${taskFilterState.type === 'all' ? 'selected' : ''}>كل الأنواع</option>
        <option value="واجب" ${taskFilterState.type === 'واجب' ? 'selected' : ''}>واجب</option>
        <option value="اختبار قصير" ${taskFilterState.type === 'اختبار قصير' ? 'selected' : ''}>اختبار قصير</option>
      </select>
      <button class="btn" style="margin-right:auto" data-action="sync-now">↻ تحديث من LMS</button>
    </div>
    <div class="task-list" id="taskList">
      ${list.map(taskCard).join('') || '<div class="empty">لا توجد مهام ضمن هذا التصنيف حالياً.</div>'}
    </div>
    <div class="callout info">تتم مزامنة حالة الإنجاز تلقائياً عند تسليم الواجب أو إنهاء الاختبار في نظام LMS، كما يمكنك تعليم المهمة كمنجزة لإيقاف تذكيرات العد التنازلي في البوت.</div>
  `;
}

function filterTasks() {
  taskFilterState.state = document.getElementById('taskState')?.value || 'all';
  taskFilterState.course = document.getElementById('taskCourse')?.value || 'all';
  taskFilterState.type = document.getElementById('taskType')?.value || 'all';
  const list = getFilteredTasks();
  const el = document.getElementById('taskList');
  if (el) el.innerHTML = list.map(taskCard).join('') || '<div class="empty">لا توجد مهام في هذا التصنيف.</div>';
}

function gradesPage() {
  return `
    ${header('سجل العلامات', 'تفاصيل الدرجات والتقييمات المرصودة لكل مقرر مباشرة من كشف علامات LMS.')}
    <div class="toolbar">
      <select class="field" id="gradeCourse" data-change="filter-grades">
        <option value="all">كل المقررات</option>
        ${courses.map(c => `<option value="${escapeHTML(c.id)}" ${String(gradeFilterState.course) === String(c.id) ? 'selected' : ''}>${escapeHTML(c.name)}</option>`).join('')}
      </select>
      <select class="field" id="gradeFilter" data-change="filter-grades">
        <option value="all" ${gradeFilterState.filter === 'all' ? 'selected' : ''}>كل التقييمات (${grades.length})</option>
        <option value="graded" ${gradeFilterState.filter === 'graded' ? 'selected' : ''}>المرصودة فقط (${grades.filter(g => g.grade_val && g.grade_val !== '-').length})</option>
      </select>
      <button class="btn" style="margin-right:auto" data-action="sync-now">↻ تحديث العلامات</button>
    </div>
    <div id="gradeTable"></div>
    <div class="callout">تُجلب الدرجات كما رصدها مدرس المقرر في سجل الدرجات (Gradebook) على منصة جامعة مؤتة.</div>
  `;
}

function filterGrades() {
  const c = document.getElementById('gradeCourse')?.value || gradeFilterState.course || 'all';
  const f = document.getElementById('gradeFilter')?.value || gradeFilterState.filter || 'all';
  gradeFilterState = { course: c, filter: f };

  const rows = grades.filter(g => {
    const matchCourse = c === 'all' || String(g.course_id) === String(c);
    const hasVal = Boolean(g.grade_val && g.grade_val !== '-' && g.grade_val !== '—');
    const matchFilter = f === 'all' || (f === 'graded' && hasVal);
    return matchCourse && matchFilter;
  });

  const tbl = document.getElementById('gradeTable');
  if (!tbl) return;
  if (!rows.length) {
    tbl.innerHTML = '<div class="empty">لا توجد علامات مسجلة تطابق الفلتر المحدد.</div>';
    return;
  }

  tbl.innerHTML = `
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>المقرر</th>
            <th>بند التقييم</th>
            <th>الدرجة</th>
            <th>المدى / العظمى</th>
            <th>النسبة / ملاحظات</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(r => {
            const gUrl = r.item_url || r.url || '';
            return `
            <tr>
              <td>${escapeHTML(r.course_name)}</td>
              <td>
                ${gUrl ? `<button class="text-link" style="font-weight:600" data-action="open-link" data-url="${escapeHTML(gUrl)}">${escapeHTML(r.item_name)} ↗</button>` : `<b>${escapeHTML(r.item_name)}</b>`}
              </td>
              <td><b style="color:var(--teal)">${escapeHTML(r.grade_val || '—')}</b></td>
              <td>${escapeHTML(r.grade_max || '—')}</td>
              <td>${escapeHTML([r.percentage, r.feedback].filter(Boolean).join(' · ') || '—')}</td>
            </tr>
          `;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function attendancePage() {
  const withAtt = courses.filter(c => c.total > 0 || c.att_sessions.length > 0);
  const withoutAtt = courses.filter(c => c.total === 0 && c.att_sessions.length === 0);

  return `
    ${header('الحضور والغياب', 'السجل التفصيلي والنسبة لكل مقرر مع تنبيه غياب شخصي قابل للضبط.')}
    <div class="toolbar">
      <label class="tag" style="padding:6px 10px;font-size:11px">
        حد التنبيه الشخصي:
        <input id="absenceLimit" type="number" value="${limit}" min="5" max="50" style="width:45px;border:1px solid var(--line);border-radius:5px;background:#fff;padding:2px 4px;text-align:center" data-change="set-limit">
        ٪ من الغياب
      </label>
      <span class="tag">النسبة الرسمية تعتمد على سياسة الجامعة</span>
      <button class="btn" style="margin-right:auto" data-action="sync-now">↻ تحديث السجلات</button>
    </div>
    <div class="course-list">
      ${withAtt.map(c => {
        const rate = Math.max(0, Math.min(100, c.total ? Math.round((c.attendance / c.total) * 100) : 100));
        const miss = Math.max(0, Math.min(100, c.total ? Math.round((c.absent / c.total) * 100) : 0));
        const alert = c.total > 0 && miss >= limit;
        return `
          <article class="card">
            <div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap">
              <div>
                <strong>${escapeHTML(c.name)}</strong>
                <div class="course-meta">
                  ${escapeHTML(c.code)} · ${c.attendance} حضور · ${c.absent} غياب من ${c.total} محاضرة مسجلة
                  ${c.att_points ? ` · النقاط: ${escapeHTML(c.att_points)}` : ''}
                </div>
              </div>
              <div style="display:flex;gap:6px;align-items:center">
                <span class="${alert ? 'pill red' : 'pill good'}">${alert ? 'تنبيه غياب (' + miss + '٪)' : rate + '٪ حضور'}</span>
                ${c.att_sessions && c.att_sessions.length ? `<button class="btn" data-action="open-attendance-sessions" data-id="${escapeHTML(c.id)}">سجل الجلسات (${c.att_sessions.length})</button>` : ''}
                ${c.att_url ? `<button class="btn" data-action="open-link" data-url="${escapeHTML(c.att_url)}">LMS ↗</button>` : ''}
              </div>
            </div>
            <div class="progress"><i style="width:${rate}%;background:${alert ? 'var(--red)' : 'var(--teal)'}"></i></div>
            ${alert ? `<p class="item-meta" style="color:var(--red);font-weight:600;margin-top:7px">⚠️ تنبيه: نسبة الغياب في هذا المقرر (${miss}٪) وصلت أو تجاوزت حد التنبيه الشخصي (${limit}٪).</p>` : ''}
          </article>
        `;
      }).join('') || '<div class="empty">لا توجد سجلات حضور (Attendance) مفعلة من قِبل المدرسين في مقرراتك الحالية حتى الآن.</div>'}
    </div>
    ${withoutAtt.length ? `
      <div class="section-head"><h2>مقررات بدون أداة حضور إلكتروني في LMS</h2></div>
      <div class="course-list">
        ${withoutAtt.map(c => `
          <article class="course">
            <div>
              <strong class="course-title">${escapeHTML(c.name)}</strong>
              <div class="course-meta">${escapeHTML(c.code)} · لم يضف مدرس المساق جدول حضور إلكتروني في صفحة المقرر</div>
            </div>
            <span class="pill neutral">غير مفعّل في LMS</span>
          </article>
        `).join('')}
      </div>
    ` : ''}
    <p class="footnote">استخدم حد التنبيه للمتابعة الشخصية؛ يرسل البوت أيضاً إشعاراً فورياً عبر تيليجرام عند رصد أي محاضرة جديدة في سجل الحضور.</p>
  `;
}

function openAttendanceSessionsModal(cid) {
  const c = course(cid);
  const sessions = c.att_sessions || [];
  openModal(`
    <div class="modal-head">
      <div>
        <span class="pill">سجل المحاضرات</span>
        <h2 id="modalTitle" style="margin-top:6px">${escapeHTML(c.name)}</h2>
      </div>
      <button type="button" class="close" aria-label="إغلاق النافذة" data-action="close-modal">×</button>
    </div>
    <p class="item-meta">${c.attendance} حضور · ${c.absent} غياب · الإجمالي: ${c.total} محاضرة</p>
    <div class="table-wrap" style="margin-top:10px">
      <table class="table">
        <thead><tr><th>التاريخ والوقت</th><th>الحالة</th><th>النقاط</th><th>ملاحظات</th></tr></thead>
        <tbody>
          ${sessions.map(s => {
            const st = String(s.status || '');
            const isAbs = /absent|غائب|غياب/i.test(st);
            const isPres = /present|حاضر|موجود/i.test(st);
            return `
              <tr>
                <td>${escapeHTML(formatFriendlyDateTime(s.date) || s.date || '—')}</td>
                <td><span class="pill ${isAbs ? 'red' : isPres ? 'good' : 'warn'}">${escapeHTML(st || '—')}</span></td>
                <td>${escapeHTML(s.points || '—')}</td>
                <td>${escapeHTML(s.remarks || '—')}</td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    </div>
    <div class="actions" style="margin-top:14px">
      ${c.att_url ? `<button class="btn btn-primary" data-action="open-link" data-url="${escapeHTML(c.att_url)}">فتح سجل الحضور في LMS ↗</button>` : ''}
      <button class="btn" data-action="close-modal">إغلاق</button>
    </div>
  `);
}

function announcementsPage() {
  const f = newsFilterState.filter;
  const c = newsFilterState.course;
  const list = announcements.filter(a => {
    const isRead = readSet.has(String(a.id));
    const matchRead = f === 'all' || (f === 'read' && isRead) || (f === 'unread' && !isRead);
    const matchCourse = c === 'all' || String(a.course) === String(c);
    return matchRead && matchCourse;
  });

  return `
    ${header('إعلانات المقررات', 'الإعلانات والمنشورات الصادرة من مدرسي المقررات في منتديات الأخبار على LMS.')}
    <div class="toolbar">
      <select class="field" id="newsFilter" data-change="filter-news">
        <option value="all" ${f === 'all' ? 'selected' : ''}>كل الإعلانات (${announcements.length})</option>
        <option value="unread" ${f === 'unread' ? 'selected' : ''}>غير المقروءة (${unreadCount()})</option>
        <option value="read" ${f === 'read' ? 'selected' : ''}>المقروءة</option>
      </select>
      <select class="field" id="newsCourse" data-change="filter-news">
        <option value="all">كل المقررات</option>
        ${courses.map(co => `<option value="${escapeHTML(co.id)}" ${String(c) === String(co.id) ? 'selected' : ''}>${escapeHTML(co.name)}</option>`).join('')}
      </select>
      <button class="btn" data-action="mark-all-read">تعليم الكل كمقروء</button>
    </div>
    <div class="notice-list" id="newsList">
      ${list.map(announceCard).join('') || '<div class="empty">لا توجد إعلانات ضمن هذا الفلتر حالياً.</div>'}
    </div>
  `;
}

function filterNews() {
  newsFilterState.filter = document.getElementById('newsFilter')?.value || 'all';
  newsFilterState.course = document.getElementById('newsCourse')?.value || 'all';
  render();
}

function openAnnouncementModal(id) {
  const a = announcements.find(x => String(x.id) === String(id));
  if (!a) return;
  readSet.add(String(a.id));
  persist(true);
  render();
  openModal(`
    <div class="modal-head">
      <div>
        <span class="pill">إعلان مقرر</span>
        <h2 id="modalTitle" style="margin-top:8px">${escapeHTML(a.title)}</h2>
      </div>
      <button type="button" class="close" aria-label="إغلاق النافذة" data-action="close-modal">×</button>
    </div>
    <p class="item-meta">${escapeHTML(a.course_name)}${a.author ? ' · الناشر: ' + escapeHTML(a.author) : ''} · ${escapeHTML(a.date)}</p>
    <div class="callout" style="white-space:pre-line">${escapeHTML(a.body)}</div>
    <div class="actions">
      ${a.url ? `<button class="btn btn-primary" data-action="open-link" data-url="${escapeHTML(a.url)}">فتح الإعلان في LMS ↗</button>` : ''}
      <button class="btn" data-action="close-modal">إغلاق</button>
    </div>
  `);
}

function filesPage() {
  const c = fileFilterState.course;
  const q = fileFilterState.q.toLowerCase();
  const list = files.filter(f =>
    (c === 'all' || f.course === c) &&
    (f.name.toLowerCase().includes(q) || f.course.toLowerCase().includes(q) || f.kind.toLowerCase().includes(q))
  );

  return `
    ${header('ملفات المقررات', 'المحاضرات والشرائح والملفات الدراسية التي رُفعت على صفحات مقرراتك في LMS.')}
    <div class="toolbar">
      <select class="field" id="fileCourse" data-change="filter-files">
        <option value="all">كل المقررات (${files.length} ملف)</option>
        ${courses.map(co => `<option value="${escapeHTML(co.name)}" ${c === co.name ? 'selected' : ''}>${escapeHTML(co.name)}</option>`).join('')}
      </select>
      <input class="field search" id="fileSearch" value="${escapeHTML(fileFilterState.q)}" placeholder="ابحث عن ملف أو محاضرة..." data-input="filter-files">
    </div>
    <div class="file-list" id="fileList">
      ${list.map(fileCard).join('') || '<div class="empty">لا توجد ملفات مطابقة لبحثك.</div>'}
    </div>
    <p class="footnote">اضغط على «فتح / تنزيل ↗» لفتح الملف مباشرة عبر منصة جامعة مؤتة (LMS)، كما يقوم البوت بإرسال الملفات الجديدة تلقائياً في محادثة تيليجرام.</p>
  `;
}

function filterFiles() {
  fileFilterState.course = document.getElementById('fileCourse')?.value || 'all';
  fileFilterState.q = document.getElementById('fileSearch')?.value || '';
  const c = fileFilterState.course;
  const q = fileFilterState.q.toLowerCase();
  const list = files.filter(f =>
    (c === 'all' || f.course === c) &&
    (f.name.toLowerCase().includes(q) || f.course.toLowerCase().includes(q) || f.kind.toLowerCase().includes(q))
  );
  const el = document.getElementById('fileList');
  if (el) el.innerHTML = list.map(fileCard).join('') || '<div class="empty">لا توجد ملفات مطابقة.</div>';
}

function notificationsPage() {
  const entries = [
    ...announcements.filter(a => !readSet.has(String(a.id))).map(a => ({
      kind: 'إعلان جديد',
      pill: 'pill',
      title: a.title,
      meta: `${a.course_name} · ${a.date}`,
      action: 'open-announcement',
      id: String(a.id)
    })),
    ...tasks.filter(t => !isTaskDone(t)).map(t => {
      const [lbl, cl] = dueInfo(t);
      return {
        kind: lbl,
        pill: cl,
        title: `${t.type}: ${t.title}`,
        meta: `${t.course_name} · ${fmtDate(t.dueDateObj, t.due_str)}`,
        action: 'task-detail',
        id: String(t.id)
      };
    }),
    ...courses.filter(c => c.total > 0 && Math.round((c.absent / c.total) * 100) >= limit).map(c => ({
      kind: 'تنبيه غياب',
      pill: 'pill red',
      title: `تجاوز حد الغياب الشخصي في: ${c.name}`,
      meta: `${c.absent} غياب من ${c.total} محاضرة (${Math.round((c.absent / c.total) * 100)}٪)`,
      action: 'go',
      page: 'attendance'
    })),
    ...reminders.map((r, idx) => ({
      kind: 'تذكير شخصي',
      pill: 'pill purple',
      title: `تذكير: ${r.title}`,
      meta: `${r.course_name ? r.course_name + ' · ' : ''}${r.when}`,
      action: r.task_id ? 'task-detail' : 'show-toast',
      id: r.task_id ? String(r.task_id) : '',
      msg: r.task_id ? '' : 'تذكير شخصي محفوظ',
      delIdx: idx
    }))
  ];

  return `
    ${header('مركز الإشعارات والتنبيهات', 'الإعلانات غير المقروءة والمهام الجارية وتنبيهات الغياب والتذكيرات الشخصية في مكان واحد.')}
    <div class="toolbar">
      <button class="btn" data-action="mark-all-read">تعليم الإعلانات كمقروءة</button>
      <button class="btn" data-action="open-custom-reminder">＋ تذكير شخصي جديد</button>
      <span class="tag">${entries.length} عناصر</span>
    </div>
    <div class="generic-list">
      ${entries.map(e => `
        <article class="generic-row">
          <div>
            <button class="text-link item-title" data-action="${escapeHTML(e.action)}"${e.id ? ` data-id="${escapeHTML(e.id)}"` : ''}${e.page ? ` data-page="${escapeHTML(e.page)}"` : ''}${e.msg ? ` data-msg="${escapeHTML(e.msg)}"` : ''}>${escapeHTML(e.title)}</button>
            <div class="item-meta">${escapeHTML(e.meta)}</div>
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            <span class="${escapeHTML(e.pill)}">${escapeHTML(e.kind)}</span>
            ${typeof e.delIdx === 'number' ? `<button type="button" class="btn" style="padding:4px 8px" title="حذف التذكير" aria-label="حذف التذكير" data-action="remove-personal-reminder" data-index="${Number(e.delIdx)}">×</button>` : ''}
          </div>
        </article>
      `).join('') || '<div class="empty">لا توجد إشعارات أو تنبيهات معلقة حالياً.</div>'}
    </div>
    <p class="footnote">بالإضافة إلى هذا المركز، يرسل البوت إشعارات فورية إلى حسابك في تيليجرام عند نشر أي واجب أو علامة أو إعلان أو ملف جديد.</p>
  `;
}

function openCustomReminderModal(taskId = '') {
  const t = taskId ? tasks.find(x => String(x.id) === String(taskId)) : null;
  openModal(`
    <div class="modal-head">
      <h2 id="modalTitle">إضافة تذكير شخصي</h2>
      <button type="button" class="close" aria-label="إغلاق النافذة" data-action="close-modal">×</button>
    </div>
    <div class="form-grid" style="margin-top:12px">
      <div>
        <label>عنوان التذكير</label>
        <input id="remTitle" class="field" style="width:100%" value="${escapeHTML(t ? t.title : '')}" placeholder="مثال: مراجعة الفصل الرابع أو تسليم التقرير">
      </div>
      <div>
        <label>الموعد أو الملاحظة الزمنية</label>
        <input id="remWhen" class="field" style="width:100%" placeholder="مثال: غداً الساعة ٨ مساءً / قبل الموعد بيوم">
      </div>
      <div class="actions">
        <button class="btn btn-primary" data-action="save-custom-reminder" data-task-id="${escapeHTML(taskId)}" data-course-name="${escapeHTML(t ? t.course_name : '')}">حفظ التذكير</button>
        <button class="btn" data-action="close-modal">إلغاء</button>
      </div>
    </div>
  `);
}

function settingsPage() {
  const prefItems = [
    ['notify_assign', 'الواجبات والمواعيد', 'إشعار فوري عند نشر واجب جديد أو تذكيرات العد التنازلي (٢٤ ساعة، ٣ ساعات، ساعة)'],
    ['notify_quiz', 'الاختبارات القصيرة (Quizzes)', 'إشعار فوري عند إضافة كويز جديد أو اقتراب موعد إغلاقه'],
    ['notify_resource', 'ملفات ومحاضرات المقررات', 'إرسال ملفات المقرر الجديدة مباشرة إلى محادثة تيليجرام'],
    ['notify_grade', 'رادار العلامات الجديدة', 'إشعار فوري عند رصد أو تعديل أي درجة في سجل العلامات'],
    ['notify_announcement', 'إعلانات المقررات', 'إشعار فوري عند نشر إعلان جديد من مدرس المقرر'],
    ['notify_attendance', 'رادار الحضور والغياب', 'إشعار فوري عند تسجيل حالة حضور أو غياب جديدة في LMS']
  ];

  const intervals = [
    [5, 'كل ٥ دقائق'],
    [10, 'كل ١٠ دقائق'],
    [15, 'كل ١٥ دقيقة'],
    [30, 'كل ٣٠ دقيقة'],
    [60, 'كل ساعة'],
    [120, 'كل ساعتين'],
    [360, 'كل ٦ ساعات'],
    [720, 'كل ١٢ ساعة'],
    [1440, 'مرة يومياً (٢٤ ساعة)']
  ];

  return `
    ${header('الإعدادات والحساب', 'تحكم بتفضيلات الإشعارات الفورية في تيليجرام، دورة الفحص التلقائي، وإدارة حسابك الأكاديمي.')}
    <div class="section-head"><h2>تفضيلات إشعارات تيليجرام والمنصة</h2></div>
    <div class="card" style="padding:0">
      ${prefItems.map(([k, t, d]) => `
        <div class="setting">
          <div><strong>${t}</strong><p>${d}</p></div>
          <input class="switch" type="checkbox" ${userProfile[k] ? 'checked' : ''} data-change="set-server-pref" data-key="${escapeHTML(k)}" aria-label="${escapeHTML(t)}">
        </div>
      `).join('')}
    </div>

    <div class="section-head"><h2>المزامنة والفحص التلقائي</h2></div>
    <div class="card" style="padding:0">
      <div class="setting">
        <div>
          <strong>تكرار الفحص التلقائي</strong>
          <p>الفترة الزمنية بين كل دورة فحص آلي لحسابك في LMS (من ٥ دقائق إلى ٢٤ ساعة).</p>
        </div>
        <select class="field" data-change="set-server-interval" aria-label="تكرار الفحص التلقائي">
          ${intervals.map(([n, lbl]) => `<option value="${n}" ${Number(userProfile.check_interval) === n ? 'selected' : ''}>${lbl}</option>`).join('')}
        </select>
      </div>
      <div class="setting">
        <div>
          <strong>تشغيل المراقبة التلقائية النشطة</strong>
          <p>فحص المقررات في الخلفية على مدار الساعة وإرسال تنبيهات تيليجرام.</p>
        </div>
        <input class="switch" type="checkbox" ${userProfile.is_monitoring ? 'checked' : ''} data-change="set-server-monitoring" aria-label="تشغيل المراقبة التلقائية النشطة">
      </div>
      <div style="padding:12px 14px">
        <button class="btn btn-primary" data-action="sync-now">↻ فحص فوري الآن لكافة المقررات</button>
      </div>
    </div>

    <div class="section-head"><h2>حساب الجامعة (Moodle LMS)</h2></div>
    ${isLoggedIn ? `
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
          <div>
            <strong>${escapeHTML(userProfile.full_name)}</strong>
            <div class="item-meta">الرقم الجامعي: ${escapeHTML(userProfile.lms_username)} · آخر فحص: ${escapeHTML(formatFriendlyDateTime(userProfile.last_check, true))}</div>
          </div>
          <span class="pill good">مربوط ومتصل</span>
        </div>
        <div class="callout info">كلمة المرور مشفرة ومحفوظة بأمان لمعالجة جلسات المراقبة التلقائية مع بوابة جامعة مؤتة.</div>
        <div class="actions">
          <button class="btn btn-primary" data-action="open-add-course-modal">＋ إضافة مقرر برقم ID</button>
          <button class="btn" data-action="sync-all-courses">استيراد مقرراتي من LMS</button>
          <button class="btn btn-danger" data-action="logout-account">تسجيل الخروج من الحساب</button>
        </div>
      </div>
    ` : loginBannerCard()}
    ${appDevicePanel()}
    <p class="footnote">جميع التغييرات في هذه الصفحة تُحفظ مباشرة في الخادم وتنعكس فوراً على البوت.</p>
  `;
}

function appDevicePanel() {
  if (isInTelegram()) return '';
  const linked = isLinkedDevice();
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const installed = isStandalone();
  return `
    <div class="section-head"><h2>التطبيق على هذا الهاتف</h2></div>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <div>
          <strong>${installed ? 'التطبيق مثبّت على جهازك' : 'ثبّت دفتر الطالب كتطبيق'}</strong>
          <div class="item-meta">${linked ? 'هذا الجهاز مرتبط بحسابك.' : 'هذا الجهاز غير مرتبط برمز.'} تنبيهات المهام والعلامات والحضور تصلك عبر بوت تيليجرام كما هي.</div>
        </div>
        <span class="pill ${linked ? 'good' : 'warn'}">${linked ? 'جهاز مرتبط' : 'غير مرتبط'}</span>
      </div>
      <div class="actions">
        ${!installed && deferredInstall ? `<button class="btn btn-primary" data-action="install-app">⬇ تثبيت التطبيق</button>` : ''}
        ${linked ? `<button class="btn btn-danger" data-action="unlink-device">فصل هذا الجهاز</button>` : ''}
      </div>
      ${!installed && !deferredInstall ? `<div class="callout info">${ios
        ? 'للتثبيت على آيفون: افتح الصفحة في Safari ← زر المشاركة ← «إضافة إلى الشاشة الرئيسية».'
        : 'للتثبيت على أندرويد: افتح الصفحة في Chrome ← قائمة ⋮ ← «تثبيت التطبيق» أو «إضافة إلى الشاشة الرئيسية».'}</div>` : ''}
    </div>
  `;
}

function helpPage() {
  return `
    ${header('المساعدة وحالة النظام', 'معلومات الاتصال الحية وإجابات سريعة حول مزايا دفتر الطالب الذكي.')}
    <div class="callout info">
      <b>حالة النظام:</b> ${isLoggedIn ? `متصل بحساب الطالب (${escapeHTML(userProfile.lms_username)}) · تتم متابعة ${courses.filter(c => c.is_monitored).length} مقررات دراسية · آخر فحص: ${escapeHTML(formatFriendlyDateTime(userProfile.last_check, true))}.` : 'غير مربوط بحساب جامعي حالياً — انتقل إلى الإعدادات لتسجيل الدخول.'}
    </div>
    <div class="help-grid">
      <article class="help-card">
        <strong>كيف يتم كشف إنجاز الواجبات والكويزات؟</strong>
        <p>يقوم النظام بفحص صفحات الواجبات والاختبارات وسجل الدرجات في LMS تلقائياً؛ وعند تسليمك للواجب أو إنهاء المحاولة يتحول إلى «مكتمل» وتتوقف تذكيرات العد التنازلي فوراً.</p>
      </article>
      <article class="help-card">
        <strong>كيف أضيف الموعد إلى تقويم هاتفي؟</strong>
        <p>افتح تفاصيل أي مهمة ثم اضغط «إضافة للتقويم» لتنزيل ملف تقويم قياسي (.ics) يعمل مع تقويم Apple وGoogle وOutlook.</p>
      </article>
      <article class="help-card">
        <strong>كيف أضيف تذكيراً أو ملاحظة شخصية؟</strong>
        <p>اضغط على عنوان أي مهمة لفتح نافذتها، ثم اكتب ملاحظتك أو أضف تذكيراً شخصياً؛ تُحفظ بياناتك في حسابك على الخادم وعلى جهازك.</p>
      </article>
      <article class="help-card">
        <strong>كيف تعمل مكتبة الملفات؟</strong>
        <p>يرصد النظام كافة ملفات الـ PDF والعروض التقديمية والروابط المرفوعة في صفحات مقرراتك ويعرضها في قسم «الملفات» مع إرسالها في تيليجرام.</p>
      </article>
      <article class="help-card">
        <strong>ماذا يعني حد التنبيه في الحضور؟</strong>
        <p>نسبة مئوية تحددها بنفسك (مثلاً ٢٠٪) لينبهك الدفتر باللون الأحمر عند اقتراب غيابك في أي مقرر من هذا الحد.</p>
      </article>
      <article class="help-card">
        <strong>كيف يعمل الفحص الفوري (↻)؟</strong>
        <p>عند الضغط على زر المزامنة في الأعلى، يتصل الخادم ببوابة جامعة مؤتة ويفحص كافة مقرراتك بالتوازي خلال ثوانٍ معدودة.</p>
      </article>
    </div>
    <div class="section-head">
      <h2>سجل عمليات المزامنة</h2>
      <button data-action="go" data-page="settings">الإعدادات ←</button>
    </div>
    <div class="generic-list">
      ${syncLogs.slice(0, 8).map(l => `
        <div class="generic-row">
          <div>
            <strong class="item-title">${escapeHTML(l.status)}</strong>
            <div class="item-meta">${escapeHTML(formatFriendlyDateTime(l.time, false))} · ${escapeHTML(l.detail)}</div>
          </div>
          <span class="pill good">متزامن</span>
        </div>
      `).join('') || '<div class="empty">لا توجد سجلات مزامنة بعد. اضغط على زر المزامنة (↻) لإجراء فحص فوري.</div>'}
    </div>
  `;
}

function taskDetail(id) {
  const t = tasks.find(x => String(x.id) === String(id));
  if (!t) return;
  const c = course(t.course);
  const note = notes[t.id] || '';
  const done = isTaskDone(t);
  const [statusLbl, statusCls] = dueInfo(t);
  const pillMod = statusCls === 'pill red' ? 'red' : statusCls === 'pill warn' ? 'warn' : statusCls === 'pill good' ? 'good' : '';

  openModal(`
    <div class="modal-head">
      <div>
        <span class="pill ${pillMod}">${escapeHTML(t.type)}</span>
        <h2 id="modalTitle" style="margin-top:8px">${escapeHTML(t.title)}</h2>
      </div>
      <button type="button" class="close" aria-label="إغلاق النافذة" data-action="close-modal">×</button>
    </div>
    <p class="item-meta">${escapeHTML(t.course_name)} · ${escapeHTML(c.teacher)}</p>
    <div class="callout">
      ${escapeHTML(t.desc)}<br>
      <b>الموعد:</b> ${escapeHTML(fmtDate(t.dueDateObj, t.due_str))} · <b>الحالة:</b> ${escapeHTML(statusLbl)}
      ${t.grade ? `<br><b>الدرجة المرصودة:</b> ${escapeHTML(t.grade.grade_val)}${t.grade.grade_max ? ' / ' + escapeHTML(t.grade.grade_max) : ''}` : ''}
    </div>
    <div class="actions">
      <button class="btn btn-primary" data-action="open-link" data-url="${escapeHTML(t.url)}">فتح المهمة في LMS ↗</button>
      <button class="btn" data-action="open-custom-reminder" data-id="${escapeHTML(t.id)}">＋ تذكير شخصي</button>
      <button class="btn" data-action="download-ics" data-id="${escapeHTML(t.id)}">إضافة للتقويم (.ics)</button>
      <button class="btn" data-action="toggle-done" data-id="${escapeHTML(t.id)}" data-reopen="1">${done ? 'إلغاء الإنجاز' : 'تسجيل إنجاز ✓'}</button>
    </div>
    <div class="section-head"><h2>ملاحظتك الشخصية</h2></div>
    <textarea id="taskNote" class="field" style="width:100%;min-height:80px" placeholder="اكتب ملاحظة أو خطوة تالية حول هذا الواجب أو الاختبار...">${escapeHTML(note)}</textarea>
    <div class="actions">
      <button class="btn btn-primary" data-action="save-note" data-id="${escapeHTML(t.id)}">حفظ الملاحظة</button>
      <span class="tag">تُحفظ في حسابك وعلى هذا الجهاز</span>
    </div>
  `);
}

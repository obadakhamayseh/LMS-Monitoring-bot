/* Arabic/English date parsing, due countdown calculations, and week/semester helpers */

const _DATE_MONTH_MAP = {
  jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
  jul: 7, aug: 8, sep: 9, sept: 9, oct: 10, nov: 11, dec: 12,
  january: 1, february: 2, march: 3, april: 4,
  june: 6, july: 7, august: 8, september: 9,
  october: 10, november: 11, december: 12,
  'يناير': 1, 'فبراير': 2, 'مارس': 3, 'أبريل': 4, 'إبريل': 4, 'ابريل': 4,
  'مايو': 5, 'يونيو': 6, 'يوليو': 7, 'أغسطس': 8, 'اغسطس': 8,
  'سبتمبر': 9, 'أكتوبر': 10, 'اكتوبر': 10, 'نوفمبر': 11, 'ديسمبر': 12,
  'كانون الثاني': 1, 'كانون ثاني': 1,
  'شباط': 2,
  'آذار': 3, 'أذار': 3, 'اذار': 3,
  'نيسان': 4,
  'أيار': 5, 'ايار': 5,
  'حزيران': 6,
  'تموز': 7,
  'آب': 8, 'أب': 8, 'اب': 8,
  'أيلول': 9, 'إيلول': 9, 'ايلول': 9,
  'تشرين الأول': 10, 'تشرين الاول': 10, 'تشرين أول': 10, 'تشرين اول': 10,
  'تشرين الثاني': 11, 'تشرين ثاني': 11,
  'كانون الأول': 12, 'كانون الاول': 12, 'كانون أول': 12, 'كانون اول': 12
};

const _MONTH_ALT = Object.keys(_DATE_MONTH_MAP)
  .sort((a, b) => b.length - a.length)
  .map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  .join('|');

const _NAMED_DATE_RE = new RegExp(
  `(?:^|[^\\d])(\\d{1,2})\\s+(${_MONTH_ALT})\\.?(?:\\s+(\\d{4}))?(?:[,،\\s]+(\\d{1,2}):(\\d{2})(?::(\\d{2}))?(?:\\s*(صباحاً|صباحا|مساءً|مساءا|مساء|a\\.?m\\.?|p\\.?m\\.?|ص|م))?)?`,
  'i'
);

function _normalizeDigits(str) {
  return String(str)
    .replace(/[٠-٩]/g, ch => String(ch.charCodeAt(0) - 0x0660))
    .replace(/[۰-۹]/g, ch => String(ch.charCodeAt(0) - 0x06F0));
}

function _nearestYearDate(month, day, hour, minute, second, refDate) {
  const ref = (refDate && typeof refDate.getTime === 'function' && !isNaN(refDate.getTime())) ? refDate : new Date(Date.now());
  const baseYear = ref.getFullYear();
  let best = null;
  let bestDiff = Infinity;
  for (const y of [baseYear - 1, baseYear, baseYear + 1]) {
    const cand = new Date(y, month - 1, day, hour, minute, second);
    if (isNaN(cand.getTime())) continue;
    if (cand.getMonth() !== month - 1 || cand.getDate() !== day) continue;
    const diff = Math.abs(cand.getTime() - ref.getTime());
    if (diff < bestDiff) {
      bestDiff = diff;
      best = cand;
    }
  }
  return best;
}

function parseAnyDate(val, tsFallback, nowOverride) {
  if (tsFallback && Number(tsFallback) > 0) return new Date(Number(tsFallback) * 1000);
  if (!val) return null;
  if (val instanceof Date || (typeof val === 'object' && typeof val.getTime === 'function')) {
    return isNaN(val.getTime()) ? null : new Date(val.getTime());
  }
  if (typeof val === 'number') return new Date(val > 1e11 ? val : val * 1000);
  let s = _normalizeDigits(String(val)).replace(/\s+/g, ' ').trim();
  if (!s || s === 'لم يتم بعد' || s === 'حديثاً' || s === 'مرصود' || s === '—') return null;
  s = s.replace(/^(?:Due|Closes|Closed|Opened|Opens|تاريخ الاستحقاق|يغلق|مغلق|يفتح)\s*[:：]\s*/i, '').trim();

  // 1. Explicit ISO / SQL YYYY-MM-DD[ T]HH:MM(:SS)? — Safari-safe (never passes space-separated string to new Date())
  const isoM = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?)?$/i);
  if (isoM) {
    if (isoM[8]) {
      const normalizedIso = s.includes(' ') ? s.replace(' ', 'T') : s;
      const dz = new Date(normalizedIso);
      if (!isNaN(dz.getTime())) return dz;
    }
    const y = Number(isoM[1]);
    const mo = Number(isoM[2]);
    const da = Number(isoM[3]);
    const hh = isoM[4] !== undefined ? Number(isoM[4]) : 0;
    const mm = isoM[5] !== undefined ? Number(isoM[5]) : 0;
    const ss = isoM[6] !== undefined ? Number(isoM[6]) : 0;
    const dLocal = new Date(y, mo - 1, da, hh, mm, ss);
    if (!isNaN(dLocal.getTime()) && dLocal.getMonth() === mo - 1 && dLocal.getDate() === da) return dLocal;
    return null;
  }

  // 2. Explicit DD/MM/YYYY [HH:MM]
  const slashM = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:[,،\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$/);
  if (slashM) {
    const da = Number(slashM[1]);
    const mo = Number(slashM[2]);
    const y = Number(slashM[3]);
    const hh = slashM[4] !== undefined ? Number(slashM[4]) : 23;
    const mm = slashM[5] !== undefined ? Number(slashM[5]) : 59;
    const ss = slashM[6] !== undefined ? Number(slashM[6]) : 0;
    const dSlash = new Date(y, mo - 1, da, hh, mm, ss);
    if (!isNaN(dSlash.getTime()) && dSlash.getMonth() === mo - 1 && dSlash.getDate() === da) return dSlash;
  }

  // 3. Named month dates (English + Arabic Gregorian + Arabic Levantine compound months, with optional year & AM/PM)
  const nm = s.match(_NAMED_DATE_RE);
  if (nm) {
    const day = Number(nm[1]);
    const monthKey = nm[2].replace(/\s+/g, ' ').trim().toLowerCase();
    const month = _DATE_MONTH_MAP[monthKey];
    if (month) {
      let hour = nm[4] !== undefined ? Number(nm[4]) : 23;
      const minute = nm[5] !== undefined ? Number(nm[5]) : 59;
      const second = nm[6] !== undefined ? Number(nm[6]) : 0;
      const ampm = (nm[7] || '').replace(/\./g, '').trim().toLowerCase();
      const isPm = ampm === 'pm' || ampm === 'م' || ampm === 'مساءً' || ampm === 'مساءا' || ampm === 'مساء';
      const isAm = ampm === 'am' || ampm === 'ص' || ampm === 'صباحاً' || ampm === 'صباحا';
      if (isPm && hour >= 1 && hour < 12) hour += 12;
      else if (isAm && hour === 12) hour = 0;

      if (nm[3]) {
        const year = Number(nm[3]);
        const dNamed = new Date(year, month - 1, day, hour, minute, second);
        if (!isNaN(dNamed.getTime()) && dNamed.getMonth() === month - 1 && dNamed.getDate() === day) return dNamed;
      } else {
        const dNearest = _nearestYearDate(month, day, hour, minute, second, nowOverride);
        if (dNearest) return dNearest;
      }
    }
  }

  // 4. Fallback to standard parser (only for formats not already handled above)
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}
const parseLmsDate = parseAnyDate;

function formatRelativeTime(val) {
  const d = parseAnyDate(val);
  if (!d) return 'لم يتم بعد';
  const now = Date.now();
  const diffSec = Math.round((now - d.getTime()) / 1000);
  if (diffSec < 45) return 'الآن';
  const diffMin = Math.round(diffSec / 60);
  if (diffMin <= 1) return 'منذ دقيقة';
  if (diffMin === 2) return 'منذ دقيقتين';
  if (diffMin <= 10) return `منذ ${diffMin} دقائق`;
  if (diffMin < 60) return `منذ ${diffMin} دقيقة`;
  const diffHours = Math.round(diffMin / 60);
  if (diffHours <= 1) return 'منذ ساعة';
  if (diffHours === 2) return 'منذ ساعتين';
  if (diffHours <= 10) return `منذ ${diffHours} ساعات`;
  if (diffHours < 24) return `منذ ${diffHours} ساعة`;
  const diffDays = Math.round(diffHours / 24);
  if (diffDays <= 1) return 'أمس';
  if (diffDays === 2) return 'منذ يومين';
  if (diffDays <= 10) return `منذ ${diffDays} أيام`;
  return d.toLocaleDateString('ar-JO-u-nu-latn', { day: 'numeric', month: 'short', year: 'numeric' });
}

function formatFriendlyDateTime(val, withRelative = false) {
  const d = parseAnyDate(val);
  if (!d) return typeof val === 'string' && val.trim() ? val : '—';
  const now = new Date();
  const isToday = d.toDateString() === now.toDateString();
  const yest = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  const isYesterday = d.toDateString() === yest.toDateString();
  const tom = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  const isTomorrow = d.toDateString() === tom.toDateString();
  const timeStr = d.toLocaleTimeString('ar-JO-u-nu-latn', { hour: 'numeric', minute: '2-digit', hour12: true });

  let prefix = '';
  if (isToday) prefix = 'اليوم';
  else if (isYesterday) prefix = 'أمس';
  else if (isTomorrow) prefix = 'غداً';
  else prefix = d.toLocaleDateString('ar-JO-u-nu-latn', { weekday: 'short', day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' });

  const res = `${prefix}، ${timeStr}`;
  if (withRelative) {
    const rel = formatRelativeTime(d);
    if (rel && rel !== 'الآن' && !rel.startsWith('اليوم') && !rel.startsWith('أمس')) {
      return `${res} (${rel})`;
    }
  }
  return res;
}

function formatDateOnly(val) {
  const d = parseAnyDate(val);
  if (!d) return typeof val === 'string' && val.trim() ? val : 'مرصود';
  return d.toLocaleDateString('ar-JO-u-nu-latn', { day: 'numeric', month: 'long', year: 'numeric' });
}

function fmtDate(dInput, fallbackStr) {
  if (!dInput && !fallbackStr) return 'بدون موعد محدد';
  const d = parseAnyDate(dInput);
  if (d) return formatFriendlyDateTime(d);
  return fallbackStr || 'بدون موعد محدد';
}

function dueInfo(t) {
  if (isTaskDone(t)) return ['مكتمل في LMS', 'pill good'];
  if (!t.dueDateObj) return ['قيد الحل', 'pill warn'];
  const nowMs = Date.now();
  const dueMs = t.dueDateObj.getTime();
  const diff = dueMs - nowMs;
  if (diff < 0) return ['متأخر / مغلق', 'pill red'];

  const diffHours = diff / 3600000;
  if (diffHours < 1) {
    const mins = Math.max(1, Math.round(diff / 60000));
    return [`قريب · خلال ${mins} دقيقة`, 'pill warn'];
  }
  if (diffHours < 24) {
    const hrs = Math.floor(diffHours);
    const nowDate = new Date(nowMs);
    const dueDate = new Date(dueMs);
    const sameCalDay = nowDate.getFullYear() === dueDate.getFullYear()
      && nowDate.getMonth() === dueDate.getMonth()
      && nowDate.getDate() === dueDate.getDate();
    if (sameCalDay) {
      return [`قريب · اليوم (${hrs} س)`, 'pill warn'];
    }
    return [`قريب · خلال ${hrs} ساعة`, 'pill warn'];
  }

  const nowStart = new Date(nowMs);
  nowStart.setHours(0, 0, 0, 0);
  const dueStart = new Date(dueMs);
  dueStart.setHours(0, 0, 0, 0);
  const calDays = Math.round((dueStart.getTime() - nowStart.getTime()) / 86400000);
  if (calDays <= 1) return ['قريب · غداً', 'pill warn'];
  if (calDays === 2) return ['قريب · خلال يومين', 'pill warn'];
  return [t.time_remaining ? `متبقي ${t.time_remaining}` : `خلال ${calDays} أيام`, 'pill'];
}

function getWeekBounds(offset) {
  const now = new Date();
  const dayOfWeek = now.getDay(); // 0 = Sunday .. 6 = Saturday
  const sunday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - dayOfWeek + (offset * 7));
  const saturday = new Date(sunday.getFullYear(), sunday.getMonth(), sunday.getDate() + 6);
  return { sunday, saturday };
}

function getCurrentSemesterLabel(now = new Date()) {
  const d = (now && typeof now.getTime === 'function' && !isNaN(now.getTime())) ? now : new Date();
  const y = d.getFullYear();
  const m = d.getMonth(); // 0 = Jan .. 11 = Dec
  if (m >= 8) return `الفصل الأول ${y}/${y + 1}`;
  if (m === 0) return `الفصل الأول ${y - 1}/${y}`;
  if (m >= 1 && m <= 5) return `الفصل الثاني ${y - 1}/${y}`;
  return `الفصل الصيفي ${y - 1}/${y}`;
}

function escapeICSText(text) {
  return String(text ?? '')
    .replace(/\\/g, '\\\\')
    .replace(/\r\n|\r|\n/g, '\\n')
    .replace(/;/g, '\\;')
    .replace(/,/g, '\\,');
}

function formatICSDateUTC(d) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}T${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`;
}

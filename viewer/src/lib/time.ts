// 時刻の扱い。API はすべて UTC、表示はすべて日本時間(JST、UTC+9、夏時間なし)。

export const SECOND = 1000;
export const MINUTE = 60 * SECOND;
export const HOUR = 60 * MINUTE;
export const DAY = 24 * HOUR;
const JST_OFFSET = 9 * HOUR;

export const parseTime = (iso: string): number => Date.parse(iso);

/** JST の日付文字列(YYYY-MM-DD) */
export function jstDate(ms: number): string {
  return new Date(ms + JST_OFFSET).toISOString().slice(0, 10);
}

/** JST の日付(YYYY-MM-DD)の 0 時を epoch ミリ秒で */
export function jstDayStart(date: string): number {
  return Date.parse(`${date}T00:00:00+09:00`);
}

/** その時刻を含む JST の日の 0 時 */
export function jstDayStartOf(ms: number): number {
  return jstDayStart(jstDate(ms));
}

export function addDays(date: string, days: number): string {
  return jstDate(jstDayStart(date) + days * DAY);
}

export const floorTo = (ms: number, step: number) => Math.floor(ms / step) * step;

const pad = (n: number) => String(n).padStart(2, '0');

function jstParts(ms: number) {
  const d = new Date(ms + JST_OFFSET);
  return {
    y: d.getUTCFullYear(),
    mo: d.getUTCMonth() + 1,
    d: d.getUTCDate(),
    h: d.getUTCHours(),
    mi: d.getUTCMinutes(),
    s: d.getUTCSeconds(),
    dow: d.getUTCDay(),
  };
}

/** 10:20 */
export function formatHm(ms: number): string {
  const p = jstParts(ms);
  return `${pad(p.h)}:${pad(p.mi)}`;
}

/** 10:20:05 */
export function formatHms(ms: number): string {
  const p = jstParts(ms);
  return `${pad(p.h)}:${pad(p.mi)}:${pad(p.s)}`;
}

const DOW = ['日', '月', '火', '水', '木', '金', '土'];

/** 10/24(土) */
export function formatMd(ms: number): string {
  const p = jstParts(ms);
  return `${p.mo}/${p.d}(${DOW[p.dow]})`;
}

/** 2026/10/24(土) 10:20:05 */
export function formatDateTime(ms: number): string {
  const p = jstParts(ms);
  return `${p.y}/${pad(p.mo)}/${pad(p.d)}(${DOW[p.dow]}) ${formatHms(ms)}`;
}

/** 経過時間を「3 分」「1 時間 5 分」のように */
export function formatDuration(ms: number): string {
  const m = Math.max(0, Math.round(ms / MINUTE));
  if (m < 1) return '1 分未満';
  if (m < 60) return `${m} 分`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest ? `${h} 時間 ${rest} 分` : `${h} 時間`;
}

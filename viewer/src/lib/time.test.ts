import { describe, expect, it } from 'vitest';
import { addDays, formatDateTime, formatDuration, jstDate, jstDayStart } from './time';

describe('日本時間', () => {
  it('UTC 15 時以降は JST の翌日', () => {
    expect(jstDate(Date.parse('2026-10-24T14:59:59Z'))).toBe('2026-10-24');
    expect(jstDate(Date.parse('2026-10-24T15:00:00Z'))).toBe('2026-10-25');
  });
  it('JST の 0 時は UTC の前日 15 時', () => {
    expect(new Date(jstDayStart('2026-10-24')).toISOString()).toBe('2026-10-23T15:00:00.000Z');
  });
  it('日付の加減は月をまたぐ', () => {
    expect(addDays('2026-10-31', 1)).toBe('2026-11-01');
    expect(addDays('2026-11-01', -1)).toBe('2026-10-31');
  });
  it('表示', () => {
    expect(formatDateTime(Date.parse('2026-10-24T01:02:03Z'))).toBe('2026/10/24(土) 10:02:03');
    expect(formatDuration(90 * 60_000)).toBe('1 時間 30 分');
  });
});

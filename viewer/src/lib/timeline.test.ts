import { describe, expect, it } from 'vitest';
import {
  layoutBars,
  makeTicks,
  mergeIntervals,
  nextRecordingStart,
  prevRecordingStart,
  ratioToTime,
  snapToRecording,
  timeToRatio,
  toTimelineData,
  viewAround,
} from './timeline';
import { HOUR, jstDayStart, MINUTE } from './time';

const day0 = jstDayStart('2026-10-24'); // 2026-10-23T15:00:00Z
const day = { start: day0, end: day0 + 24 * HOUR };

describe('時刻と位置の変換', () => {
  it('往復で元に戻る', () => {
    const t = day0 + 6 * HOUR;
    expect(timeToRatio(day, t)).toBeCloseTo(0.25);
    expect(ratioToTime(day, 0.25)).toBe(t);
  });
  it('範囲外の比率は端に丸める', () => {
    expect(ratioToTime(day, -0.2)).toBe(day.start);
    expect(ratioToTime(day, 1.5)).toBe(day.end);
  });
});

describe('layoutBars', () => {
  it('% の帯にし、表示範囲からはみ出す部分は切る', () => {
    const bars = layoutBars(
      [
        { start: day0 - HOUR, end: day0 + 6 * HOUR },
        { start: day0 + 12 * HOUR, end: day0 + 18 * HOUR },
        { start: day0 + 30 * HOUR, end: day0 + 31 * HOUR },
      ],
      's3',
      day,
    );
    expect(bars).toHaveLength(2);
    expect(bars[0]).toEqual({ kind: 's3', left: 0, width: 25 });
    expect(bars[1].left).toBeCloseTo(50);
    expect(bars[1].width).toBeCloseTo(25);
  });

  it('細すぎる帯は最小幅まで太らせる', () => {
    const [bar] = layoutBars([{ start: day0, end: day0 + MINUTE }], 'failed', day, 0.3);
    expect(bar.width).toBe(0.3);
  });
});

describe('目盛り', () => {
  it('1 日表示は JST の 0, 3, 6 ... 24 時', () => {
    const ticks = makeTicks(day);
    expect(ticks.map((t) => t.label)).toEqual([
      '00:00', '03:00', '06:00', '09:00', '12:00', '15:00', '18:00', '21:00', '00:00',
    ]);
    expect(ticks[0].left).toBe(0);
    expect(ticks[8].left).toBe(100);
  });
  it('1 時間表示は 10 分おき', () => {
    const view = { start: day0 + 10 * HOUR + 5 * MINUTE, end: day0 + 11 * HOUR + 5 * MINUTE };
    expect(makeTicks(view).map((t) => t.label)).toEqual([
      '10:10', '10:20', '10:30', '10:40', '10:50', '11:00',
    ]);
  });
});

describe('ズームの表示範囲', () => {
  it('中心に置き、1 日の端では内側に寄せる', () => {
    expect(viewAround(day0 + 12 * HOUR, HOUR, day)).toEqual({
      start: day0 + 11.5 * HOUR,
      end: day0 + 12.5 * HOUR,
    });
    expect(viewAround(day0 + 10 * MINUTE, HOUR, day)).toEqual({ start: day0, end: day0 + HOUR });
    expect(viewAround(day.end, HOUR, day)).toEqual({ start: day.end - HOUR, end: day.end });
  });
});

describe('前後の録画へのジャンプ', () => {
  const merged = mergeIntervals(
    [
      { start: 0, end: 10_000 },
      { start: 10_500, end: 20_000 }, // 0.5 秒の隙間はつなぐ
      { start: 60_000, end: 90_000 },
      { start: 120_000, end: 150_000 },
    ],
    1000,
  );

  it('隙間が許容値以下ならつなぐ', () => {
    expect(merged).toEqual([
      { start: 0, end: 20_000 },
      { start: 60_000, end: 90_000 },
      { start: 120_000, end: 150_000 },
    ]);
  });
  it('次へ: 今より後に始まる録画の頭', () => {
    expect(nextRecordingStart(merged, 5_000)).toBe(60_000);
    expect(nextRecordingStart(merged, 60_000)).toBe(120_000);
    expect(nextRecordingStart(merged, 130_000)).toBeNull();
  });
  it('前へ: 頭から進んでいれば今の録画の頭、頭付近なら 1 つ前', () => {
    expect(prevRecordingStart(merged, 80_000)).toBe(60_000);
    expect(prevRecordingStart(merged, 61_000)).toBe(0);
    expect(prevRecordingStart(merged, 100_000)).toBe(60_000);
    expect(prevRecordingStart(merged, 1_000)).toBeNull();
  });
  it('録画がない時刻は次の録画に寄せる', () => {
    expect(snapToRecording(merged, 15_000)).toBe(15_000);
    expect(snapToRecording(merged, 30_000)).toBe(60_000);
    expect(snapToRecording(merged, 200_000)).toBeNull();
  });
});

describe('toTimelineData', () => {
  it('ISO 文字列を数値にし、S3 と KVS の区間をつなぐ', () => {
    const data = toTimelineData({
      camera_id: 'cam-001',
      from: '2026-10-23T15:00:00Z',
      to: '2026-10-24T15:00:00Z',
      video_processed_until: '2026-10-24T10:25:00Z',
      detect_processed_until: null,
      recorded: [
        { start: '2026-10-24T10:00:00Z', end: '2026-10-24T10:25:00Z', source: 's3' },
        { start: '2026-10-24T10:25:00Z', end: '2026-10-24T10:29:00Z', source: 'kvs' },
      ],
      failed: [{ start: '2026-10-24T09:00:00Z', end: '2026-10-24T09:01:00Z' }],
      minutes: [],
    });
    expect(data.merged).toEqual([
      { start: Date.parse('2026-10-24T10:00:00Z'), end: Date.parse('2026-10-24T10:29:00Z') },
    ]);
    expect(data.videoProcessedUntil).toBe(Date.parse('2026-10-24T10:25:00Z'));
  });
});

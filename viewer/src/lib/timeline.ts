// シークバーの描画計算(DOM に依存しない純粋な関数)

import type { TimelineResponse } from '../api/types';
import { formatHm, HOUR, MINUTE, parseTime } from './time';

export interface Interval {
  start: number;
  end: number;
}

export type BarKind = 's3' | 'kvs' | 'failed';

export interface TimelineData {
  recorded: (Interval & { source: 's3' | 'kvs' })[];
  failed: Interval[];
  /** 録画区間を種類に関係なくつないだもの(ジャンプ用) */
  merged: Interval[];
  videoProcessedUntil: number | null;
  minutes: { start: number; sprite: string }[];
}

export interface View {
  start: number;
  end: number;
}

export interface Bar {
  kind: BarKind;
  /** 0〜100 の % */
  left: number;
  width: number;
}

export interface Tick {
  at: number;
  /** 0〜100 の % */
  left: number;
  label: string;
}

/** ズーム段階。名前と表示する幅 */
export const ZOOMS = [
  { value: '24h', label: '1日', span: 24 * HOUR },
  { value: '6h', label: '6時間', span: 6 * HOUR },
  { value: '1h', label: '1時間', span: HOUR },
] as const;
export type ZoomValue = (typeof ZOOMS)[number]['value'];

export function zoomSpan(zoom: ZoomValue): number {
  return ZOOMS.find((z) => z.value === zoom)!.span;
}

/** 開始順に並べ、重なりと隙間 tolerance 以下をつなげる */
export function mergeIntervals(list: Interval[], tolerance = 0): Interval[] {
  const sorted = [...list].filter((i) => i.end > i.start).sort((a, b) => a.start - b.start);
  const out: Interval[] = [];
  for (const i of sorted) {
    const last = out[out.length - 1];
    if (last && i.start <= last.end + tolerance) {
      last.end = Math.max(last.end, i.end);
    } else {
      out.push({ start: i.start, end: i.end });
    }
  }
  return out;
}

/** API のレスポンスを数値に直す */
export function toTimelineData(res: TimelineResponse): TimelineData {
  const recorded = res.recorded.map((r) => ({
    start: parseTime(r.start),
    end: parseTime(r.end),
    source: r.source,
  }));
  return {
    recorded,
    failed: res.failed.map((f) => ({ start: parseTime(f.start), end: parseTime(f.end) })),
    merged: mergeIntervals(recorded, 1000),
    videoProcessedUntil: res.video_processed_until ? parseTime(res.video_processed_until) : null,
    minutes: res.minutes
      .map((m) => ({ start: parseTime(m.start), sprite: m.sprite }))
      .sort((a, b) => a.start - b.start),
  };
}

export const timeToRatio = (view: View, t: number) => (t - view.start) / (view.end - view.start);

export function ratioToTime(view: View, ratio: number): number {
  const r = Math.min(1, Math.max(0, ratio));
  return view.start + r * (view.end - view.start);
}

/**
 * 区間を表示範囲に合わせて % の帯にする。範囲外は捨て、はみ出しは切る。
 * 細すぎて見えない帯(1 分の FAILED を 1 日表示したときなど)は minWidth(%)まで太らせる。
 */
export function layoutBars(
  intervals: Interval[],
  kind: BarKind,
  view: View,
  minWidth = 0,
): Bar[] {
  const span = view.end - view.start;
  const bars: Bar[] = [];
  for (const i of intervals) {
    const s = Math.max(i.start, view.start);
    const e = Math.min(i.end, view.end);
    if (e <= s) continue;
    const left = ((s - view.start) / span) * 100;
    let width = ((e - s) / span) * 100;
    if (width < minWidth) width = Math.min(minWidth, 100 - left);
    bars.push({ kind, left, width });
  }
  return bars;
}

/** 表示幅に応じた目盛りの間隔 */
export function tickStep(span: number, narrow = false): number {
  if (span > 12 * HOUR) return narrow ? 6 * HOUR : 3 * HOUR;
  if (span > 2 * HOUR) return narrow ? 2 * HOUR : HOUR;
  return narrow ? 20 * MINUTE : 10 * MINUTE;
}

/** 目盛り。JST の時刻の切りのよいところに置く(JST は UTC+9 の整数時間なので UTC で丸めてよい) */
export function makeTicks(view: View, narrow = false): Tick[] {
  const step = tickStep(view.end - view.start, narrow);
  const ticks: Tick[] = [];
  for (let t = Math.ceil(view.start / step) * step; t <= view.end; t += step) {
    const left = timeToRatio(view, t) * 100;
    ticks.push({ at: t, left, label: formatHm(t) });
  }
  return ticks;
}

/**
 * ズームしたときの表示範囲。中心を center に置き、1 日の範囲(day)からはみ出さないように寄せる。
 */
export function viewAround(center: number, span: number, day: View): View {
  const daySpan = day.end - day.start;
  if (span >= daySpan) return { ...day };
  let start = center - span / 2;
  start = Math.max(day.start, Math.min(start, day.end - span));
  return { start, end: start + span };
}

/** その時刻に録画があるか */
export function findInterval(list: Interval[], t: number): Interval | null {
  return list.find((i) => t >= i.start && t < i.end) ?? null;
}

/** t より後に始まる最初の録画の開始 */
export function nextRecordingStart(merged: Interval[], t: number): number | null {
  const next = merged.find((i) => i.start > t);
  return next ? next.start : null;
}

/**
 * 前の録画の開始。今いる録画の頭から GRACE 以上進んでいればその頭へ、
 * そうでなければ 1 つ前の録画の頭へ(音楽プレイヤーの「前へ」と同じ振る舞い)。
 */
export function prevRecordingStart(merged: Interval[], t: number, grace = 3000): number | null {
  const current = merged.find((i) => t >= i.start && t < i.end);
  if (current && t - current.start > grace) return current.start;
  const before = merged.filter((i) => i.end <= (current ? current.start : t) && i.start < t);
  return before.length ? before[before.length - 1].start : null;
}

/** 録画がない時刻を要求されたら、次の録画の頭に寄せる。なければ null */
export function snapToRecording(merged: Interval[], t: number): number | null {
  if (findInterval(merged, t)) return t;
  return nextRecordingStart(merged, t);
}

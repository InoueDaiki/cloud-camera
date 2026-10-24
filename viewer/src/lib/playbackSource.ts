// 再生元の判定(構想.md「再生」節)
//
// - ライブ: KVS の HLS(LIVE)
// - 処理済み位置(video_processed_until)以後: KVS の HLS(ON_DEMAND)。S3 へ退避する前の直近の録画
// - 処理済み位置より前: S3 の vod.m3u8
//
// ユーザーには切り替えを意識させないため、プレイヤーは「絶対時刻」で位置を扱い、
// 再生窓の終わりに来たら次の窓(または次の再生元)を自動で読み込む。

import { floorTo, HOUR, MINUTE } from './time';

export type SourceKind = 'live' | 'kvs' | 's3';

/** 再生の要求。ライブか、ある時刻からの再生か */
export type PlaybackTarget = { kind: 'live' } | { kind: 'time'; at: number };

/** ライブとみなす「現在からの近さ」。これより現在に近い時刻を要求されたらライブにする */
export const LIVE_THRESHOLD_MS = 15_000;

/** S3 の再生窓の長さ。1 本のプレイリストに入れる範囲 */
export const S3_WINDOW_MS = HOUR;

export function decideSource(
  target: PlaybackTarget,
  videoProcessedUntil: number | null,
  now: number,
): SourceKind {
  if (target.kind === 'live') return 'live';
  if (target.at >= now - LIVE_THRESHOLD_MS) return 'live';
  if (videoProcessedUntil !== null && target.at < videoProcessedUntil) return 's3';
  return 'kvs';
}

export interface PlaybackWindow {
  kind: SourceKind;
  /** 窓の始まり(ライブなら null) */
  from: number | null;
  /** 窓の終わり。ここまで再生したら次の窓へ進む(ライブなら null) */
  to: number | null;
}

/**
 * ある時刻を再生するための窓を決める。
 * - S3: 時刻を含む 1 時間(時の境界で切る)。ただし処理済み位置で打ち切る
 * - KVS: 時刻を含む分の始まりから現在まで
 */
export function playbackWindow(
  target: PlaybackTarget,
  videoProcessedUntil: number | null,
  now: number,
): PlaybackWindow {
  const kind = decideSource(target, videoProcessedUntil, now);
  if (kind === 'live' || target.kind === 'live') return { kind: 'live', from: null, to: null };
  if (kind === 's3') {
    const from = floorTo(target.at, S3_WINDOW_MS);
    const to = Math.min(from + S3_WINDOW_MS, videoProcessedUntil ?? from + S3_WINDOW_MS);
    return { kind, from, to };
  }
  const from = Math.max(floorTo(target.at, MINUTE), videoProcessedUntil ?? 0);
  return { kind, from: Math.min(from, target.at), to: now };
}

/** 読み込み済みの窓のまま、その時刻へシークできるか */
export function windowCovers(win: PlaybackWindow, kind: SourceKind, at: number): boolean {
  if (win.kind !== kind || kind === 'live') return false;
  return win.from !== null && win.to !== null && at >= win.from && at < win.to;
}

export const SOURCE_LABEL: Record<SourceKind, string> = {
  live: 'ライブ',
  kvs: '直近の録画(KVS)',
  s3: '録画(S3)',
};

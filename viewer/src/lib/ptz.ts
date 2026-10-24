// PTZ 操作の純粋なロジック(押している方向 → 速度、キー割り当て、送信の順番待ち、表示用の計算)。
// 座標は ONVIF の Generic 空間: pan / tilt は -1〜1(tilt は上が +)、zoom は 0〜1。速度は -1〜1。

import type { PtzCommand } from '../api/types';

export type PtzDir = 'up' | 'down' | 'left' | 'right' | 'in' | 'out';

/** continuous の速度(押している間の一定速度) */
export const PAN_TILT_SPEED = 0.5;
export const ZOOM_SPEED = 0.5;
/** continuous の自動停止。stop を取りこぼしても、この時間で止まる */
export const CONTINUOUS_TIMEOUT_MS = 4000;
/** 押し続けている間、自動停止の前に continuous を送り直す間隔 */
export const CONTINUOUS_REFRESH_MS = 2500;
/** これより短い押下はタップとみなし、一定量(STEP)だけ動かす */
export const HOLD_DELAY_MS = 250;
/** タップ 1 回の動き(pan / tilt は端から端まで 2、zoom は 0〜1) */
export const PAN_TILT_STEP = 0.1;
export const ZOOM_STEP = 0.1;

export interface Velocity {
  pan: number;
  tilt: number;
  zoom: number;
}

/** 押している方向の集合から速度を作る(上と下を同時に押したら打ち消す) */
export function velocityOf(held: Iterable<PtzDir>): Velocity {
  const s = new Set(held);
  const axis = (pos: PtzDir, neg: PtzDir) => (s.has(pos) ? 1 : 0) - (s.has(neg) ? 1 : 0);
  return {
    pan: axis('right', 'left') * PAN_TILT_SPEED,
    tilt: axis('up', 'down') * PAN_TILT_SPEED,
    zoom: axis('in', 'out') * ZOOM_SPEED,
  };
}

export const isZero = (v: Velocity) => v.pan === 0 && v.tilt === 0 && v.zoom === 0;

/** 速度から送る命令(すべて 0 なら stop) */
export function moveCommand(v: Velocity): PtzCommand {
  if (isZero(v)) return { op: 'stop' };
  return { op: 'continuous', pan: v.pan, tilt: v.tilt, zoom: v.zoom, timeout_ms: CONTINUOUS_TIMEOUT_MS };
}

/** タップしたときの一定量の移動(relative) */
export function stepCommand(dirs: Iterable<PtzDir>): PtzCommand {
  const v = velocityOf(dirs);
  const cmd: PtzCommand = { op: 'relative' };
  if (v.pan !== 0 || v.tilt !== 0) {
    cmd.pan = Math.sign(v.pan) * PAN_TILT_STEP;
    cmd.tilt = Math.sign(v.tilt) * PAN_TILT_STEP;
  }
  if (v.zoom !== 0) cmd.zoom = Math.sign(v.zoom) * ZOOM_STEP;
  return cmd;
}

/** キーボード(パネルにフォーカスがあるときだけ)。矢印で向き、+ / − でズーム */
export function keyToDir(e: { key: string; code?: string }): PtzDir | null {
  switch (e.key) {
    case 'ArrowUp':
      return 'up';
    case 'ArrowDown':
      return 'down';
    case 'ArrowLeft':
      return 'left';
    case 'ArrowRight':
      return 'right';
    case '+':
    case '=':
      return 'in';
    case '-':
    case '_':
      return 'out';
  }
  if (e.code === 'NumpadAdd') return 'in';
  if (e.code === 'NumpadSubtract') return 'out';
  return null;
}

const isMotion = (c: PtzCommand) => c.op === 'continuous' || c.op === 'stop';

/**
 * 命令を 1 つずつ順に送る(前の 202 を待ってから次を送る)。
 * HTTP を並べて送ると、continuous と stop の順が入れ替わって止まらないことがあるため。
 * 待っている間に来た continuous / stop は最後の 1 つだけ残す(途中の速度は送っても意味がない)。
 */
export class PtzQueue {
  private pending: PtzCommand[] = [];
  private busy = false;
  private readonly send: (cmd: PtzCommand) => Promise<unknown>;
  private readonly onError: (e: unknown, cmd: PtzCommand) => void;

  constructor(send: (cmd: PtzCommand) => Promise<unknown>, onError: (e: unknown, cmd: PtzCommand) => void = () => {}) {
    this.send = send;
    this.onError = onError;
  }

  push(cmd: PtzCommand): void {
    if (isMotion(cmd)) this.pending = this.pending.filter((c) => !isMotion(c));
    this.pending.push(cmd);
    void this.pump();
  }

  /** 送信待ちを捨てる(カメラを切り替えたとき) */
  clear(): void {
    this.pending = [];
  }

  get size(): number {
    return this.pending.length + (this.busy ? 1 : 0);
  }

  private async pump(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      let cmd: PtzCommand | undefined;
      while ((cmd = this.pending.shift())) {
        try {
          await this.send(cmd);
        } catch (e) {
          this.onError(e, cmd);
        }
      }
    } finally {
      this.busy = false;
    }
  }
}

/** 拡大率の目安(zoom 0 → 1 倍、1 → maxZoom 倍。エミュレーターの既定は 4 倍) */
export const MAX_ZOOM_FACTOR = 4;
export const zoomFactor = (zoom: number) => 1 + Math.min(1, Math.max(0, zoom)) * (MAX_ZOOM_FACTOR - 1);

/**
 * 全体の中で表示している範囲(切り出し窓)を 0〜1 の矩形で返す(ミニマップ用の目安)。
 * pan -1 が左端、tilt 1 が上端(画面の y は下向きなので反転する)。
 */
export function cropRect(pan: number, tilt: number, zoom: number) {
  const size = 1 / zoomFactor(zoom);
  const clamp = (v: number) => Math.min(1, Math.max(-1, v));
  const left = ((clamp(pan) + 1) / 2) * (1 - size);
  const top = ((1 - clamp(tilt)) / 2) * (1 - size);
  return { left, top, width: size, height: size };
}

/** 表示用の数値(+0.25 / −0.10)。値がなければ — */
export function formatAxis(v: number | null | undefined, signed = true): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const s = Math.abs(v).toFixed(2);
  if (!signed) return s;
  if (Math.abs(v) < 0.005) return (0).toFixed(2);
  return `${v < 0 ? '−' : '+'}${s}`;
}

/** 次のプリセット名の候補 */
export function nextPresetName(names: string[]): string {
  for (let i = names.length + 1; ; i++) {
    const n = `プリセット ${i}`;
    if (!names.includes(n)) return n;
  }
}

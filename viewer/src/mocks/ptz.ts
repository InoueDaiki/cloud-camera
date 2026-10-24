// モックの疑似 PTZ。エミュレーターと同じく、速度上限付きで滑らかに動く(瞬間移動しない)。
// GET のたびに経過時間ぶん進める。座標は ONVIF の Generic 空間(pan / tilt -1〜1、zoom 0〜1)。

import type { PtzCommand, PtzPreset, PtzState } from '../api/types';

/** 対応しているカメラ(倉庫 cam-003 は非対応) */
const SUPPORTED = new Set(['cam-001', 'cam-002']);
/** 1 秒あたりの最大の動き(速度 1 のとき) */
const MAX_RATE = 0.5;

interface Sim {
  pos: { pan: number; tilt: number; zoom: number };
  vel: { pan: number; tilt: number; zoom: number };
  velUntil: number;
  target: { pan: number; tilt: number; zoom: number } | null;
  last: number;
  presets: (PtzPreset & { pos: Sim['pos'] })[];
  updatedAt: number;
}

const sims = new Map<string, Sim>();

function sim(id: string, now: number): Sim {
  let s = sims.get(id);
  if (!s) {
    s = {
      pos: id === 'cam-001' ? { pan: 0.2, tilt: -0.1, zoom: 0.35 } : { pan: 0, tilt: 0, zoom: 0 },
      vel: { pan: 0, tilt: 0, zoom: 0 },
      velUntil: 0,
      target: null,
      last: now,
      presets:
        id === 'cam-001'
          ? [
              { token: '1', name: '玄関の扉', pos: { pan: -0.4, tilt: 0.2, zoom: 0.6 } },
              { token: '2', name: '郵便受け', pos: { pan: 0.6, tilt: -0.5, zoom: 0.8 } },
            ]
          : [],
      updatedAt: now,
    };
    sims.set(id, s);
  }
  return s;
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const moving = (s: Sim) => s.target !== null || s.vel.pan !== 0 || s.vel.tilt !== 0 || s.vel.zoom !== 0;

function advance(s: Sim, now: number) {
  const until = Math.min(now, s.velUntil);
  const dtVel = Math.max(0, until - s.last) / 1000;
  const dt = Math.max(0, now - s.last) / 1000;
  if (moving(s)) s.updatedAt = now;
  if (s.target) {
    const step = MAX_RATE * dt;
    let done = true;
    for (const k of ['pan', 'tilt', 'zoom'] as const) {
      const d = s.target[k] - s.pos[k];
      if (Math.abs(d) <= step) s.pos[k] = s.target[k];
      else {
        s.pos[k] += Math.sign(d) * step;
        done = false;
      }
    }
    if (done) s.target = null;
  } else {
    s.pos.pan = clamp(s.pos.pan + s.vel.pan * MAX_RATE * dtVel, -1, 1);
    s.pos.tilt = clamp(s.pos.tilt + s.vel.tilt * MAX_RATE * dtVel, -1, 1);
    s.pos.zoom = clamp(s.pos.zoom + s.vel.zoom * MAX_RATE * dtVel, 0, 1);
  }
  if (now >= s.velUntil) s.vel = { pan: 0, tilt: 0, zoom: 0 };
  s.last = now;
}

export function mockPtzSupported(id: string): boolean {
  return SUPPORTED.has(id);
}

export function mockPtzState(id: string, now: number): PtzState {
  if (!SUPPORTED.has(id)) return { supported: false };
  const s = sim(id, now);
  advance(s, now);
  return {
    supported: true,
    ...s.pos,
    presets: s.presets.map(({ token, name }) => ({ token, name })),
    move_status: moving(s) ? 'MOVING' : 'IDLE',
    updated_at: new Date(s.updatedAt).toISOString().replace(/\.\d{3}Z$/, 'Z'),
  };
}

/** 命令を適用する。エラーなら文言を返す */
export function mockPtzCommand(id: string, cmd: PtzCommand, now: number): string | null {
  if (!SUPPORTED.has(id)) return 'このカメラは PTZ に対応していません';
  const s = sim(id, now);
  advance(s, now);
  switch (cmd.op) {
    case 'continuous':
      s.target = null;
      s.vel = { pan: cmd.pan ?? 0, tilt: cmd.tilt ?? 0, zoom: cmd.zoom ?? 0 };
      s.velUntil = now + (cmd.timeout_ms ?? 10_000);
      break;
    case 'stop':
      s.target = null;
      s.vel = { pan: 0, tilt: 0, zoom: 0 };
      break;
    case 'home':
      s.target = { pan: 0, tilt: 0, zoom: 0 };
      break;
    case 'absolute':
      s.target = { pan: cmd.pan ?? s.pos.pan, tilt: cmd.tilt ?? s.pos.tilt, zoom: cmd.zoom ?? s.pos.zoom };
      break;
    case 'relative':
      s.target = {
        pan: clamp(s.pos.pan + (cmd.pan ?? 0), -1, 1),
        tilt: clamp(s.pos.tilt + (cmd.tilt ?? 0), -1, 1),
        zoom: clamp(s.pos.zoom + (cmd.zoom ?? 0), 0, 1),
      };
      break;
    case 'goto_preset': {
      const p = s.presets.find((x) => x.token === cmd.preset_token);
      if (!p) return `プリセットがありません: ${cmd.preset_token}`;
      s.target = { ...p.pos };
      break;
    }
    case 'set_preset': {
      const token = cmd.preset_token ?? String(Math.max(0, ...s.presets.map((p) => Number(p.token) || 0)) + 1);
      s.presets = s.presets.filter((p) => p.token !== token);
      s.presets.push({ token, name: cmd.preset_name ?? `プリセット ${token}`, pos: { ...s.pos } });
      break;
    }
    default:
      return `op が不正です: ${String((cmd as { op: unknown }).op)}`;
  }
  s.updatedAt = now;
  return null;
}

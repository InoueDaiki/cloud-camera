// モックのデータ。現在時刻から逆算して、それらしい録画の隙間、FAILED、未退避(KVS)の区間を作る。
// 同じ時刻には常に同じ結果を返す(乱数は時刻とカメラから決める)。

import type { CameraRaw, RecordedRange, TimelineResponse, TimeRange, WebRtcSessionInfo } from '../api/types';
import { DAY, floorTo, HOUR, jstDayStartOf, MINUTE, SECOND } from '../lib/time';
import { mockPtzState } from './ptz';

/** モックの録画は 10 秒単位(テスト用 HLS の 1 セグメントの長さに合わせる) */
export const SLOT = 10 * SECOND;
/** 何日前まで録画があることにするか */
const RETENTION = 7 * DAY;

interface MockCamera {
  camera_id: string;
  name: string;
  gateway_id: string;
  /** 処理済み位置の遅れ(分) */
  lagMinutes: number;
  /** この分だけ前にオフラインになった(null ならオンライン) */
  offlineMinutesAgo: number | null;
  streaming: boolean;
  /** KVS WebRTC のライブ(false なら desired で止めている。ビューワーは HLS で見る) */
  webrtc: boolean;
}

const cameras: MockCamera[] = [
  { camera_id: 'cam-001', name: '玄関', gateway_id: 'gw-001', lagMinutes: 2, offlineMinutesAgo: null, streaming: true, webrtc: true },
  // 駐車場は WebRTC を止めている(HLS で見る。PTZ は WebRTC のときだけなので無効になる)
  { camera_id: 'cam-002', name: '駐車場', gateway_id: 'gw-001', lagMinutes: 14, offlineMinutesAgo: null, streaming: true, webrtc: false },
  { camera_id: 'cam-003', name: '倉庫', gateway_id: 'gw-002', lagMinutes: 2, offlineMinutesAgo: 40, streaming: false, webrtc: true },
];

/** 配信の開始・停止の指示(PUT /streaming)をモックの中で覚えておく */
const desired = new Map<string, boolean>(cameras.map((c) => [c.camera_id, c.streaming]));

// 0〜1 の決定的な乱数
function rand(...keys: number[]): number {
  let h = 2166136261;
  for (const k of keys) {
    h ^= k & 0xffffffff;
    h = Math.imul(h, 16777619);
    h ^= Math.floor(k / 0x100000000);
    h = Math.imul(h, 16777619);
  }
  h ^= h >>> 13;
  h = Math.imul(h, 0x5bd1e995);
  h ^= h >>> 15;
  return (h >>> 0) / 0xffffffff;
}

const camIndex = (id: string) => cameras.findIndex((c) => c.camera_id === id);
const jstMinuteOfDay = (t: number) => Math.floor((t - jstDayStartOf(t)) / MINUTE);

function offlineAt(cam: MockCamera, now: number): number | null {
  return cam.offlineMinutesAgo === null ? null : floorTo(now, SLOT) - cam.offlineMinutesAgo * MINUTE;
}

export function processedUntil(cam: MockCamera, now: number): number {
  return floorTo(now, MINUTE) - cam.lagMinutes * MINUTE;
}

/** その 1 分の退避が FAILED になったか */
function isFailedMinute(idx: number, minuteStart: number): boolean {
  const mod = jstMinuteOfDay(minuteStart);
  if (idx === 0) return mod === 9 * 60 + 41; // 毎日 09:41
  if (idx === 1) return (mod >= 6 * 60 + 20 && mod < 6 * 60 + 25) || rand(idx, minuteStart, 7) < 0.003;
  return false;
}

/** その 10 秒に録画があるか(FAILED を除く元の録画) */
function slotRecorded(idx: number, slotStart: number, now: number): boolean {
  const cam = cameras[idx];
  if (slotStart < now - RETENTION || slotStart + SLOT > now) return false;
  const off = offlineAt(cam, now);
  if (off !== null && slotStart >= off) return false;
  const mod = jstMinuteOfDay(slotStart);
  if (idx === 0) {
    if (mod >= 3 * 60 + 10 && mod < 3 * 60 + 40) return false; // 深夜の回線断
    return rand(idx, Math.floor(slotStart / SLOT)) > 0.015;
  }
  if (idx === 1) {
    if (mod >= 13 * 60 && mod < 13 * 60 + 15) return false; // ゲートウェイ再起動
    return rand(idx, Math.floor(slotStart / SLOT)) > 0.008;
  }
  // 倉庫: 動きがあるときだけ録る想定で、15 分単位でまばらに
  return rand(idx, Math.floor(slotStart / (15 * MINUTE))) < 0.45;
}

export type SlotState = 's3' | 'kvs' | 'failed' | null;

export function slotState(cameraId: string, slotStart: number, now: number): SlotState {
  const idx = camIndex(cameraId);
  if (idx < 0) return null;
  const minuteStart = floorTo(slotStart, MINUTE);
  const processed = processedUntil(cameras[idx], now);
  if (minuteStart < processed && isFailedMinute(idx, minuteStart)) return 'failed';
  if (!slotRecorded(idx, slotStart, now)) return null;
  return minuteStart < processed ? 's3' : 'kvs';
}

const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');

/** GET /api/cameras(API と同じ形。`online` は API がまだ返さない任意の項目だが、表示の確認のため返す) */
export function mockCameras(now: number): CameraRaw[] {
  return cameras.map((c) => {
    const off = offlineAt(c, now);
    const want = desired.get(c.camera_id) ?? false;
    return {
      camera_id: c.camera_id,
      gateway_id: c.gateway_id,
      stream_name: `cloud-camera-${c.camera_id}`,
      online: off === null,
      reported: {
        name: c.name,
        streaming: off === null && want,
        rtsp_url: `rtsp://emulator:8554/cam${c.camera_id.slice(-1)}/main`,
        codec: mockCodec(c.camera_id, now),
        encoder: mockEncoder(c.camera_id, now),
        resolution: '1920x1080',
        error: off === null ? null : 'ゲートウェイからの応答がありません(LWT 受信)',
        ptz: mockPtzState(c.camera_id, now),
        webrtc: mockWebRtcReported(c, now),
      },
      desired: {
        name: c.name,
        streaming: want,
        username: 'admin',
        password: '***',
        profile_token: null,
        ...(c.webrtc ? {} : { webrtc: { enabled: false } }),
        ...(codecs.has(c.camera_id) ? { codec: codecs.get(c.camera_id)!.want } : {}),
        ...(encoders.has(c.camera_id) ? { encoder: encoders.get(c.camera_id)!.want } : {}),
      },
      reported_at: iso(off ?? now - 12 * SECOND),
      video_processed_until: iso(processedUntil(c, now)),
      detect_processed_until: iso(processedUntil(c, now) - MINUTE),
    };
  });
}

function webrtcCodecs(cameraId: string, now: number): string[] {
  // main が H.265 なら H.265(main)と H.264(sub)、H.264 に切り替えたカメラは H.264 だけ
  return mockCodec(cameraId, now) === 'H265' ? ['H265', 'H264'] : ['H264'];
}

function mockWebRtcReported(c: MockCamera, now: number) {
  const live = offlineAt(c, now) === null && (desired.get(c.camera_id) ?? false);
  const state = !c.webrtc || !live ? 'DISABLED' : 'CONNECTED';
  return { enabled: c.webrtc, channel: `cloud-camera-${c.camera_id}`, state, codecs: state === 'CONNECTED' ? webrtcCodecs(c.camera_id, now) : [], viewers: 0 };
}

/** GET /api/cameras/{id}/webrtc。見られないカメラは API と同じ 409 */
export function mockWebRtc(
  cameraId: string,
  now: number,
): WebRtcSessionInfo | { status: number; error: { code: string; message: string } } {
  const c = cameras[camIndex(cameraId)];
  if (!c) return { status: 404, error: { code: 'CAMERA_NOT_FOUND', message: 'カメラがありません' } };
  if (!c.webrtc) return { status: 409, error: { code: 'WEBRTC_DISABLED', message: 'このカメラは WebRTC のライブを止めています' } };
  if (offlineAt(c, now) !== null) return { status: 409, error: { code: 'CAMERA_OFFLINE', message: 'カメラ(ゲートウェイ)がオフラインです' } };
  if (!(desired.get(c.camera_id) ?? false)) {
    return { status: 409, error: { code: 'WEBRTC_UNAVAILABLE', message: 'エッジの WebRTC が待ち受けていません(state=DISABLED)' } };
  }
  const clientId = `viewer-${Math.random().toString(16).slice(2, 14)}`;
  return {
    camera_id: c.camera_id,
    channel_name: `cloud-camera-${c.camera_id}`,
    channel_arn: `arn:aws:kinesisvideo:ap-northeast-1:000000000000:channel/cloud-camera-${c.camera_id}/0`,
    region: 'ap-northeast-1',
    client_id: clientId,
    wss_endpoint: 'wss://mock.invalid',
    wss_url: 'wss://mock.invalid/?X-Amz-Signature=mock',
    ice_servers: [],
    codecs: webrtcCodecs(c.camera_id, now),
    expires_at: iso(now + 299 * SECOND),
    mock: true,
  };
}

// 映像形式の指示(desired の codec)。エッジの切り替えを模して、指示から 4 秒後に reported が変わる
const codecs = new Map<string, { want: 'H265' | 'H264'; prev: string; since: number }>();
const CODEC_SWITCH_MS = 4000;

function mockCodec(cameraId: string, now: number): string {
  const c = codecs.get(cameraId);
  if (!c) return 'H265';
  return now - c.since >= CODEC_SWITCH_MS ? c.want : c.prev;
}

export function setDesiredCodec(cameraId: string, codec: 'H265' | 'H264', now: number): boolean {
  if (camIndex(cameraId) < 0) return false;
  codecs.set(cameraId, { want: codec, prev: mockCodec(cameraId, now), since: now });
  return true;
}

// エンコーダーの設定(desired の encoder)。エッジがカメラに設定するのを模して、指示から 3 秒後に reported が変わる
type MockEncoder = { quality: number; bitrate_limit_kbps: number; rate_control: 'VBR' | 'CBR' };
const DEFAULT_ENCODER: MockEncoder = { quality: 60, bitrate_limit_kbps: 4000, rate_control: 'VBR' };
const encoders = new Map<string, { want: Partial<MockEncoder>; prev: MockEncoder; since: number }>();
const ENCODER_APPLY_MS = 3000;

function mockEncoder(cameraId: string, now: number) {
  const e = encoders.get(cameraId);
  const cur = !e ? DEFAULT_ENCODER : now - e.since >= ENCODER_APPLY_MS ? { ...e.prev, ...e.want } : e.prev;
  return { ...cur, framerate: 30, gov_length: 30 };
}

export function setDesiredEncoder(cameraId: string, want: Partial<MockEncoder>, now: number): boolean {
  if (camIndex(cameraId) < 0) return false;
  const { quality, bitrate_limit_kbps, rate_control } = mockEncoder(cameraId, now);
  encoders.set(cameraId, {
    want: { ...encoders.get(cameraId)?.want, ...want },
    prev: { quality, bitrate_limit_kbps, rate_control },
    since: now,
  });
  return true;
}

export function setDesiredStreaming(cameraId: string, streaming: boolean): boolean {
  if (camIndex(cameraId) < 0) return false;
  desired.set(cameraId, streaming);
  return true;
}

export function mockTimeline(cameraId: string, from: number, to: number, now: number): TimelineResponse | null {
  const idx = camIndex(cameraId);
  if (idx < 0) return null;
  const cam = cameras[idx];
  const recorded: RecordedRange[] = [];
  const failed: TimeRange[] = [];
  const minutes: TimelineResponse['minutes'] = [];

  const push = (list: { start: string; end: string }[], s: number, e: number, extra?: object) => {
    const last = list[list.length - 1] as (TimeRange & { source?: string }) | undefined;
    const lastSource = last?.source;
    const nextSource = (extra as { source?: string } | undefined)?.source;
    if (last && Date.parse(last.end) === s && lastSource === nextSource) {
      last.end = iso(e);
    } else {
      list.push({ start: iso(s), end: iso(e), ...extra });
    }
  };

  const end = Math.min(to, now);
  for (let m = floorTo(from, MINUTE); m < end; m += MINUTE) {
    let any = false;
    for (let s = m; s < m + MINUTE && s < end; s += SLOT) {
      const st = slotState(cameraId, s, now);
      if (st === 'failed') {
        push(failed, s, s + SLOT);
      } else if (st) {
        push(recorded, s, s + SLOT, { source: st });
        if (st === 's3') any = true;
      }
    }
    if (any) {
      const d = new Date(m);
      const p = (n: number) => String(n).padStart(2, '0');
      minutes.push({
        start: iso(m),
        sprite: `/media/${cameraId}/${d.getUTCFullYear()}/${p(d.getUTCMonth() + 1)}/${p(d.getUTCDate())}/${p(d.getUTCHours())}/${p(d.getUTCMinutes())}/sprite.jpg`,
      });
    }
  }

  return {
    camera_id: cameraId,
    from: iso(from),
    to: iso(to),
    video_processed_until: iso(processedUntil(cam, now)),
    detect_processed_until: iso(processedUntil(cam, now) - MINUTE),
    recorded,
    failed,
    minutes,
  };
}

// ---- 動画(公開のテスト HLS のセグメントで代用) ----

const TEST_BASE = 'https://test-streams.mux.dev/x36xhzz/url_0/';
/** 長さがちょうど 10 秒のセグメントだけを使う */
const TEST_SEGMENTS = Array.from({ length: 20 }, (_, i) => `${TEST_BASE}url_${471 + i}/193039199_mp4_h264_aac_hd_7.ts`);
const segmentFor = (slotStart: number) => TEST_SEGMENTS[Math.floor(slotStart / SLOT) % TEST_SEGMENTS.length];

/** [from, to) の録画を、区間ごとに EXT-X-DISCONTINUITY と PROGRAM-DATE-TIME を付けて並べる */
export function mockPlaylist(
  cameraId: string,
  from: number,
  to: number,
  now: number,
  source: 's3' | 'kvs',
): string {
  const lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-TARGETDURATION:10', '#EXT-X-PLAYLIST-TYPE:VOD'];
  let prevEnd: number | null = null;
  let count = 0;
  for (let s = floorTo(from, SLOT); s < to && s < now; s += SLOT) {
    const st = slotState(cameraId, s, now);
    if (st !== source) continue;
    if (prevEnd !== s) {
      if (count > 0) lines.push('#EXT-X-DISCONTINUITY');
      lines.push(`#EXT-X-PROGRAM-DATE-TIME:${new Date(s).toISOString()}`);
    }
    lines.push('#EXTINF:10.000,', segmentFor(s));
    prevEnd = s + SLOT;
    count++;
  }
  lines.push('#EXT-X-ENDLIST');
  return lines.join('\n') + '\n';
}

/** ライブ: 直近 3 セグメントのスライディングウィンドウ */
export function mockLivePlaylist(now: number): string {
  const last = floorTo(now, SLOT) - SLOT;
  const first = last - 2 * SLOT;
  const lines = [
    '#EXTM3U',
    '#EXT-X-VERSION:3',
    '#EXT-X-TARGETDURATION:10',
    `#EXT-X-MEDIA-SEQUENCE:${Math.floor(first / SLOT)}`,
    `#EXT-X-PROGRAM-DATE-TIME:${new Date(first).toISOString()}`,
  ];
  for (let s = first; s <= last; s += SLOT) lines.push('#EXTINF:10.000,', segmentFor(s));
  return lines.join('\n') + '\n';
}

// ---- 自然言語検索 ----

export function mockSearch(question: string, now: number) {
  const q = question.toLowerCase();
  const isCar = /車|car|トラック|駐車/.test(q);
  const label = isCar ? 'car' : 'person';
  const cameraId = isCar ? 'cam-002' : 'cam-001';
  const yesterday = jstDayStartOf(now) - DAY;
  // 昨日の夕方(16〜19 時)から、録画がある時刻を拾う
  const hits = [] as { camera_id: string; ts: string; label: string; confidence: number }[];
  for (let i = 0; hits.length < (isCar ? 4 : 6) && i < 200; i++) {
    const t = yesterday + 16 * HOUR + Math.floor(rand(i, 99) * 3 * HOUR);
    const ms = floorTo(t, SECOND) + 400;
    if (slotState(cameraId, floorTo(ms, SLOT), now) !== 's3') continue;
    hits.push({
      camera_id: cameraId,
      ts: new Date(ms).toISOString(),
      label,
      confidence: Math.round((0.62 + rand(i, 3) * 0.36) * 100) / 100,
    });
  }
  hits.sort((a, b) => a.ts.localeCompare(b.ts));
  const day = new Date(yesterday + 9 * HOUR).toISOString().slice(0, 10);
  const fmt = (ts: string) => new Date(Date.parse(ts) + 9 * HOUR).toISOString().slice(11, 16);
  const answer = isCar
    ? `昨日(${day})の夕方、駐車場のカメラで車両が ${hits.length} 回検出されました。最初は ${fmt(hits[0].ts)}、最後は ${fmt(hits[hits.length - 1].ts)} です。`
    : `昨日(${day})の 16〜19 時に、玄関のカメラで人物が ${hits.length} 回検出されました。${fmt(hits[0].ts)} ごろが最初で、${fmt(hits[hits.length - 1].ts)} ごろにも映っています。各時刻をクリックすると映像を確認できます。`;
  const sql = `SELECT camera_id, ts, label, max(confidence) AS confidence
FROM cloud_camera.detections
WHERE dt = '${day}'
  AND camera_id = '${cameraId}'
  AND label = '${label}'
  AND ts BETWEEN timestamp '${day} 07:00:00' AND timestamp '${day} 10:00:00'
  AND confidence >= 0.6
GROUP BY camera_id, ts, label
ORDER BY ts
LIMIT 50`;
  return {
    answer,
    hits,
    sql,
    usage: { input_tokens: 2841, output_tokens: 312, total_tokens: 3153, model_id: 'amazon.nova-2-lite-v1:0', bedrock_calls: 2, tool_calls: 1, athena_bytes_scanned: 4_718_592, elapsed_ms: 3480 },
  };
}

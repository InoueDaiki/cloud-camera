import type {
  Camera,
  CameraRaw,
  CamerasResponse,
  CamerasResponseRaw,
  EncoderSettings,
  KvsHlsResponse,
  PtzCommand,
  PtzCommandResponse,
  PtzState,
  SearchResponse,
  TimelineResponse,
  WebRtcSessionInfo,
} from './types';

// 本番は CloudFront の同一オリジンから配信されるので、API は相対パスで呼ぶ。
const API = '/api';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  constructor(status: number, message: string, code: string | null = null) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    let code: string | null = null;
    try {
      // API のエラーは {"error": {"code", "message"}}
      const body = (await res.json()) as { message?: string; error?: string | { code?: string; message?: string } };
      const err = body.error;
      message = (typeof err === 'object' ? err?.message : err) ?? body.message ?? message;
      code = (typeof err === 'object' ? err?.code : null) ?? null;
    } catch {
      // 本文が JSON でなければステータスだけ出す
    }
    throw new ApiError(res.status, message, code);
  }
  return (await res.json()) as T;
}

const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');

const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);

const obj = (v: unknown): EncoderSettings | null =>
  v && typeof v === 'object' && !Array.isArray(v) ? (v as EncoderSettings) : null;

/** API のカメラ(シャドウの reported / desired を含む)を画面で使う形に直す */
export function normalizeCamera(raw: CameraRaw): Camera {
  const rep = raw.reported ?? null;
  const des = raw.desired ?? null;
  const streaming = rep?.streaming === true;
  return {
    camera_id: raw.camera_id,
    name: str(rep?.name) ?? str(des?.name),
    gateway_id: raw.gateway_id ?? null,
    online: typeof raw.online === 'boolean' ? raw.online : null,
    streaming,
    desired_streaming: typeof des?.streaming === 'boolean' ? des.streaming : streaming,
    codec: str(rep?.codec),
    desired_codec: str(des?.codec),
    codec_error: str(rep?.codec_error),
    encoder: obj(rep?.encoder),
    desired_encoder: obj(des?.encoder),
    encoder_error: str(rep?.encoder_error),
    resolution: str(rep?.resolution),
    error: str(rep?.error),
    ptz_supported: rep?.ptz?.supported === true,
    reported_at: raw.reported_at ?? null,
    video_processed_until: raw.video_processed_until ?? null,
    detect_processed_until: raw.detect_processed_until ?? null,
  };
}

export const api = {
  cameras: async (): Promise<CamerasResponse> => {
    const res = await request<CamerasResponseRaw>('/cameras');
    return { cameras: res.cameras.map(normalizeCamera) };
  },

  setStreaming: (cameraId: string, streaming: boolean) =>
    request<unknown>(`/cameras/${encodeURIComponent(cameraId)}/streaming`, {
      method: 'PUT',
      body: JSON.stringify({ streaming }),
    }),

  /** 映像形式の切り替え(シャドウの desired に書く。エッジがカメラを切り替えて配信をつなぎ直す) */
  setCodec: (cameraId: string, codec: 'H265' | 'H264') =>
    request<unknown>(`/cameras/${encodeURIComponent(cameraId)}/codec`, {
      method: 'PUT',
      body: JSON.stringify({ codec }),
    }),

  /** エンコーダーの設定(書いた項目だけをシャドウの desired に書く。エッジが配信を止めずにカメラへ設定する) */
  setEncoder: (cameraId: string, encoder: Pick<EncoderSettings, 'quality' | 'bitrate_limit_kbps' | 'rate_control'>) =>
    request<unknown>(`/cameras/${encodeURIComponent(cameraId)}/encoder`, {
      method: 'PUT',
      body: JSON.stringify(encoder),
    }),

  timeline: (cameraId: string, fromMs: number, toMs: number) =>
    request<TimelineResponse>(
      `/cameras/${encodeURIComponent(cameraId)}/timeline?from=${iso(fromMs)}&to=${iso(toMs)}`,
    ),

  kvsHls: (cameraId: string, mode: 'LIVE' | 'ON_DEMAND', fromMs?: number, toMs?: number) => {
    const q = new URLSearchParams({ mode });
    if (fromMs !== undefined) q.set('from', iso(fromMs));
    if (toMs !== undefined) q.set('to', iso(toMs));
    return request<KvsHlsResponse>(`/cameras/${encodeURIComponent(cameraId)}/kvs-hls?${q}`);
  },

  /** KVS WebRTC の視聴情報(署名付き WSS URL と ICE サーバー)。WebRTC で見られないカメラは 409 */
  webrtc: (cameraId: string) => request<WebRtcSessionInfo>(`/cameras/${encodeURIComponent(cameraId)}/webrtc`),

  /** S3 に退避済みの区間を連結した HLS プレイリストの URL(取得は hls.js / Safari が行う) */
  vodUrl: (cameraId: string, fromMs: number, toMs: number) =>
    `${API}/cameras/${encodeURIComponent(cameraId)}/vod.m3u8?from=${iso(fromMs)}&to=${iso(toMs)}`,

  ptz: (cameraId: string) => request<PtzState>(`/cameras/${encodeURIComponent(cameraId)}/ptz`),

  /** PTZ の操作。API は IoT Core に送ったところで 202 を返す(映像への反映はライブの遅れの分だけ後) */
  ptzCommand: (cameraId: string, cmd: PtzCommand) =>
    request<PtzCommandResponse>(`/cameras/${encodeURIComponent(cameraId)}/ptz`, {
      method: 'POST',
      body: JSON.stringify(cmd),
    }),

  search: (question: string) =>
    request<SearchResponse>('/search', { method: 'POST', body: JSON.stringify({ question }) }),
};

export { iso as toIsoSeconds };

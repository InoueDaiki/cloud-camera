import type {
  CamerasResponse,
  KvsHlsResponse,
  SearchResponse,
  TimelineResponse,
} from './types';

// 本番は CloudFront の同一オリジンから配信されるので、API は相対パスで呼ぶ。
const API = '/api';

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { message?: string; error?: string };
      message = body.message ?? body.error ?? message;
    } catch {
      // 本文が JSON でなければステータスだけ出す
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
}

const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');

export const api = {
  cameras: () => request<CamerasResponse>('/cameras'),

  setStreaming: (cameraId: string, streaming: boolean) =>
    request<unknown>(`/cameras/${encodeURIComponent(cameraId)}/streaming`, {
      method: 'PUT',
      body: JSON.stringify({ streaming }),
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

  /** S3 に退避済みの区間を連結した HLS プレイリストの URL(取得は hls.js / Safari が行う) */
  vodUrl: (cameraId: string, fromMs: number, toMs: number) =>
    `${API}/cameras/${encodeURIComponent(cameraId)}/vod.m3u8?from=${iso(fromMs)}&to=${iso(toMs)}`,

  search: (question: string) =>
    request<SearchResponse>('/search', { method: 'POST', body: JSON.stringify({ question }) }),
};

export { iso as toIsoSeconds };

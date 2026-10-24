// MSW のハンドラー。docs/contracts.md の HTTP API とメディアの URL を真似る。

import { delay, http, HttpResponse } from 'msw';
import {
  mockCameras,
  mockLivePlaylist,
  mockPlaylist,
  mockSearch,
  mockTimeline,
  setDesiredStreaming,
} from './data';
import { mockPtzCommand, mockPtzState } from './ptz';
import { spriteBlob } from './sprite';
import type { PtzCommand } from '../api/types';

const t = (v: string | null) => (v ? Date.parse(v) : NaN);
const m3u8 = (body: string) =>
  new HttpResponse(body, { headers: { 'Content-Type': 'application/vnd.apple.mpegurl' } });

export const handlers = [
  http.get('/api/cameras', async () => {
    await delay(150);
    return HttpResponse.json({ cameras: mockCameras(Date.now()) });
  }),

  http.put('/api/cameras/:id/streaming', async ({ params, request }) => {
    const body = (await request.json()) as { streaming?: boolean };
    await delay(400);
    if (typeof body.streaming !== 'boolean') return HttpResponse.json({ error: { code: 'BAD_REQUEST', message: 'streaming が必要です' } }, { status: 400 });
    if (!setDesiredStreaming(String(params.id), body.streaming)) return HttpResponse.json({ error: { code: 'CAMERA_NOT_FOUND', message: 'カメラがありません' } }, { status: 404 });
    return HttpResponse.json({ camera_id: params.id, streaming: body.streaming, version: 1 });
  }),

  http.get('/api/cameras/:id/ptz', async ({ params }) => {
    await delay(120);
    return HttpResponse.json(mockPtzState(String(params.id), Date.now()));
  }),

  // 実際の API は IoT Core に送ったところで 202 を返す(エッジの処理は待たない)
  http.post('/api/cameras/:id/ptz', async ({ params, request }) => {
    const body = (await request.json()) as PtzCommand;
    await delay(150);
    const err = mockPtzCommand(String(params.id), body, Date.now());
    if (err) return HttpResponse.json({ error: { code: 'BAD_REQUEST', message: err } }, { status: 400 });
    return HttpResponse.json({ request_id: crypto.randomUUID() }, { status: 202 });
  }),

  http.get('/api/cameras/:id/timeline', async ({ params, request }) => {
    const url = new URL(request.url);
    const from = t(url.searchParams.get('from'));
    const to = t(url.searchParams.get('to'));
    if (Number.isNaN(from) || Number.isNaN(to)) return HttpResponse.json({ error: { code: 'BAD_REQUEST', message: 'from と to が必要です' } }, { status: 400 });
    await delay(200);
    const res = mockTimeline(String(params.id), from, to, Date.now());
    return res ? HttpResponse.json(res) : HttpResponse.json({ error: { code: 'CAMERA_NOT_FOUND', message: 'カメラがありません' } }, { status: 404 });
  }),

  http.get('/api/cameras/:id/vod.m3u8', ({ params, request }) => {
    const url = new URL(request.url);
    const body = mockPlaylist(String(params.id), t(url.searchParams.get('from')), t(url.searchParams.get('to')), Date.now(), 's3');
    // 実際の API と同じく、録画がなければ 404
    if (!body.includes('#EXTINF')) return HttpResponse.json({ error: { code: 'NO_RECORDING', message: '範囲内に S3 へ退避済みの録画がありません' } }, { status: 404 });
    return m3u8(body);
  }),

  http.get('/api/cameras/:id/kvs-hls', async ({ params, request }) => {
    const url = new URL(request.url);
    const mode = url.searchParams.get('mode');
    await delay(250);
    const expires = new Date(Date.now() + 300_000).toISOString();
    if (mode === 'LIVE') {
      const cam = mockCameras(Date.now()).find((c) => c.camera_id === params.id);
      // KVS は配信中でないと ResourceNotFoundException(「断片がない」)を返す
      if (!cam?.reported?.streaming) return HttpResponse.json({ error: { code: 'NO_FRAGMENTS', message: 'ライブ配信がありません' } }, { status: 404 });
    }
    if (mode === 'LIVE') return HttpResponse.json({ url: `/__mock/kvs/${params.id}/live.m3u8`, expires_at: expires });
    const q = new URLSearchParams({ from: url.searchParams.get('from') ?? '', to: url.searchParams.get('to') ?? '' });
    return HttpResponse.json({ url: `/__mock/kvs/${params.id}/ondemand.m3u8?${q}`, expires_at: expires });
  }),

  // KVS の HLS セッション URL の代わり
  http.get('/__mock/kvs/:id/live.m3u8', () => m3u8(mockLivePlaylist(Date.now()))),
  http.get('/__mock/kvs/:id/ondemand.m3u8', ({ params, request }) => {
    const url = new URL(request.url);
    return m3u8(mockPlaylist(String(params.id), t(url.searchParams.get('from')), t(url.searchParams.get('to')), Date.now(), 'kvs'));
  }),

  http.get('/media/:id/:y/:mo/:d/:h/:mi/sprite.jpg', async ({ params }) => {
    const minute = Date.parse(`${params.y}-${params.mo}-${params.d}T${params.h}:${params.mi}:00Z`);
    const blob = await spriteBlob(String(params.id), minute, Date.now());
    return new HttpResponse(blob, { headers: { 'Content-Type': 'image/jpeg', 'Cache-Control': 'max-age=3600' } });
  }),

  http.post('/api/search', async ({ request }) => {
    const { question } = (await request.json()) as { question?: string };
    if (!question?.trim()) return HttpResponse.json({ error: { code: 'BAD_REQUEST', message: '質問を入力してください' } }, { status: 400 });
    await delay(1200);
    return HttpResponse.json(mockSearch(question, Date.now()));
  }),
];

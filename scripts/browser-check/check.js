// H.265 のブラウザ確認。静的ページから読み込む(外部送信なし)。
// 結果は window.__result に入れ、終わったら window.__done = true にする(run.mjs が読む)。
'use strict';

// テスト用 fMP4(gen-media.sh で生成)。codec はファイルの実際の値(ffprobe で確認済み)
const MEDIA = [
  { id: 'hevc-main', label: 'H.265 Main 8bit', url: 'media/hevc-main.mp4', codec: 'hvc1.1.6.L63.90' },
  { id: 'hevc-main10', label: 'H.265 Main10', url: 'media/hevc-main10.mp4', codec: 'hvc1.2.4.L63.90' },
  { id: 'h264-main', label: 'H.264 Main(比較)', url: 'media/h264-main.mp4', codec: 'avc1.4D4016' },
];

// isTypeSupported / canPlayType で聞く文字列。L93 = レベル 3.1(720p)、L120 = 4.0(1080p)、L150 = 5.0(4K)
const TYPES = [
  'video/mp4; codecs="hvc1.1.6.L93.90"',
  'video/mp4; codecs="hev1.1.6.L93.90"',
  'video/mp4; codecs="hvc1.1.6.L120.90"',
  'video/mp4; codecs="hvc1.1.6.L150.90"',
  'video/mp4; codecs="hvc1.2.4.L120.90"',
  'video/mp4; codecs="hvc1.1.6.L93.90,mp4a.40.2"',
  'video/mp4; codecs="avc1.42E01E"',
  'video/mp4; codecs="avc1.640028"',
  'application/vnd.apple.mpegurl',
];

const MC = [
  { label: 'H.265 720p30', contentType: 'video/mp4; codecs="hvc1.1.6.L93.90"', width: 1280, height: 720, framerate: 30, bitrate: 2_000_000 },
  { label: 'H.265 1080p30', contentType: 'video/mp4; codecs="hvc1.1.6.L120.90"', width: 1920, height: 1080, framerate: 30, bitrate: 4_000_000 },
  { label: 'H.265 4K30', contentType: 'video/mp4; codecs="hvc1.1.6.L150.90"', width: 3840, height: 2160, framerate: 30, bitrate: 12_000_000 },
  { label: 'H.265 Main10 1080p30', contentType: 'video/mp4; codecs="hvc1.2.4.L120.90"', width: 1920, height: 1080, framerate: 30, bitrate: 4_000_000 },
  { label: 'H.264 1080p30(比較)', contentType: 'video/mp4; codecs="avc1.640028"', width: 1920, height: 1080, framerate: 30, bitrate: 4_000_000 },
];

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const mark = (v) => (v === true ? '<span class="ok">○</span>' : v === false ? '<span class="ng">×</span>' : esc(v ?? '—'));
function table(el, head, rows) {
  el.innerHTML = `<tr>${head.map((h) => `<th>${esc(h)}</th>`).join('')}</tr>` +
    rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('');
}
function withTimeout(p, ms, what) {
  return Promise.race([p, sleep(ms).then(() => { throw new Error(`${what}: ${ms} ms で打ち切り`); })]);
}

async function env() {
  const e = { userAgent: navigator.userAgent, platform: navigator.platform, hardwareConcurrency: navigator.hardwareConcurrency };
  if (navigator.userAgentData?.getHighEntropyValues) {
    try {
      const h = await navigator.userAgentData.getHighEntropyValues(['platform', 'platformVersion', 'architecture', 'model', 'fullVersionList']);
      e.uaData = { platform: h.platform, platformVersion: h.platformVersion, architecture: h.architecture, model: h.model, mobile: navigator.userAgentData.mobile,
        brands: (h.fullVersionList || []).map((b) => `${b.brand} ${b.version}`).join(', ') };
    } catch (err) { e.uaData = String(err); }
  }
  e.MediaSource = typeof window.MediaSource === 'function';
  e.ManagedMediaSource = typeof window.ManagedMediaSource === 'function';
  e.RTCPeerConnection = typeof window.RTCPeerConnection === 'function';
  // WebGL のレンダラー(GPU の目安。ハードウェアデコーダの有無の手がかり)
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
    e.gpu = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl ? gl.getParameter(gl.RENDERER) : null;
  } catch { e.gpu = null; }
  return e;
}

function types() {
  const MS = window.MediaSource || window.ManagedMediaSource;
  const v = document.createElement('video');
  return TYPES.map((t) => ({ type: t, isTypeSupported: MS ? MS.isTypeSupported(t) : null, canPlayType: v.canPlayType(t) || '""' }));
}

async function mediaCaps() {
  if (!navigator.mediaCapabilities) return [];
  const out = [];
  for (const m of MC) {
    const row = { label: m.label };
    for (const type of ['media-source', 'file']) {
      try {
        const r = await navigator.mediaCapabilities.decodingInfo({ type, video: { contentType: m.contentType, width: m.width, height: m.height, bitrate: m.bitrate, framerate: m.framerate } });
        row[type] = { supported: r.supported, smooth: r.smooth, powerEfficient: r.powerEfficient };
      } catch (err) { row[type] = { error: String(err) }; }
    }
    out.push(row);
  }
  return out;
}

// 映像が本当に出ているか(真っ黒でないか)を 32×18 に縮めて見る
function brightness(video) {
  try {
    const c = document.createElement('canvas'); c.width = 32; c.height = 18;
    const ctx = c.getContext('2d'); ctx.drawImage(video, 0, 0, 32, 18);
    const d = ctx.getImageData(0, 0, 32, 18).data; let s = 0;
    for (let i = 0; i < d.length; i += 4) s += d[i] + d[i + 1] + d[i + 2];
    return Math.round(s / (d.length / 4) / 3);
  } catch { return null; }
}

async function waitFrames(video) {
  const t0 = performance.now();
  while (performance.now() - t0 < 6000) {
    if (video.error) throw new Error(`MediaError ${video.error.code} ${video.error.message || ''}`);
    const q = video.getVideoPlaybackQuality ? video.getVideoPlaybackQuality() : null;
    const frames = q ? q.totalVideoFrames : null;
    if (video.videoWidth > 0 && video.currentTime > 0.5 && (frames == null || frames >= 5)) {
      return { ok: true, currentTime: +video.currentTime.toFixed(2), frames, dropped: q?.droppedVideoFrames ?? null, size: `${video.videoWidth}x${video.videoHeight}`, brightness: brightness(video) };
    }
    await sleep(100);
  }
  return { ok: false, error: `6 秒で再生が進まない(currentTime=${video.currentTime.toFixed(2)}, videoWidth=${video.videoWidth}, readyState=${video.readyState})` };
}

function newVideo(title) {
  const v = document.createElement('video');
  v.muted = true; v.playsInline = true; v.title = title; v.disableRemotePlayback = true;
  $('videos').appendChild(v);
  return v;
}

async function playMse(m) {
  const MS = window.MediaSource || window.ManagedMediaSource;
  if (!MS) return { ok: false, error: 'MediaSource / ManagedMediaSource がない' };
  const mime = `video/mp4; codecs="${m.codec}"`;
  if (!MS.isTypeSupported(mime)) return { ok: false, error: `isTypeSupported(${mime}) = false` };
  const buf = await (await fetch(m.url)).arrayBuffer();
  const video = newVideo(`MSE ${m.label}`);
  const ms = new MS();
  video.src = URL.createObjectURL(ms);
  try {
    await withTimeout(new Promise((r) => ms.addEventListener('sourceopen', r, { once: true })), 5000, 'sourceopen');
    const sb = ms.addSourceBuffer(mime);
    await withTimeout(new Promise((res, rej) => { sb.addEventListener('updateend', res, { once: true }); sb.addEventListener('error', () => rej(new Error('SourceBuffer error')), { once: true }); sb.appendBuffer(buf); }), 5000, 'appendBuffer');
    ms.endOfStream();
    video.play().catch(() => {});
    return await waitFrames(video);
  } catch (err) {
    return { ok: false, error: String(err.message || err) + (video.error ? ` / MediaError ${video.error.code} ${video.error.message || ''}` : '') };
  }
}

async function playFile(m) {
  const video = newVideo(`src= ${m.label}`);
  video.src = m.url;
  video.play().catch(() => {});
  try { return await waitFrames(video); } catch (err) { return { ok: false, error: String(err.message || err) }; }
}

// WebCodecs の VideoDecoder(Chrome では WebRTC と同じハードウェアデコーダの経路。参考値)
async function webCodecs() {
  if (!window.VideoDecoder?.isConfigSupported) return [];
  const out = [];
  for (const codec of ['hvc1.1.6.L93.90', 'hvc1.2.4.L120.90', 'avc1.42E01E']) {
    for (const hardwareAcceleration of ['no-preference', 'prefer-hardware', 'prefer-software']) {
      try {
        const r = await VideoDecoder.isConfigSupported({ codec, codedWidth: 1280, codedHeight: 720, hardwareAcceleration });
        out.push({ codec, hardwareAcceleration, supported: r.supported });
      } catch (err) { out.push({ codec, hardwareAcceleration, error: String(err) }); }
    }
  }
  return out;
}

async function rtc() {
  const r = { receiverH265: [], senderH265: [], loopback: null };
  if (!window.RTCRtpReceiver?.getCapabilities) { r.error = 'RTCRtpReceiver.getCapabilities がない'; return r; }
  const rc = RTCRtpReceiver.getCapabilities('video')?.codecs || [];
  const sc = RTCRtpSender.getCapabilities?.('video')?.codecs || [];
  r.receiverCodecs = [...new Set(rc.map((c) => c.mimeType))];
  r.receiverH265 = rc.filter((c) => /h265/i.test(c.mimeType)).map((c) => c.sdpFmtpLine || '(fmtp なし)');
  r.senderH265 = sc.filter((c) => /h265/i.test(c.mimeType)).map((c) => c.sdpFmtpLine || '(fmtp なし)');
  if (!r.senderH265.length || !r.receiverH265.length) {
    r.loopback = { ok: null, note: '送信側に H.265 エンコーダがない(または受信不可)ので折り返し試験はしない' };
    return r;
  }
  // 同じページ内で H.265 だけを載せた折り返し接続を作り、受信側で実際にデコードできたかを見る
  const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 360;
  const ctx = canvas.getContext('2d'); let n = 0;
  const timer = setInterval(() => { ctx.fillStyle = `hsl(${(n += 7) % 360} 70% 50%)`; ctx.fillRect(0, 0, 640, 360); ctx.fillStyle = '#fff'; ctx.font = '48px sans-serif'; ctx.fillText(String(n), 20, 200); }, 66);
  const pc1 = new RTCPeerConnection(); const pc2 = new RTCPeerConnection();
  try {
    pc1.onicecandidate = (e) => e.candidate && pc2.addIceCandidate(e.candidate);
    pc2.onicecandidate = (e) => e.candidate && pc1.addIceCandidate(e.candidate);
    const track = canvas.captureStream(15).getVideoTracks()[0];
    const tr = pc1.addTransceiver(track, { direction: 'sendonly' });
    const prefs = sc.filter((c) => /h265|rtx/i.test(c.mimeType));
    try { tr.setCodecPreferences(prefs); } catch (err) { r.setCodecPreferencesError = String(err); }
    const video = newVideo('WebRTC H.265 折り返し');
    pc2.ontrack = (e) => { video.srcObject = new MediaStream([e.track]); video.play().catch(() => {}); };
    await pc1.setLocalDescription(); await pc2.setRemoteDescription(pc1.localDescription);
    await pc2.setLocalDescription(); await pc1.setRemoteDescription(pc2.localDescription);
    await sleep(4000);
    let outbound = null; (await pc1.getStats()).forEach((s) => { if (s.type === 'outbound-rtp' && s.kind === 'video') outbound = s; });
    const stats = await pc2.getStats(); let inbound = null; const codecs = {};
    stats.forEach((s) => { if (s.type === 'inbound-rtp' && s.kind === 'video') inbound = s; if (s.type === 'codec') codecs[s.id] = s; });
    const enc = { framesEncoded: outbound?.framesEncoded ?? null, encoderImplementation: outbound?.encoderImplementation };
    if (!outbound?.framesEncoded) {
      // 送信側(このブラウザのエンコーダ)がフレームを出さないときは、受信(デコード)の可否は分からない
      r.loopback = { ok: null, note: '送信側のエンコーダがフレームを出さなかった(受信側の可否は不明)', ...enc };
    } else r.loopback = inbound ? {
      ok: (inbound.framesDecoded || 0) > 0 && /h265/i.test(codecs[inbound.codecId]?.mimeType || ''),
      codec: codecs[inbound.codecId]?.mimeType, framesDecoded: inbound.framesDecoded, decoderImplementation: inbound.decoderImplementation,
      powerEfficientDecoder: inbound.powerEfficientDecoder, size: `${inbound.frameWidth}x${inbound.frameHeight}`, ...enc,
    } : { ok: false, note: 'inbound-rtp が出ない', ...enc };
  } catch (err) {
    r.loopback = { ok: false, error: String(err) };
  } finally { clearInterval(timer); pc1.close(); pc2.close(); }
  return r;
}

function summarize(res) {
  const mse = res.play.find((p) => p.id === 'hevc-main' && p.via === 'MSE')?.ok === true;
  const file = res.play.find((p) => p.id === 'hevc-main' && p.via === 'src=')?.ok === true;
  const hlsNative = /maybe|probably/.test(res.types.find((t) => t.type === 'application/vnd.apple.mpegurl')?.canPlayType || '');
  const rec = mse || (hlsNative && file);
  const recv = res.rtc.receiverH265.length > 0;
  const lb = res.rtc.loopback?.ok;
  return {
    recording: rec,
    recordingNote: mse ? 'MSE(hls.js)で H.265 の fMP4 を再生できた' : hlsNative && file ? 'MSE は不可だがネイティブ HLS / src= で再生できた(Safari 型)' : 'H.265 の fMP4 を再生できない',
    webrtc: recv,
    webrtcNote: recv ? (lb === true ? '受信能力あり。折り返しで実際にデコードできた' : lb === false ? '受信能力はあるが折り返しでデコードできなかった' : '受信能力あり(折り返しでは確かめられず。詳細は下の WebRTC 表)') : 'RTCRtpReceiver に H.265 がない → ライブは H.264(sub)',
  };
}

function render(res) {
  const s = res.summary;
  $('summary').innerHTML =
    `<div class="card">録画 / HLS(H.265)<b class="${s.recording ? 'ok' : 'ng'}">${s.recording ? '再生できる' : '再生できない'}</b><span class="muted">${esc(s.recordingNote)}</span></div>` +
    `<div class="card">ライブ WebRTC(H.265)<b class="${s.webrtc ? 'ok' : 'ng'}">${s.webrtc ? '受信できる' : '受信できない'}</b><span class="muted">${esc(s.webrtcNote)}</span></div>`;
  table($('env'), ['項目', '値'], Object.entries(res.env).map(([k, v]) => [esc(k), `<code>${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</code>`]));
  table($('play'), ['経路', '素材', '結果', '詳細'], res.play.map((p) => [esc(p.via), esc(p.label), mark(p.ok), `<code>${esc(p.ok ? `frames=${p.frames} t=${p.currentTime}s ${p.size} 明るさ=${p.brightness}` : p.error)}</code>`]));
  table($('types'), ['type', 'isTypeSupported', 'canPlayType'], res.types.map((t) => [`<code>${esc(t.type)}</code>`, mark(t.isTypeSupported), `<code>${esc(t.canPlayType)}</code>`]));
  const mcCell = (o) => (o?.error ? `<code>${esc(o.error)}</code>` : `${mark(o?.supported)} smooth=${mark(o?.smooth)} 省電力=${mark(o?.powerEfficient)}`);
  table($('mc'), ['条件', 'media-source', 'file'], res.mediaCapabilities.map((m) => [esc(m.label), mcCell(m['media-source']), mcCell(m.file)]));
  table($('wc'), ['codec', 'hardwareAcceleration', 'supported'], res.webCodecs.map((w) => [`<code>${esc(w.codec)}</code>`, esc(w.hardwareAcceleration), w.error ? `<code>${esc(w.error)}</code>` : mark(w.supported)]));
  const r = res.rtc;
  table($('rtc'), ['項目', '値'], [
    ['受信 H.265(fmtp)', `<code>${esc(r.receiverH265.join(' | ') || 'なし')}</code>`],
    ['送信 H.265(fmtp)', `<code>${esc(r.senderH265.join(' | ') || 'なし')}</code>`],
    ['受信コーデック', `<code>${esc((r.receiverCodecs || []).join(', '))}</code>`],
    ['折り返し試験', `<code>${esc(JSON.stringify(r.loopback))}</code>`],
  ]);
  $('json').textContent = JSON.stringify(res, null, 2);
}

$('copy').onclick = () => navigator.clipboard.writeText($('json').textContent);

(async () => {
  const res = { checkedAt: new Date().toISOString(), env: await env(), types: types(), mediaCapabilities: await mediaCaps(), webCodecs: await webCodecs(), play: [], rtc: null };
  for (const m of MEDIA) {
    res.play.push({ via: 'MSE', id: m.id, label: m.label, ...(await playMse(m)) });
    res.play.push({ via: 'src=', id: m.id, label: m.label, ...(await playFile(m)) });
  }
  try { res.rtc = await rtc(); } catch (err) { res.rtc = { receiverH265: [], senderH265: [], error: String(err) }; }
  res.summary = summarize(res);
  render(res);
  window.__result = res;
  window.__done = true;
})().catch((err) => { window.__result = { fatal: String(err && err.stack || err) }; window.__done = true; $('summary').textContent = `エラー: ${err}`; });

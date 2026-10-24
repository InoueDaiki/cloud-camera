// プレイヤーの中核。位置は常に「絶対時刻(epoch ミリ秒)」で扱い、
// 再生元(ライブ / KVS / S3)の切り替えと窓の読み直しはここで隠す。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Hls, { type Fragment } from 'hls.js';
import { api } from '../api/client';
import { detectSupport } from '../lib/codec';
import {
  playbackWindow,
  windowCovers,
  type PlaybackTarget,
  type PlaybackWindow,
  type SourceKind,
} from '../lib/playbackSource';

export type PlayerStatus = 'idle' | 'loading' | 'playing' | 'paused' | 'buffering' | 'error' | 'no-live';

export interface PlayerError {
  kind: 'codec' | 'network' | 'media' | 'api';
  message: string;
}

interface Session {
  cameraId: string;
  win: PlaybackWindow;
  hls: Hls | null;
  frags: Fragment[];
  /** PROGRAM-DATE-TIME がないときの、currentTime 0 に対応する時刻 */
  base: number;
  pendingSeek: number | null;
  /** 同じ窓を何度も読み直さないための印 */
  id: number;
}

interface Request {
  cameraId: string | null;
  target: PlaybackTarget;
  n: number;
}

const isCodecError = (details: string) =>
  /codec|incompatible/i.test(details) || details === Hls.ErrorDetails.BUFFER_ADD_CODEC_ERROR;

export function usePlayback(processedUntil: (cameraId: string) => number | null) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const support = useMemo(() => detectSupport(), []);
  const [videoEl, setVideoEl] = useState<HTMLVideoElement | null>(null);
  const attachVideo = useCallback((el: HTMLVideoElement | null) => {
    videoRef.current = el;
    setVideoEl(el);
  }, []);
  const [request, setRequest] = useState<Request>({ cameraId: null, target: { kind: 'live' }, n: 0 });
  const [position, setPosition] = useState<number | null>(null);
  const [source, setSource] = useState<SourceKind | null>(null);
  const [status, setStatus] = useState<PlayerStatus>('idle');
  const [error, setError] = useState<PlayerError | null>(null);
  const [rate, setRateState] = useState(1);
  const sessionRef = useRef<Session | null>(null);
  const processedRef = useRef(processedUntil);
  const rateRef = useRef(1);
  const sessionSeq = useRef(0);

  useEffect(() => {
    processedRef.current = processedUntil;
  }, [processedUntil]);

  const open = useCallback((cameraId: string, target: PlaybackTarget) => {
    setRequest((r) => ({ cameraId, target, n: r.n + 1 }));
  }, []);

  // ---- 位置の変換 ----

  const absoluteTime = useCallback((s: Session): number | null => {
    const video = videoRef.current;
    if (!video) return null;
    if (s.hls) {
      const d = s.hls.playingDate;
      if (d) return d.getTime();
    } else {
      const start = (video as HTMLVideoElement & { getStartDate?: () => Date }).getStartDate?.();
      if (start && !Number.isNaN(start.getTime())) return start.getTime() + video.currentTime * 1000;
    }
    if (s.win.kind === 'live') return Date.now();
    return s.base + video.currentTime * 1000;
  }, []);

  /** 絶対時刻 → video.currentTime。録画の隙間なら次の断片の頭 */
  const mediaTimeFor = useCallback((s: Session, at: number): number => {
    const frags = s.frags.filter((f) => f.programDateTime !== null);
    if (frags.length) {
      for (const f of frags) {
        const pdt = f.programDateTime!;
        if (at < pdt) return f.start;
        if (at < pdt + f.duration * 1000) return f.start + (at - pdt) / 1000;
      }
      const last = frags[frags.length - 1];
      return last.start + last.duration - 0.5;
    }
    return Math.max(0, (at - s.base) / 1000);
  }, []);

  // ---- 読み込み ----

  useEffect(() => {
    const { cameraId, target } = request;
    const video = videoRef.current;
    if (!cameraId || !video) return;
    let cancelled = false;

    const advance = (s: Session) => {
      // 窓の終わりまで来たら次の窓へ。現在に追いついたらライブへ
      if (s.win.kind === 'live') return;
      const next = s.win.to ?? Date.now();
      setRequest((r) =>
        r.cameraId === s.cameraId
          ? { cameraId: s.cameraId, target: s.win.kind === 'kvs' ? { kind: 'live' } : { kind: 'time', at: next }, n: r.n + 1 }
          : r,
      );
    };

    const run = async () => {
      const now = Date.now();
      const win = playbackWindow(target, processedRef.current(cameraId), now);
      const current = sessionRef.current;

      // 読み込み済みの窓の中なら、その場でシーク
      if (current && current.cameraId === cameraId && target.kind === 'time' && windowCovers(current.win, win.kind, target.at)) {
        video.currentTime = mediaTimeFor(current, target.at);
        setPosition(target.at);
        void video.play().catch(() => undefined);
        return;
      }

      destroySession(sessionRef.current, video);
      sessionRef.current = null;
      setStatus('loading');
      setSource(win.kind);
      setError(null);
      setPosition(target.kind === 'time' ? target.at : now);

      let url: string;
      try {
        if (win.kind === 's3') url = api.vodUrl(cameraId, win.from!, win.to!);
        else if (win.kind === 'kvs') url = (await api.kvsHls(cameraId, 'ON_DEMAND', win.from!, win.to!)).url;
        else url = (await api.kvsHls(cameraId, 'LIVE')).url;
      } catch (e) {
        if (cancelled) return;
        if (win.kind === 'live') {
          setStatus('no-live');
        } else {
          setStatus('error');
          setError({ kind: 'api', message: e instanceof Error ? e.message : String(e) });
        }
        return;
      }
      if (cancelled) return;

      const s: Session = {
        cameraId,
        win,
        hls: null,
        frags: [],
        base: win.from ?? now,
        pendingSeek: target.kind === 'time' ? target.at : null,
        id: ++sessionSeq.current,
      };
      sessionRef.current = s;

      if (support.engine === 'native') {
        video.src = url;
        video.addEventListener(
          'loadedmetadata',
          () => {
            if (s.pendingSeek !== null) video.currentTime = Math.max(0, (s.pendingSeek - s.base) / 1000);
            s.pendingSeek = null;
            video.playbackRate = rateRef.current;
            void video.play().catch(() => undefined);
          },
          { once: true },
        );
        return;
      }

      const hls = new Hls({
        autoStartLoad: false,
        liveSyncDurationCount: 2,
        maxBufferLength: 30,
        backBufferLength: 60,
      });
      s.hls = hls;
      let started = false;
      const startAt = () => {
        if (started) return;
        started = true;
        if (s.pendingSeek !== null && s.frags.length) {
          const pos = mediaTimeFor(s, s.pendingSeek);
          hls.startLoad(pos);
          s.pendingSeek = null;
          // 前のセッション(ライブなど)の currentTime が残って開始位置がずれることがあるので、
          // 最初の断片が入った時点でずれていれば合わせ直す
          hls.once(Hls.Events.FRAG_BUFFERED, () => {
            if (sessionRef.current?.id === s.id && Math.abs(video.currentTime - pos) > 2) video.currentTime = pos;
          });
        } else {
          hls.startLoad(-1);
        }
        video.playbackRate = rateRef.current;
        void video.play().catch(() => undefined);
      };
      hls.on(Hls.Events.LEVEL_LOADED, (_e, data) => {
        if (sessionRef.current?.id !== s.id) return;
        s.frags = data.details.fragments;
        if (!data.details.live && s.frags.length === 0) {
          advance(s);
          return;
        }
        if (!started) startAt();
        else if (s.pendingSeek !== null) {
          video.currentTime = mediaTimeFor(s, s.pendingSeek);
          s.pendingSeek = null;
        }
      });
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        // メディアプレイリストなら直後に LEVEL_LOADED が来る。来なければマルチバリアント(KVS など)で、
        // 読み込みを始めないとレベルが読まれないので、いったん先頭から始める(位置は LEVEL_LOADED で合わせ直す)
        window.setTimeout(() => {
          if (sessionRef.current?.id === s.id && !started && s.frags.length === 0) hls.startLoad(-1);
        }, 0);
      });
      hls.on(Hls.Events.ERROR, (_e, d) => {
        if (sessionRef.current?.id !== s.id || !d.fatal) return;
        if (d.details === Hls.ErrorDetails.LEVEL_EMPTY_ERROR) {
          advance(s);
          return;
        }
        if (isCodecError(d.details)) {
          setError({ kind: 'codec', message: 'このブラウザはこの映像の形式(H.265 など)を再生できません。' });
        } else if (d.type === Hls.ErrorTypes.NETWORK_ERROR) {
          if (s.win.kind === 'live') {
            setStatus('no-live');
            return;
          }
          setError({ kind: 'network', message: `映像を取得できませんでした(${d.details})` });
        } else {
          setError({ kind: 'media', message: `映像を再生できませんでした(${d.details})` });
        }
        setStatus('error');
      });
      hls.loadSource(url);
      hls.attachMedia(video);
    };

    void run();
    return () => {
      cancelled = true;
    };
  }, [request, videoEl, support.engine, mediaTimeFor]);

  // ---- video 要素のイベント ----

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const onTime = () => {
      const s = sessionRef.current;
      if (!s) return;
      const t = absoluteTime(s);
      if (t !== null) setPosition(t);
    };
    const onPlaying = () => setStatus('playing');
    const onPause = () => setStatus((st) => (st === 'error' || st === 'no-live' ? st : 'paused'));
    const onWaiting = () => setStatus((st) => (st === 'playing' ? 'buffering' : st));
    const onEnded = () => {
      const s = sessionRef.current;
      if (!s || s.win.kind === 'live') return;
      const next = s.win.to ?? Date.now();
      setRequest((r) => ({
        cameraId: s.cameraId,
        target: s.win.kind === 'kvs' ? { kind: 'live' } : { kind: 'time', at: next },
        n: r.n + 1,
      }));
    };
    const onError = () => {
      if (sessionRef.current?.hls) return; // hls.js 側で扱う
      const code = video.error?.code;
      setStatus('error');
      setError(
        code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED || code === MediaError.MEDIA_ERR_DECODE
          ? { kind: 'codec', message: 'このブラウザはこの映像の形式(H.265 など)を再生できません。' }
          : { kind: 'network', message: '映像を取得できませんでした。' },
      );
    };
    video.addEventListener('timeupdate', onTime);
    video.addEventListener('playing', onPlaying);
    video.addEventListener('pause', onPause);
    video.addEventListener('waiting', onWaiting);
    video.addEventListener('ended', onEnded);
    video.addEventListener('error', onError);
    return () => {
      video.removeEventListener('timeupdate', onTime);
      video.removeEventListener('playing', onPlaying);
      video.removeEventListener('pause', onPause);
      video.removeEventListener('waiting', onWaiting);
      video.removeEventListener('ended', onEnded);
      video.removeEventListener('error', onError);
    };
  }, [videoEl, absoluteTime]);

  // ライブ中は映像が止まっていても時計を進める(timeupdate が来ない間の表示用)
  useEffect(() => {
    if (source !== 'live') return;
    const id = window.setInterval(() => {
      const s = sessionRef.current;
      if (!s) return;
      const t = absoluteTime(s);
      setPosition(t ?? Date.now());
    }, 1000);
    return () => window.clearInterval(id);
  }, [source, absoluteTime]);

  useEffect(
    () => () => {
      destroySession(sessionRef.current, videoRef.current);
      sessionRef.current = null;
    },
    [],
  );

  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) void video.play().catch(() => undefined);
    else video.pause();
  }, []);

  const setRate = useCallback((r: number) => {
    rateRef.current = r;
    setRateState(r);
    if (videoRef.current) videoRef.current.playbackRate = r;
  }, []);

  const retry = useCallback(() => setRequest((r) => ({ ...r, n: r.n + 1 })), []);

  return {
    /** <video ref={...}> に渡す */
    attachVideo,
    support,
    cameraId: request.cameraId,
    target: request.target,
    position,
    source,
    status,
    error,
    rate,
    open,
    togglePlay,
    setRate,
    retry,
  };
}

function destroySession(s: Session | null, video: HTMLVideoElement | null) {
  if (!s) return;
  s.hls?.destroy();
  if (!s.hls && video) {
    video.removeAttribute('src');
    video.load();
  }
}

export type Playback = ReturnType<typeof usePlayback>;

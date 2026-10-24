// 1 日分のシークバー。録画あり(S3 / KVS)、録画なし、FAILED を色分けし、ホバーでスプライトのコマを出す。

import { useMemo, useRef, useState, type PointerEvent } from 'react';
import { ActionIcon, Group, Skeleton, Text, Tooltip } from '@mantine/core';
import { useElementSize } from '@mantine/hooks';
import { IconChevronLeft, IconChevronRight } from '@tabler/icons-react';
import {
  findInterval,
  layoutBars,
  makeTicks,
  ratioToTime,
  timeToRatio,
  viewAround,
  zoomSpan,
  type TimelineData,
  type View,
  type ZoomValue,
} from '../lib/timeline';
import { spriteFrameAt, spriteStyle } from '../lib/sprite';
import { formatHms } from '../lib/time';
import classes from './Timeline.module.css';

const PREVIEW_WIDTH = 160;

interface Props {
  day: View;
  data: TimelineData | undefined;
  loading: boolean;
  position: number | null;
  now: number;
  zoom: ZoomValue;
  onSeek: (t: number) => void;
}

export function Timeline({ day, data, loading, position, now, zoom, onSeek }: Props) {
  const span = zoomSpan(zoom);
  // ズーム中は再生位置を中心に表示する。現在に近いときは未来側を広く取りすぎないよう右に寄せる
  const center = Math.min(position ?? now, now - span * 0.35, day.end);
  const viewKey = `${span}:${day.start}`;
  const [pan, setPan] = useState<{ key: string; center: number } | null>(null);
  const pannedCenter = pan && pan.key === viewKey ? pan.center : null;
  const view = useMemo(() => viewAround(pannedCenter ?? center, span, day), [pannedCenter, center, span, day]);
  const { ref: sizeRef, width } = useElementSize();
  const trackRef = useRef<HTMLDivElement | null>(null);
  const [hover, setHover] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const seek = (t: number) => {
    setPan(null);
    onSeek(t);
  };

  const narrow = width > 0 && width < 520;
  const ticks = useMemo(() => makeTicks(view, narrow), [view, narrow]);
  const minWidth = width > 0 ? (2 / width) * 100 : 0.2;
  const bars = useMemo(() => {
    if (!data) return [];
    return [
      ...layoutBars(data.recorded.filter((r) => r.source === 's3'), 's3', view),
      ...layoutBars(data.recorded.filter((r) => r.source === 'kvs'), 'kvs', view),
      ...layoutBars(data.failed, 'failed', view, minWidth),
    ];
  }, [data, view, minWidth]);

  const futureLeft = Math.max(0, Math.min(100, timeToRatio(view, now) * 100));
  const timeAt = (clientX: number) => {
    const rect = trackRef.current!.getBoundingClientRect();
    return ratioToTime(view, (clientX - rect.left) / rect.width);
  };

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    setDragging(true);
    setHover(timeAt(e.clientX));
  };
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    if (e.pointerType === 'mouse' || dragging) setHover(timeAt(e.clientX));
  };
  const onPointerUp = (e: PointerEvent<HTMLDivElement>) => {
    if (!dragging) return;
    setDragging(false);
    const t = timeAt(e.clientX);
    if (e.pointerType !== 'mouse') setHover(null);
    seek(Math.min(t, now));
  };

  const panBy = (dir: -1 | 1) => {
    const c = viewAround((view.start + view.end) / 2 + dir * span * 0.75, span, day);
    setPan({ key: viewKey, center: (c.start + c.end) / 2 });
  };
  const zoomed = span < day.end - day.start;

  const hoverRatio = hover !== null ? timeToRatio(view, hover) : null;
  const frame = hover !== null && data ? spriteFrameAt(data.minutes, hover) : null;
  const hoverState =
    hover === null || !data
      ? null
      : hover > now
        ? '未来'
        : findInterval(data.failed, hover)
          ? '処理失敗(FAILED)'
          : (() => {
              const r = data.recorded.find((x) => hover >= x.start && hover < x.end);
              return r ? (r.source === 'kvs' ? '未退避(KVS)' : null) : '録画なし';
            })();
  const previewLeft =
    hoverRatio !== null && width > 0
      ? Math.max(0, Math.min(width - PREVIEW_WIDTH - 10, hoverRatio * width - PREVIEW_WIDTH / 2 - 4))
      : 0;

  return (
    <Group gap={4} wrap="nowrap" align="flex-start">
      {zoomed && (
        <Tooltip label="前へ">
          <ActionIcon aria-label="表示を前へ送る" onClick={() => panBy(-1)} disabled={view.start <= day.start} mt={2}>
            <IconChevronLeft size={18} />
          </ActionIcon>
        </Tooltip>
      )}
      <div className={classes.root} ref={sizeRef} style={{ flex: 1, minWidth: 0 }}>
        {hover !== null && hoverRatio !== null && hoverRatio >= 0 && hoverRatio <= 1 && (
          <div className={classes.preview} style={{ left: previewLeft }}>
            {frame && !hoverState ? (
              <div className={classes.thumb} style={spriteStyle(frame, PREVIEW_WIDTH)} />
            ) : (
              <div className={classes.noThumb} style={{ width: PREVIEW_WIDTH, height: (PREVIEW_WIDTH * 9) / 16 }}>
                {hoverState ?? 'サムネイルなし'}
              </div>
            )}
            <Text size="xs" ta="center" mt={2} className="tabular" fw={500}>
              {formatHms(hover)}
            </Text>
          </div>
        )}
        <div
          ref={trackRef}
          className={classes.track}
          role="slider"
          aria-label="再生位置"
          aria-valuemin={view.start}
          aria-valuemax={view.end}
          aria-valuenow={position ?? undefined}
          aria-valuetext={position ? formatHms(position) : undefined}
          tabIndex={0}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={() => {
            setDragging(false);
            setHover(null);
          }}
          onPointerLeave={(e) => e.pointerType === 'mouse' && !dragging && setHover(null)}
          onKeyDown={(e) => {
            if (position === null) return;
            const step = e.shiftKey ? 60_000 : 10_000;
            if (e.key === 'ArrowLeft') seek(position - step);
            if (e.key === 'ArrowRight') seek(Math.min(position + step, now));
          }}
        >
          {loading && !data && <Skeleton height="100%" radius={0} />}
          {futureLeft < 100 && <div className={classes.future} style={{ left: `${futureLeft}%`, right: 0 }} />}
          {bars.map((b, i) => (
            <div
              key={`${b.kind}-${i}`}
              className={`${classes.bar} ${classes[b.kind]}`}
              style={{ left: `${b.left}%`, width: `${b.width}%` }}
            />
          ))}
          {hoverRatio !== null && hoverRatio >= 0 && hoverRatio <= 1 && (
            <div className={classes.hoverLine} style={{ left: `${hoverRatio * 100}%` }} />
          )}
        </div>
        {position !== null && position >= view.start && position <= view.end && (
          <div
            className={classes.playhead}
            style={{ left: `${timeToRatio(view, position) * 100}%`, top: -4, height: 36 }}
          />
        )}
        <div className={classes.ticks}>
          {ticks.map((t) => (
            <span key={t.at} className={`${classes.tick} tabular`} style={{ left: `${t.left}%` }}>
              {t.label}
            </span>
          ))}
        </div>
      </div>
      {zoomed && (
        <Tooltip label="次へ">
          <ActionIcon aria-label="表示を次へ送る" onClick={() => panBy(1)} disabled={view.end >= day.end} mt={2}>
            <IconChevronRight size={18} />
          </ActionIcon>
        </Tooltip>
      )}
    </Group>
  );
}

export function TimelineLegend() {
  const items = [
    ['var(--cc-rec-s3)', '録画'],
    ['var(--cc-rec-kvs)', '未退避(KVS)'],
    ['var(--cc-rec-failed)', '処理失敗'],
    ['var(--cc-track)', '録画なし'],
  ] as const;
  return (
    <div className={classes.legend}>
      {items.map(([color, label]) => (
        <span key={label}>
          <span className={classes.swatch} style={{ background: color, border: '1px solid var(--cc-border)' }} />
          {label}
        </span>
      ))}
    </div>
  );
}

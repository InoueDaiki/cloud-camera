import { useEffect, useMemo, useState } from 'react';
import {
  ActionIcon,
  Alert,
  Button,
  Group,
  Loader,
  Menu,
  SegmentedControl,
  Stack,
  Text,
  Tooltip,
} from '@mantine/core';
import { DatePickerInput } from '@mantine/dates';
import { useLocalStorage } from '@mantine/hooks';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  IconAlertTriangle,
  IconCalendar,
  IconChevronLeft,
  IconChevronRight,
  IconPlayerPauseFilled,
  IconPlayerPlayFilled,
  IconPlayerSkipBackFilled,
  IconPlayerSkipForwardFilled,
  IconRefresh,
  IconVideoOff,
} from '@tabler/icons-react';
import { api } from '../api/client';
import type { Camera } from '../api/types';
import { LIVE_THRESHOLD_MS, SOURCE_LABEL } from '../lib/playbackSource';
import {
  nextRecordingStart,
  prevRecordingStart,
  snapToRecording,
  toTimelineData,
  ZOOMS,
  type ZoomValue,
} from '../lib/timeline';
import { addDays, DAY, formatDateTime, formatHm, jstDate, jstDayStart } from '../lib/time';
import type { Playback } from './usePlayback';
import { Timeline, TimelineLegend } from './Timeline';
import classes from './Player.module.css';

interface Props {
  pb: Playback;
  camera: Camera | undefined;
  now: number;
}

const RATES = [1, 2, 4, 8];

export function Player({ pb, camera, now }: Props) {
  const { attachVideo, cameraId, open, position: pos, source, status, error, support, rate, togglePlay, setRate, retry } = pb;
  const qc = useQueryClient();
  const [zoom, setZoom] = useState<ZoomValue>('24h');
  const [pendingDate, setPendingDate] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [hevcDismissed, setHevcDismissed] = useLocalStorage({ key: 'cc-hevc-dismissed', defaultValue: false });

  const today = jstDate(now);
  const isLive = source === 'live';
  const positionDate = pos !== null ? jstDate(pos) : today;
  const viewDate = pendingDate ?? positionDate;
  const dayStart = jstDayStart(viewDate);
  const day = useMemo(() => ({ start: dayStart, end: dayStart + DAY }), [dayStart]);

  const timeline = useQuery({
    queryKey: ['timeline', cameraId, viewDate],
    queryFn: () => api.timeline(cameraId!, day.start, day.end),
    select: toTimelineData,
    enabled: !!cameraId,
    refetchInterval: viewDate === today ? 30_000 : false,
  });
  const data = timeline.data;

  useEffect(() => {
    if (!notice) return;
    const id = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(id);
  }, [notice]);

  if (!cameraId) return null;

  const goLive = () => {
    setPendingDate(null);
    open(cameraId, { kind: 'live' });
  };

  const seek = (t: number) => {
    setPendingDate(null);
    if (t >= Date.now() - LIVE_THRESHOLD_MS) return goLive();
    const snapped = data ? snapToRecording(data.merged, t) : t;
    if (snapped === null) {
      if (viewDate === today) return goLive();
      setNotice('この時刻より後に、この日の録画はありません');
      return;
    }
    if (snapped !== t) setNotice('録画がない時間のため、次の録画から再生します');
    open(cameraId, { kind: 'time', at: snapped });
  };

  // 日付を選んだら、その日の最初の録画から再生する。録画がなければその日のシークバーだけ出す
  const selectDate = async (date: string | null) => {
    if (!date) return;
    if (date >= today) return goLive();
    setPendingDate(date);
    const start = jstDayStart(date);
    try {
      const res = await qc.fetchQuery({
        queryKey: ['timeline', cameraId, date],
        queryFn: () => api.timeline(cameraId, start, start + DAY),
      });
      const first = toTimelineData(res).merged[0]?.start;
      if (first !== undefined) {
        setPendingDate(null);
        open(cameraId, { kind: 'time', at: first });
      } else {
        setNotice('この日の録画はありません');
      }
    } catch {
      // 失敗はシークバーの下にエラーとして出る
    }
  };

  const prev = data && pos !== null ? prevRecordingStart(data.merged, pos) : null;
  const next = data && pos !== null && !isLive ? nextRecordingStart(data.merged, pos) : null;
  const processedUntil = data?.videoProcessedUntil ?? null;
  const playing = status === 'playing' || status === 'buffering';

  return (
    <Stack gap="sm">
      {!support.hevc && !hevcDismissed && (
        <Alert
          color="yellow"
          variant="light"
          icon={<IconAlertTriangle size={18} />}
          title="H.265 を再生できない可能性があります"
          withCloseButton
          closeButtonLabel="閉じる"
          onClose={() => setHevcDismissed(true)}
          p="sm"
        >
          映像は H.265(HEVC)です。Safari か、H.265 対応端末の Chrome / Edge 最新版で開いてください(Firefox は非対応)。
          {import.meta.env.VITE_MOCK === 'true' && 'モックの映像は H.264 なので、このまま再生できます。'}
        </Alert>
      )}

      <div className={classes.stage}>
        <video ref={attachVideo} className={classes.video} muted playsInline autoPlay />
        <div className={classes.overlayTop}>
          <div>
            <div className={classes.camName}>{camera?.name ?? cameraId}</div>
            <div className={`${classes.clock} tabular`}>{pos !== null ? formatDateTime(pos) : '—'}</div>
          </div>
          {source && (
            <span className={classes.sourceChip} title="再生元">
              {isLive && <span className={classes.liveDot} />}
              {SOURCE_LABEL[source]}
            </span>
          )}
        </div>
        {status === 'loading' && (
          <div className={classes.center} style={{ background: 'transparent' }}>
            <Loader color="gray.0" size="md" />
          </div>
        )}
        {status === 'no-live' && (
          <div className={classes.center}>
            <IconVideoOff size={36} />
            <div className={classes.centerText}>
              ライブ映像がありません。
              {camera?.online === false ? 'カメラ(ゲートウェイ)がオフラインです。' : camera && !camera.streaming ? '配信が停止しています。' : ''}
            </div>
            {data && data.merged.length > 0 && (
              <Button size="xs" variant="white" color="dark" onClick={() => seek(data.merged[data.merged.length - 1].start)}>
                最新の録画を見る
              </Button>
            )}
          </div>
        )}
        {status === 'error' && error && (
          <div className={classes.center}>
            <IconAlertTriangle size={36} />
            <div className={classes.centerText}>
              {error.message}
              {error.kind === 'codec' && (
                <>
                  <br />
                  Safari、または H.265 に対応した Chrome / Edge でお試しください。
                </>
              )}
            </div>
            <Button size="xs" variant="white" color="dark" leftSection={<IconRefresh size={14} />} onClick={retry}>
              再試行
            </Button>
          </div>
        )}
        {notice && status !== 'error' && (
          <div className={classes.center} style={{ background: 'transparent', justifyContent: 'flex-end' }}>
            <span className={classes.sourceChip} style={{ fontSize: 'var(--mantine-font-size-sm)', padding: '4px 10px' }}>
              {notice}
            </span>
          </div>
        )}
      </div>

      <Group justify="space-between" gap="xs" wrap="wrap">
        <Group gap={4} wrap="nowrap">
          <Tooltip label={playing ? '一時停止' : '再生'}>
            <ActionIcon size="lg" variant="filled" color="brand" onClick={togglePlay} aria-label={playing ? '一時停止' : '再生'}>
              {playing ? <IconPlayerPauseFilled size={18} /> : <IconPlayerPlayFilled size={18} />}
            </ActionIcon>
          </Tooltip>
          <Tooltip label="前の録画へ">
            <ActionIcon size="lg" onClick={() => prev !== null && seek(prev)} disabled={prev === null} aria-label="前の録画へ">
              <IconPlayerSkipBackFilled size={18} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label="次の録画へ">
            <ActionIcon size="lg" onClick={() => next !== null && seek(next)} disabled={next === null} aria-label="次の録画へ">
              <IconPlayerSkipForwardFilled size={18} />
            </ActionIcon>
          </Tooltip>
          <Menu position="top" withinPortal>
            <Menu.Target>
              <Button variant="subtle" color="gray" size="compact-sm" disabled={isLive} aria-label="再生速度">
                {isLive ? '1' : rate}×
              </Button>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>再生速度</Menu.Label>
              {RATES.map((r) => (
                <Menu.Item key={r} onClick={() => setRate(r)} fw={r === rate ? 600 : undefined}>
                  {r}×
                </Menu.Item>
              ))}
            </Menu.Dropdown>
          </Menu>
        </Group>

        <Group gap="xs" wrap="nowrap">
          <ActionIcon onClick={() => void selectDate(addDays(viewDate, -1))} aria-label="前の日">
            <IconChevronLeft size={18} />
          </ActionIcon>
          <DatePickerInput
            value={viewDate}
            onChange={(v) => void selectDate(v ? String(v).slice(0, 10) : null)}
            maxDate={today}
            valueFormat="M/D(dd)"
            leftSection={<IconCalendar size={16} />}
            size="sm"
            w={128}
            aria-label="日付"
            popoverProps={{ position: 'top' }}
          />
          <ActionIcon onClick={() => void selectDate(addDays(viewDate, 1))} disabled={viewDate >= today} aria-label="次の日">
            <IconChevronRight size={18} />
          </ActionIcon>
          <Button
            size="sm"
            variant={isLive ? 'light' : 'filled'}
            color="red"
            onClick={goLive}
            leftSection={<span className={classes.liveDot} style={{ background: isLive ? undefined : '#fff' }} />}
          >
            ライブ
          </Button>
        </Group>
      </Group>

      <Timeline
        day={day}
        data={data}
        loading={timeline.isLoading}
        position={pendingDate ? null : pos}
        now={now}
        zoom={zoom}
        onSeek={seek}
      />

      <Group justify="space-between" gap="xs">
        <TimelineLegend />
        <Group gap="sm">
          {processedUntil !== null && viewDate === today && (
            <Text size="xs" c="dimmed" className="tabular">
              S3 退避済み {formatHm(processedUntil)} まで
            </Text>
          )}
          <SegmentedControl
            size="xs"
            value={zoom}
            onChange={(v) => setZoom(v as ZoomValue)}
            data={ZOOMS.map((z) => ({ value: z.value, label: z.label }))}
            aria-label="表示の幅"
          />
        </Group>
      </Group>
      {timeline.isError && (
        <Text size="sm" c="red">
          録画の範囲を取得できませんでした: {(timeline.error as Error).message}
        </Text>
      )}
    </Stack>
  );
}

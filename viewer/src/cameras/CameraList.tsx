// カメラ一覧: 状態(オンライン、配信中、処理済み位置の遅れ)と配信の開始・停止

import { Alert, Badge, Group, Skeleton, Stack, Switch, Text, Tooltip, UnstyledButton } from '@mantine/core';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { IconAlertCircle } from '@tabler/icons-react';
import { api } from '../api/client';
import type { Camera, CamerasResponse } from '../api/types';
import { normalizeCodec } from '../lib/codec';
import { formatDuration, formatHm, parseTime } from '../lib/time';
import classes from './CameraList.module.css';

interface Props {
  cameras: Camera[] | undefined;
  loading: boolean;
  error: Error | null;
  selectedId: string | null;
  now: number;
  onSelect: (id: string) => void;
}

/** 処理済み位置の遅れの目安。lag(30〜60 秒)+ 1 分が正常 */
function lagColor(ms: number): string {
  if (ms <= 5 * 60_000) return 'green';
  if (ms <= 15 * 60_000) return 'yellow';
  return 'red';
}

export function CameraList({ cameras, loading, error, selectedId, now, onSelect }: Props) {
  if (loading) {
    return (
      <Stack gap="xs">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} height={76} radius="md" />
        ))}
      </Stack>
    );
  }
  if (error) {
    return (
      <Alert color="red" icon={<IconAlertCircle size={18} />} title="カメラ一覧を取得できませんでした" p="sm">
        {error.message}
      </Alert>
    );
  }
  if (!cameras?.length) return <Text c="dimmed" size="sm">カメラがありません</Text>;

  return (
    <Stack gap="xs">
      {cameras.map((c) => (
        <CameraItem key={c.camera_id} camera={c} selected={c.camera_id === selectedId} now={now} onSelect={onSelect} />
      ))}
    </Stack>
  );
}

function CameraItem({ camera: c, selected, now, onSelect }: { camera: Camera; selected: boolean; now: number; onSelect: (id: string) => void }) {
  const qc = useQueryClient();
  const mutation = useMutation({
    mutationFn: (streaming: boolean) => api.setStreaming(c.camera_id, streaming),
    onMutate: async (streaming) => {
      await qc.cancelQueries({ queryKey: ['cameras'] });
      qc.setQueryData<CamerasResponse>(['cameras'], (old) =>
        old && { cameras: old.cameras.map((x) => (x.camera_id === c.camera_id ? { ...x, desired_streaming: streaming } : x)) },
      );
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['cameras'] }),
  });

  const desired = c.desired_streaming;
  const pendingChange = c.online !== false && desired !== c.streaming;
  const processed = c.video_processed_until ? parseTime(c.video_processed_until) : null;
  const lag = processed !== null ? now - processed : null;

  return (
    <UnstyledButton
      className={classes.item}
      data-selected={selected || undefined}
      onClick={() => onSelect(c.camera_id)}
      aria-current={selected ? 'true' : undefined}
    >
      <Group justify="space-between" wrap="nowrap" gap="xs" align="flex-start">
        <div style={{ minWidth: 0 }}>
          <Group gap={8} wrap="nowrap">
            <span className={classes.dot} data-online={c.online === true || (c.online === null && c.streaming) || undefined} aria-hidden />
            <Text fw={600} size="md" truncate>
              {c.name ?? c.camera_id}
            </Text>
          </Group>
          <Text size="xs" c="dimmed" ml={16}>
            {c.camera_id}
            {c.resolution ? ` · ${c.resolution}` : ''}
            {c.codec ? ` · ${normalizeCodec(c.codec) ?? c.codec}` : ''}
          </Text>
        </div>
        <Tooltip label={desired ? '配信を停止' : '配信を開始'} position="left">
          <Switch
            size="sm"
            checked={desired}
            disabled={mutation.isPending}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => mutation.mutate(e.currentTarget.checked)}
            aria-label={`${c.name ?? c.camera_id} の配信`}
          />
        </Tooltip>
      </Group>
      <Group gap={6} mt={8} ml={16}>
        {c.online === true && <Badge color="green">オンライン</Badge>}
        {c.online === false && (
          <Tooltip label={c.reported_at ? `最終報告 ${formatHm(parseTime(c.reported_at))}` : '報告なし'}>
            <Badge color="gray">オフライン</Badge>
          </Tooltip>
        )}
        {pendingChange ? (
          <Badge color="blue">{desired ? '開始中…' : '停止中…'}</Badge>
        ) : c.streaming ? (
          <Badge color="red" variant="dot">
            配信中
          </Badge>
        ) : (
          <Badge color="gray" variant="outline">
            停止
          </Badge>
        )}
        {lag !== null && (
          <Tooltip label={`S3 への退避は ${formatHm(processed!)} まで完了`}>
            <Badge color={lagColor(lag)} variant="light" className="tabular">
              遅れ {formatDuration(lag)}
            </Badge>
          </Tooltip>
        )}
      </Group>
      {c.error && (
        <Text size="xs" c="red" mt={6} ml={16}>
          {c.error}
        </Text>
      )}
      {mutation.isError && (
        <Text size="xs" c="red" mt={6} ml={16}>
          切り替えに失敗しました: {(mutation.error as Error).message}
        </Text>
      )}
    </UnstyledButton>
  );
}

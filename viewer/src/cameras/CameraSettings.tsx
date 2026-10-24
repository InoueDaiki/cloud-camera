// カメラ設定: 映像形式(H.265 / H.264)の切り替え。配信は動いたまま切り替わり(数秒途切れる)、以降の録画もその形式になる

import { ActionIcon, Badge, Group, Popover, Radio, Stack, Text, Tooltip } from '@mantine/core';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { IconSettings } from '@tabler/icons-react';
import { api } from '../api/client';
import type { Camera, CamerasResponse } from '../api/types';
import { H264_BITRATE_RATIO, isCodecSwitching, normalizeCodec, type VideoCodec } from '../lib/codec';

const API_CODEC: Record<VideoCodec, 'H265' | 'H264'> = { 'H.265': 'H265', 'H.264': 'H264' };

export function CodecSetting({ camera: c }: { camera: Camera }) {
  const qc = useQueryClient();
  const mutation = useMutation({
    mutationFn: (codec: VideoCodec) => api.setCodec(c.camera_id, API_CODEC[codec]),
    onMutate: async (codec) => {
      await qc.cancelQueries({ queryKey: ['cameras'] });
      qc.setQueryData<CamerasResponse>(['cameras'], (old) =>
        old && {
          cameras: old.cameras.map((x) => (x.camera_id === c.camera_id ? { ...x, desired_codec: API_CODEC[codec] } : x)),
        },
      );
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['cameras'] }),
  });
  // 指示がなければ実際の形式、どちらもなければ既定の H.265
  const value = normalizeCodec(c.desired_codec) ?? normalizeCodec(c.codec) ?? 'H.265';
  const switching = isCodecSwitching(c);
  const actual = normalizeCodec(c.codec);

  return (
    <Stack gap="xs" maw={320}>
      <Group justify="space-between" gap="xs">
        <Text size="sm" fw={600}>
          映像形式
        </Text>
        {switching ? (
          <Badge color="blue">切り替え中…</Badge>
        ) : (
          actual && (
            <Badge color="gray" variant="light">
              配信中 {actual}
            </Badge>
          )
        )}
      </Group>
      <Radio.Group value={value} onChange={(v) => mutation.mutate(v as VideoCodec)} aria-label="映像形式">
        <Stack gap="sm">
          <Radio
            value="H.265"
            label="H.265(推奨)"
            description="帯域と保存の費用が少ない。Firefox など一部のブラウザでは再生できない"
            disabled={mutation.isPending}
          />
          <Radio
            value="H.264"
            label="H.264(互換性重視)"
            description={`ほぼすべてのブラウザで再生できる。同じ画質で帯域と保存の費用が約 ${H264_BITRATE_RATIO} 倍`}
            disabled={mutation.isPending}
          />
        </Stack>
      </Radio.Group>
      <Text size="xs" c="dimmed">
        切り替えるとカメラの設定を変えて配信をつなぎ直します(数秒途切れます)。以降の録画はこの形式になり、それまでの録画は元の形式のままです。
      </Text>
      {c.codec_error && (
        <Text size="xs" c="red">
          {c.codec_error}
        </Text>
      )}
      {mutation.isError && (
        <Text size="xs" c="red">
          切り替えに失敗しました: {(mutation.error as Error).message}
        </Text>
      )}
    </Stack>
  );
}

/** プレーヤーのツールバーに置く「カメラ設定」ボタン */
export function CameraSettingsButton({ camera }: { camera: Camera }) {
  const switching = isCodecSwitching(camera);
  return (
    <Popover position="top-start" withArrow shadow="md" withinPortal>
      <Popover.Target>
        <Tooltip label="カメラ設定">
          <ActionIcon size="lg" variant={switching ? 'light' : 'subtle'} color={switching ? 'blue' : 'gray'} aria-label="カメラ設定">
            <IconSettings size={18} />
          </ActionIcon>
        </Tooltip>
      </Popover.Target>
      <Popover.Dropdown>
        <CodecSetting camera={camera} />
      </Popover.Dropdown>
    </Popover>
  );
}

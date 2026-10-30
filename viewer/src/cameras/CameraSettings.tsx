// カメラ設定: 映像形式(H.265 / H.264)の切り替えと、エンコーダーの画質・ビットレートの上限・VBR / CBR。
// 映像形式は配信は動いたまま切り替わり(数秒途切れる)、以降の録画もその形式になる

import {
  ActionIcon,
  Badge,
  Button,
  Divider,
  Group,
  NumberInput,
  Popover,
  Radio,
  SegmentedControl,
  Stack,
  Text,
  Tooltip,
} from '@mantine/core';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { IconSettings } from '@tabler/icons-react';
import { useState } from 'react';
import { api } from '../api/client';
import type { Camera, CamerasResponse, EncoderSettings } from '../api/types';
import { H264_BITRATE_RATIO, isCodecSwitching, normalizeCodec, type VideoCodec } from '../lib/codec';
import { RECOMMENDED_BITRATE_LIMIT_KBPS, formatVolume, isEncoderApplying, monthlyGbAt } from '../lib/encoder';

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
            description="帯域と保存の費用が少ない。Edge(拡張なし)や古い PC など一部の環境では再生できない"
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

type RateControl = 'VBR' | 'CBR';
type EncoderInput = Pick<EncoderSettings, 'quality' | 'bitrate_limit_kbps' | 'rate_control'>;

const mbps = (kbps: number) => Math.round(kbps / 100) / 10;

function describeEncoder(e: EncoderSettings | null): string | null {
  if (!e) return null;
  const parts = [
    e.quality != null && `画質 ${e.quality}`,
    e.bitrate_limit_kbps != null && `上限 ${mbps(e.bitrate_limit_kbps).toFixed(1)} Mbps`,
    e.rate_control,
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : null;
}

export function EncoderSetting({ camera: c }: { camera: Camera }) {
  const qc = useQueryClient();
  // 入力の初期値は、指示した値 → カメラの今の値 → 目安
  const init = { ...c.encoder, ...c.desired_encoder };
  const [quality, setQuality] = useState<number | string>(init.quality ?? '');
  const [limit, setLimit] = useState<number | string>(mbps(init.bitrate_limit_kbps ?? RECOMMENDED_BITRATE_LIMIT_KBPS));
  const [rate, setRate] = useState<RateControl>(init.rate_control ?? 'VBR');

  const mutation = useMutation({
    mutationFn: (enc: EncoderInput) => api.setEncoder(c.camera_id, enc),
    onMutate: async (enc) => {
      await qc.cancelQueries({ queryKey: ['cameras'] });
      qc.setQueryData<CamerasResponse>(['cameras'], (old) =>
        old && {
          cameras: old.cameras.map((x) =>
            x.camera_id === c.camera_id ? { ...x, desired_encoder: { ...x.desired_encoder, ...enc }, encoder_error: null } : x,
          ),
        },
      );
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['cameras'] }),
  });

  const limitKbps = typeof limit === 'number' && limit > 0 ? Math.round(limit * 1000) : null;
  const qualityValue = typeof quality === 'number' && quality >= 0 ? quality : null;
  const enc: EncoderInput = { bitrate_limit_kbps: limitKbps, rate_control: rate, ...(qualityValue != null && { quality: qualityValue }) };
  const want = { ...c.desired_encoder };
  const dirty =
    limitKbps !== (want.bitrate_limit_kbps ?? null) ||
    rate !== (want.rate_control ?? null) ||
    (qualityValue != null && qualityValue !== (want.quality ?? null));
  const applying = isEncoderApplying(c);
  const current = describeEncoder(c.encoder);

  return (
    <Stack gap="xs" maw={320}>
      <Group justify="space-between" gap="xs">
        <Text size="sm" fw={600}>
          画質とビットレート
        </Text>
        {applying && <Badge color="blue">適用中…</Badge>}
      </Group>
      <Text size="xs" c="dimmed">
        カメラの今の設定: {current ?? '不明(ONVIF の Media2 に対応していないカメラ)'}
      </Text>
      <SegmentedControl
        size="xs"
        value={rate}
        onChange={(v) => setRate(v as RateControl)}
        data={[
          { value: 'VBR', label: 'VBR(推奨)' },
          { value: 'CBR', label: 'CBR' },
        ]}
        aria-label="ビットレートの方式"
      />
      <NumberInput
        size="xs"
        label={rate === 'VBR' ? 'ビットレートの上限' : 'ビットレート'}
        description={
          rate === 'VBR'
            ? `人の多い時間帯もこれ以上は使わない。目安は ${mbps(RECOMMENDED_BITRATE_LIMIT_KBPS)} Mbps(1080p で VMAF 93 前後)`
            : 'いつもこのビットレートで送る(静かな時間帯も減らない)'
        }
        suffix=" Mbps"
        min={0.1}
        max={16}
        step={0.5}
        decimalScale={1}
        value={limit}
        onChange={setLimit}
      />
      {limitKbps && (
        <Text size="xs" c="dimmed">
          1 か月の通信量は{rate === 'VBR' ? '最大' : ''}約 {formatVolume(monthlyGbAt(limitKbps))}
        </Text>
      )}
      <NumberInput
        size="xs"
        label="画質(Quality)"
        description="大きいほど高画質。範囲はカメラの機種ごと(エミュレーターは 0〜100、目安は 70)。上げすぎると上限に当たったときに画質が揺れる"
        placeholder="カメラの設定のまま"
        min={0}
        step={5}
        allowNegative={false}
        value={quality}
        onChange={setQuality}
        disabled={rate === 'CBR'}
      />
      <Group justify="flex-end">
        <Button size="xs" onClick={() => mutation.mutate(enc)} disabled={!dirty || !limitKbps} loading={mutation.isPending}>
          カメラに設定
        </Button>
      </Group>
      <Text size="xs" c="dimmed">
        配信を止めずにカメラの設定を変えます(カメラによっては数秒途切れます)。
      </Text>
      {c.encoder_error && (
        <Text size="xs" c="red">
          {c.encoder_error}
        </Text>
      )}
      {mutation.isError && (
        <Text size="xs" c="red">
          設定に失敗しました: {(mutation.error as Error).message}
        </Text>
      )}
    </Stack>
  );
}

/** プレーヤーのツールバーに置く「カメラ設定」ボタン */
export function CameraSettingsButton({ camera }: { camera: Camera }) {
  const switching = isCodecSwitching(camera) || isEncoderApplying(camera);
  return (
    // 設定が縦に長いので、画面に収まる高さにしてスクロールさせる(スマホの幅で上がはみ出さないように)
    <Popover
      position="top-start"
      withArrow
      shadow="md"
      withinPortal
      middlewares={{ flip: true, shift: true, size: { padding: 8 } }}
    >
      <Popover.Target>
        <Tooltip label="カメラ設定">
          <ActionIcon size="lg" variant={switching ? 'light' : 'subtle'} color={switching ? 'blue' : 'gray'} aria-label="カメラ設定">
            <IconSettings size={18} />
          </ActionIcon>
        </Tooltip>
      </Popover.Target>
      <Popover.Dropdown style={{ overflowY: 'auto' }}>
        <Stack gap="md">
          <CodecSetting camera={camera} />
          <Divider />
          <EncoderSetting key={camera.camera_id} camera={camera} />
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}

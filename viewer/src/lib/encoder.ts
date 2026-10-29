// エンコーダーの設定(Quality、ビットレートの上限、VBR / CBR)。目安は docs/codec.md「上限付き VBR」

import type { EncoderSettings } from '../api/types';

/** VMAF 93 を狙うときの上限の目安(H.265 1080p30。docs/codec.md) */
export const RECOMMENDED_BITRATE_LIMIT_KBPS = 4000;

const SECONDS_PER_MONTH = 30 * 24 * 3600;

/** 上限のビットレートで 1 か月(30 日)流し続けたときの通信量(GB)。VBR では静かな時間帯はこれより少ない */
export function monthlyGbAt(kbps: number): number {
  return (kbps * 1000 * SECONDS_PER_MONTH) / 8 / 1e9;
}

/** 1,296 GB → "1.3 TB"、260 GB → "260 GB" */
export function formatVolume(gb: number): string {
  return gb >= 1000 ? `${(gb / 1000).toFixed(1)} TB` : `${Math.round(gb)} GB`;
}

interface EncoderState {
  encoder: EncoderSettings | null;
  desired_encoder: EncoderSettings | null;
  encoder_error: string | null;
  desired_streaming: boolean;
  online: boolean | null;
}

const KEYS = ['quality', 'bitrate_limit_kbps', 'rate_control'] as const;

/** 指示した設定がまだカメラに反映されていない(エッジが設定中)。失敗した・配信を止めている・オフラインなら false */
export function isEncoderApplying(c: EncoderState): boolean {
  const want = c.desired_encoder;
  if (!want || c.encoder_error || !c.desired_streaming || c.online === false) return false;
  return KEYS.some((k) => want[k] != null && want[k] !== c.encoder?.[k]);
}

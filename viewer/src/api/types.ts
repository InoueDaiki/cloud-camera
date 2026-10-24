// HTTP API の型(docs/contracts.md の「HTTP API」節に対応)

/** シャドウの reported / desired のうち、ビューワーが読む項目(docs/contracts.md「IoT Core」) */
export interface ShadowCamera {
  streaming?: boolean | null;
  codec?: string | null;
  resolution?: string | null;
  error?: string | null;
  name?: string | null;
  /** PTZ の状態(エッジが書く。docs/contracts.md「PTZ」) */
  ptz?: PtzState | null;
  [key: string]: unknown;
}

/** GET /api/cameras の 1 件(API の形そのまま) */
export interface CameraRaw {
  camera_id: string;
  gateway_id?: string | null;
  stream_name?: string | null;
  reported?: ShadowCamera | null;
  desired?: ShadowCamera | null;
  reported_at?: string | null;
  video_processed_until?: string | null;
  detect_processed_until?: string | null;
  /** ゲートウェイの死活。API が返すようになったら使う */
  online?: boolean | null;
}

export interface CamerasResponseRaw {
  cameras: CameraRaw[];
}

/** 画面で使う形に直したカメラ */
export interface Camera {
  camera_id: string;
  name: string | null;
  gateway_id: string | null;
  /** 死活。不明なら null */
  online: boolean | null;
  /** 実際に配信しているか(reported) */
  streaming: boolean;
  /** 配信の指示(desired)。なければ reported と同じ */
  desired_streaming: boolean;
  /** 実際に配信している映像形式(reported、"H265" / "H264" など) */
  codec: string | null;
  /** 指示した映像形式(desired の codec)。なければ null(エッジの既定 = H.265) */
  desired_codec: string | null;
  /** 指示した形式に切り替えられなかった理由(reported の codec_error) */
  codec_error: string | null;
  resolution: string | null;
  error: string | null;
  /** PTZ に対応しているか(シャドウの reported.ptz.supported) */
  ptz_supported: boolean;
  reported_at: string | null;
  video_processed_until: string | null;
  detect_processed_until: string | null;
}

export interface CamerasResponse {
  cameras: Camera[];
}

export interface TimeRange {
  start: string;
  end: string;
}

export interface RecordedRange extends TimeRange {
  source: 's3' | 'kvs';
}

export interface TimelineMinute {
  start: string;
  sprite: string;
}

/** GET /api/cameras/{id}/timeline */
export interface TimelineResponse {
  camera_id: string;
  from: string;
  to: string;
  video_processed_until: string | null;
  detect_processed_until: string | null;
  recorded: RecordedRange[];
  failed: TimeRange[];
  minutes: TimelineMinute[];
}

/** GET /api/cameras/{id}/kvs-hls */
export interface KvsHlsResponse {
  url: string;
  expires_at: string;
}

/** GET /api/cameras/{id}/webrtc(KVS WebRTC の視聴情報。AWS の認証情報は含まない) */
export interface WebRtcSessionInfo {
  camera_id?: string;
  channel_name: string;
  channel_arn: string;
  region: string;
  client_id: string;
  wss_endpoint: string;
  /** ConnectAsViewer の署名付き URL(299 秒で失効) */
  wss_url: string;
  ice_servers: RTCIceServer[];
  /** エッジが出せるコーデック(H.265 が先) */
  codecs: string[];
  expires_at: string;
  issue_ms?: number;
  /** モック(MSW)のときだけ。ブラウザ内の疑似 master につなぐ */
  mock?: boolean;
}

export interface SearchHit {
  camera_id: string;
  ts: string;
  label: string;
  confidence: number;
}

/** POST /api/search */
export interface SearchResponse {
  answer: string;
  hits: SearchHit[];
  sql?: string | null;
  usage?: Record<string, unknown> | null;
}

export interface PtzPreset {
  token: string;
  name: string;
}

/** GET /api/cameras/{id}/ptz(シャドウの reported.cameras.{id}.ptz)。pan / tilt は -1〜1、zoom は 0〜1 */
export interface PtzState {
  supported: boolean;
  pan?: number | null;
  tilt?: number | null;
  zoom?: number | null;
  presets?: PtzPreset[] | null;
  move_status?: 'MOVING' | 'IDLE' | 'UNKNOWN' | null;
  error?: string | null;
  updated_at?: string | null;
}

export type PtzOp = 'continuous' | 'stop' | 'absolute' | 'relative' | 'goto_preset' | 'set_preset' | 'home';

/** POST /api/cameras/{id}/ptz の本文(MQTT の cmd の op 以降と同じ形) */
export interface PtzCommand {
  op: PtzOp;
  pan?: number;
  tilt?: number;
  zoom?: number;
  timeout_ms?: number;
  preset_token?: string;
  preset_name?: string;
}

export interface PtzCommandResponse {
  request_id: string;
}

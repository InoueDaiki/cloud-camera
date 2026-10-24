// HTTP API の型(docs/contracts.md の「HTTP API」節に対応)

/** シャドウの reported / desired のうち、ビューワーが読む項目(docs/contracts.md「IoT Core」) */
export interface ShadowCamera {
  streaming?: boolean | null;
  codec?: string | null;
  resolution?: string | null;
  error?: string | null;
  name?: string | null;
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
  codec: string | null;
  resolution: string | null;
  error: string | null;
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

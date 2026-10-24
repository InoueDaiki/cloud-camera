// HTTP API の型(docs/contracts.md の「HTTP API」節に対応)

/** GET /api/cameras の 1 件 */
export interface Camera {
  camera_id: string;
  /** 表示名。なければ camera_id を出す */
  name?: string | null;
  gateway_id?: string | null;
  /** ゲートウェイの死活(status トピック / LWT から) */
  online: boolean;
  /** シャドウの reported.streaming(実際に配信しているか) */
  streaming: boolean;
  /** シャドウの desired.streaming(配信の指示) */
  desired_streaming?: boolean | null;
  codec?: string | null;
  resolution?: string | null;
  error?: string | null;
  /** 最後に死活を受け取った時刻 */
  last_seen?: string | null;
  video_processed_until?: string | null;
  detect_processed_until?: string | null;
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
  usage?: Record<string, number | string | null> | null;
}

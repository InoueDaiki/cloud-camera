variable "project" {
  description = "リソース名の接頭辞({p})。検証用の一時環境は cc-tmp-xxx など別名にする"
  type        = string
  default     = "cloud-camera"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.project))
    error_message = "project は小文字英数字とハイフンで 3〜32 文字にする(S3 バケット名に使うため)。"
  }
}

variable "region" {
  description = "AWS リージョン"
  type        = string
  default     = "ap-northeast-1"
}

variable "gateways" {
  description = "ゲートウェイ ID → 配下のカメラ ID のリスト。IoT Thing はゲートウェイごと、KVS ストリームとスケジュールはカメラごとに作る"
  type        = map(list(string))
  default = {
    "gw-001" = ["cam-001"]
  }

  validation {
    condition = alltrue([
      for gw, cams in var.gateways : can(regex("^gw-[0-9a-z-]+$", gw)) && alltrue([for c in cams : can(regex("^cam-[0-9a-z-]+$", c))])
    ])
    error_message = "ゲートウェイ ID は gw-001、カメラ ID は cam-001 の形式にする。"
  }
}

variable "cameras" {
  description = "カメラ ID のリスト。null なら gateways から導く。指定する場合は gateways に含まれるカメラと一致させる"
  type        = list(string)
  default     = null
}

variable "camera_codecs" {
  description = "カメラ ID → 映像コーデック(H265 / H264)。KVS ストリームの media type(video/h265 / video/h264)に使う。書いていないカメラは H265(docs/codec.md)"
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for c in values(var.camera_codecs) : contains(["H265", "H264"], c)])
    error_message = "camera_codecs の値は H265 か H264 にする。"
  }
}

variable "camera_names" {
  description = "カメラ ID → 表示名(例 {\"cam-001\" = \"玄関\"})。自然言語検索が「玄関」などの名前をカメラ ID に結びつけるのに使う"
  type        = map(string)
  default     = {}
}

variable "kvs_retention_hours" {
  description = "KVS の保持期間(時間)"
  type        = number
  default     = 1
}

variable "media_retention_days" {
  description = "メディアバケット(動画、スプライト、プレイリスト)と検出結果バケットの保存日数。DynamoDB の expires_at もこれに合わせる"
  type        = number
  default     = 30
}

variable "lag_seconds" {
  description = "現在時刻からどれだけ遅らせて区間を確定させるか(秒)"
  type        = number
  default     = 45
}

variable "detector_frame_source" {
  description = "物体検出のフレーム源。clip = 動画の退避の clip.mp4 のキーフレームをデコード(GetImages を使わない)、getimages = KVS GetImages"
  type        = string
  default     = "clip"
  validation {
    condition     = contains(["clip", "getimages"], var.detector_frame_source)
    error_message = "detector_frame_source は clip か getimages。"
  }
}

variable "detector_upstream_max_wait_seconds" {
  description = "clip のとき、物体検出が動画の退避を待つ上限(区間の終わりからの秒)。過ぎたら GetImages で取る。KVS の保持期間(1 時間)より十分短くする"
  type        = number
  default     = 1800
}

variable "max_minutes" {
  description = "1 回の起動で処理する区間数の上限"
  type        = number
  default     = 10
}

variable "max_attempts" {
  description = "区間ごとの試行回数の上限。超えたら FAILED で確定する"
  type        = number
  default     = 5
}

variable "lambda_settings" {
  description = "Lambda ごとのメモリ(MB)、タイムアウト(秒)、エフェメラルストレージ(MB)の上書き。キーは video_archiver、detector、api。指定しない関数は locals.tf の既定値"
  type = map(object({
    memory_size       = optional(number)
    timeout           = optional(number)
    ephemeral_storage = optional(number)
  }))
  default = {}

  validation {
    condition     = alltrue([for k in keys(var.lambda_settings) : contains(["video_archiver", "detector", "api"], k)])
    error_message = "lambda_settings のキーは video_archiver、detector、api のいずれか。"
  }
}

variable "lambda_layers" {
  description = "Lambda ごとに追加するレイヤー ARN(例: detector に AWS SDK for pandas のマネージドレイヤー)"
  type        = map(list(string))
  default     = {}
}

variable "lambda_zip_dir" {
  description = "Lambda の zip(video_archiver.zip、detector.zip、api.zip)を置いたディレクトリ。null ならリポジトリ直下の build/"
  type        = string
  default     = null
}

variable "log_retention_days" {
  description = "CloudWatch Logs の保持日数"
  type        = number
  default     = 14
}

variable "schedule_enabled" {
  description = "EventBridge Scheduler を有効にするか(止めたいときは false)"
  type        = bool
  default     = true
}

variable "bedrock_model_id" {
  description = "自然言語検索に使う Bedrock のモデル ID か推論プロファイル ID"
  type        = string
  default     = "jp.amazon.nova-2-lite-v1:0"
}

variable "athena_bytes_scanned_cutoff" {
  description = "Athena の 1 クエリあたりのスキャン量の上限(バイト、最小 10MB)"
  type        = number
  default     = 1073741824 # 1 GiB
}

variable "cloudfront_price_class" {
  description = "CloudFront の価格クラス"
  type        = string
  default     = "PriceClass_200"
}

variable "force_destroy_buckets" {
  description = "true なら中身が残っていても destroy でバケットを消す(検証用。通常は scripts/destroy.sh が空にする)"
  type        = bool
  default     = false
}

"""机上計算: ビットレートや検出頻度などの前提から、実測と同じ明細を作る(実測の検算と条件の比較用)。

既定値の出典:
- video_archiver の実行時間・メモリ・ファイルサイズ: lambdas/video_archiver/README.md の実測(0.6 Mbps、2026-10-24)
- DynamoDB の書き込み回数: lambdas/common/pipeline.py の UpdateItem の数(起動ごとにリース取得と解放、区間ごとに試行回数・結果・カーソル)
- エッジの死活とシャドウ: docs/contracts.md(30 秒ごとの status と reported の更新)
- 検出結果の行の大きさ: 構想.md「検出結果の保存」(1 日約 17 万行で 3〜5 MB → 1 行約 25 バイト)
物体検出の推論時間、視聴時間、質問数とトークン数は実測がまだないので仮定。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields

from . import model as m
from .model import GB, MINUTES_PER_MONTH, SECONDS_PER_MONTH, Line
from .prices import bedrock_price_prefix


@dataclass
class DeskParams:
    # 映像
    bitrate_mbps: float = 1.0  # 平均ビットレート(VBR の平均)
    recording_ratio: float = 1.0  # 録画している時間の割合(24 時間配信なら 1)
    kvs_retention_hours: float = 1.0
    media_retention_days: float = 30.0
    # 動画の退避(video_archiver)
    archiver_memory_mb: int = 512
    archiver_ephemeral_mb: int = 1024
    archiver_duration_s: float = 1.9  # 1 区間(録画あり)あたり
    archiver_empty_duration_s: float = 0.05  # 録画なしの区間
    sprite_images_per_minute: int = 61  # GetImages は [start, end] の両端を含むので 61 枚返る(README の実測)
    sprite_kb: float = 80.0
    playlist_kb: float = 3.5
    s3_puts_per_minute: int = 3  # clip.mp4、sprite.jpg、index.m3u8
    # 物体検出(detector)
    detect_interval_s: float = 1.0  # 何秒おきに 1 枚推論するか
    detect_images_extra: int = 1  # GetImages は区間の終わりの 1 枚も返す(課金される。推論には使わない)
    detector_memory_mb: int = 3008
    detector_ephemeral_mb: int = 1024
    detector_overhead_s: float = 2.0  # 1 区間あたりの推論以外(GetImages、Parquet の読み書き)
    detector_sec_per_frame: float = 0.25  # 1 枚の推論時間(仮定。メモリを変えて実測する)
    detections_per_frame: float = 2.0
    detection_row_bytes: float = 25.0  # Parquet 圧縮後の 1 行
    detections_retention_days: float | None = None  # 現行バージョンの期限はない。None なら media_retention_days と同じとみなす
    # Lambda 共通
    architecture: str = "arm64"
    log_bytes_per_invocation: float = 1500.0  # START / END / REPORT と構造化ログ 1〜2 行
    # DynamoDB
    segment_item_kb: float = 2.5  # 区間の件(hls.parts に断片 60 本)
    # 再生
    kvs_view_hours: float = 1.0  # KVS の HLS で見る時間(ライブと退避前の VOD)/ 台・月
    s3_view_hours: float = 1.0  # S3(CloudFront)で見る時間 / 台・月
    hls_segment_s: float = 1.0  # S3 の HLS の 1 セグメント(= KVS の断片)の秒数
    cloudfront_miss_ratio: float = 1.0  # CloudFront のキャッシュミス率(S3 GET の数。安全側に 1)
    api_requests_per_view_hour: float = 120.0  # timeline、vod.m3u8 などの API 呼び出し
    api_duration_ms: float = 150.0
    api_memory_mb: int = 512
    # 検索(台数によらない)
    questions_per_month: float = 100.0
    input_tokens_per_question: float = 8000.0  # ツール呼び出しの往復を含む合計
    output_tokens_per_question: float = 600.0
    bedrock_model_id: str = "jp.amazon.nova-2-lite-v1:0"
    athena_queries_per_question: float = 1.5
    athena_days_per_query: float = 1.0  # 1 クエリで読む日数(day ファイルの数 / 台)
    search_duration_s: float = 6.0  # api Lambda の 1 質問あたりの実行時間
    glue_requests_per_query: float = 3.0  # Athena が Glue のテーブル定義を引く回数(推定)
    # 制御
    cameras_per_gateway: float = 1.0
    status_interval_s: float = 30.0
    status_message_bytes: float = 400.0  # 1 台ぶんの status(カメラが増えると比例)
    shadow_update_bytes: float = 300.0

    @classmethod
    def help(cls) -> dict[str, str]:
        return {f.name: str(f.default) for f in fields(cls)}

    def as_dict(self) -> dict:
        return asdict(self)


def build_lines(p: DeskParams) -> list[Line]:
    L: list[Line] = []
    arch = p.architecture
    src = "机上"
    rec = p.recording_ratio
    minutes = MINUTES_PER_MONTH
    rec_minutes = minutes * rec
    bytes_per_s = p.bitrate_mbps * 1e6 / 8
    ingest_bytes = bytes_per_s * SECONDS_PER_MONTH * rec

    def lam(cat: str, name: str, memory_mb: float, eph_mb: float, duration_ms_total: float,
            invocations: float, log_bytes: float, basis: str, scale: str = "camera") -> None:
        L.append(Line(cat, f"Lambda {name} 実行", f"lambda.gb_s.{arch}",
                      m.lambda_gb_seconds(memory_mb, duration_ms_total), scale, src, basis))
        if eph_mb > 512:
            L.append(Line(cat, f"Lambda {name} エフェメラルストレージ", f"lambda.ephemeral_gb_s.{arch}",
                          m.lambda_ephemeral_gb_seconds(eph_mb, duration_ms_total), scale, src, f"{eph_mb} MB(512 MB 超の分)"))
        L.append(Line(cat, f"Lambda {name} リクエスト", f"lambda.requests.{arch}", invocations, scale, src,
                      f"{invocations:,.0f} 回"))
        L.append(Line(cat, f"CloudWatch Logs({name})", "logs.vended_ingest_gb", log_bytes / GB, scale, src,
                      f"{p.log_bytes_per_invocation:.0f} バイト / 起動"))

    # --- 取り込み ---
    L.append(Line("取り込み", "KVS 取り込み", "kvs.ingest_gb", ingest_bytes / GB, "camera", src,
                  f"{p.bitrate_mbps} Mbps × 30 日 × 録画率 {rec}"))
    # --- 一時保持 ---
    kvs_avg_bytes = bytes_per_s * 3600 * p.kvs_retention_hours * rec
    L.append(Line("一時保持", "KVS 保存", "kvs.storage_gb_month", kvs_avg_bytes / GB, "camera", src,
                  f"保持 {p.kvs_retention_hours} 時間分の平均保存量"))
    # --- 動画抽出(video_archiver) ---
    L.append(Line("動画抽出", "KVS HLS 取り出し(clip.mp4 用)", "kvs.hls_out_gb", ingest_bytes / GB, "camera", src,
                  "取り込んだ断片をすべて 1 回取り出す"))
    sprite_images = p.sprite_images_per_minute * rec_minutes
    L.append(Line("動画抽出", "KVS GetImages(スプライト)", "kvs.getimages_lowres", sprite_images, "camera", src,
                  f"{p.sprite_images_per_minute} 枚 / 分"))
    arch_ms = (p.archiver_duration_s * rec_minutes + p.archiver_empty_duration_s * (minutes - rec_minutes)) * 1000
    lam("動画抽出", "video_archiver", p.archiver_memory_mb, p.archiver_ephemeral_mb, arch_ms, minutes,
        minutes * p.log_bytes_per_invocation, f"{p.archiver_memory_mb} MB × {p.archiver_duration_s} 秒 / 分")
    L.append(Line("動画抽出", "EventBridge Scheduler(動画)", "scheduler.invocations", minutes, "camera", src, "1 分ごと"))
    # --- 加工・保存 ---
    per_min_bytes = bytes_per_s * 60 + (p.sprite_kb + p.playlist_kb) * 1024
    L.append(Line("加工・保存", "S3 保存(動画、スプライト、プレイリスト)", "s3.storage_gb_month",
                  m.steady_state_gb_month(per_min_bytes * 1440 * rec, p.media_retention_days), "camera", src,
                  f"保存 {p.media_retention_days:g} 日の定常状態"))
    L.append(Line("加工・保存", "S3 PUT(メディア)", "s3.put", p.s3_puts_per_minute * rec_minutes, "camera", src,
                  f"{p.s3_puts_per_minute} 回 / 分"))
    seg_wru = m.dynamodb_write_units(p.segment_item_kb * 1024)
    # 1 起動: リース取得 1 + 解放 1、1 区間: 試行回数 + 結果(区間の件のサイズ)+ カーソル 1
    wru_video = minutes * (2 + 2 * seg_wru + 1)
    L.append(Line("加工・保存", "DynamoDB 書き込み(動画の退避)", "dynamodb.wru", wru_video, "camera", src,
                  f"1 分あたり {2 + 2 * seg_wru + 1} WRU(区間の件 {p.segment_item_kb} KB)"))
    L.append(Line("加工・保存", "DynamoDB 保存(区間の件)", "dynamodb.storage_gb_month",
                  m.steady_state_gb_month(p.segment_item_kb * 1024 * 1440, p.media_retention_days), "camera", src,
                  "TTL = メディアの保存期限"))
    # --- 物体検出(detector) ---
    frames_per_min = 60 / p.detect_interval_s if p.detect_interval_s > 0 else 0
    if frames_per_min:
        imgs = frames_per_min + p.detect_images_extra
        L.append(Line("物体検出", "KVS GetImages(推論用)", "kvs.getimages_lowres", imgs * rec_minutes, "camera",
                      src, f"{imgs:g} 枚 / 分(推論 {frames_per_min:g} 枚 + 区間の終わりの 1 枚)"))
        det_ms = (p.detector_overhead_s + frames_per_min * p.detector_sec_per_frame) * rec_minutes * 1000
        det_ms += p.archiver_empty_duration_s * (minutes - rec_minutes) * 1000
        lam("物体検出", "detector", p.detector_memory_mb, p.detector_ephemeral_mb, det_ms, minutes,
            minutes * p.log_bytes_per_invocation,
            f"{p.detector_memory_mb} MB × ({p.detector_overhead_s} + {frames_per_min:g} 枚 × {p.detector_sec_per_frame} 秒)")
        L.append(Line("物体検出", "EventBridge Scheduler(検出)", "scheduler.invocations", minutes, "camera", src,
                      "1 分ごと"))
        # --- 検出結果保存 ---
        rows_per_day = frames_per_min * 1440 * rec * p.detections_per_frame
        day_bytes = rows_per_day * p.detection_row_bytes
        ret = p.detections_retention_days if p.detections_retention_days is not None else p.media_retention_days
        # 旧バージョン: 1 日 1,440 回書き直し、各版の大きさは平均で day ファイルの半分、約 1.5 日残る(ライフサイクルは 1 日 1 回)
        noncurrent = 1440 * rec * day_bytes / 2 * 1.5
        L.append(Line("検出結果保存", "S3 保存(検出結果の現行版)", "s3.storage_gb_month",
                      m.steady_state_gb_month(day_bytes, ret), "camera", src,
                      f"1 日 {rows_per_day:,.0f} 行 × {p.detection_row_bytes:g} バイト、{ret:g} 日分"))
        L.append(Line("検出結果保存", "S3 保存(検出結果の旧バージョン)", "s3.storage_gb_month", noncurrent / GB, "camera",
                      src, "1,440 版 × 平均 day ファイルの半分 × 約 1.5 日"))
        L.append(Line("検出結果保存", "S3 PUT(day ファイルの書き直し)", "s3.put", rec_minutes, "camera",
                      src, "1 分 1 回"))
        L.append(Line("検出結果保存", "S3 GET(day ファイル)", "s3.get", rec_minutes, "camera", src, "1 分 1 回"))
        L.append(Line("検出結果保存", "DynamoDB 書き込み(検出)", "dynamodb.wru", minutes * (2 + 2 * seg_wru + 1), "camera",
                      src, "動画の退避と同じ回数(同じ区間の件を更新する)"))
    # --- 再生 ---
    kvs_view_bytes = bytes_per_s * 3600 * p.kvs_view_hours
    L.append(Line("再生", "KVS HLS 取り出し(視聴)", "kvs.hls_out_gb", kvs_view_bytes / GB, "camera", src,
                  f"{p.kvs_view_hours:g} 時間 / 台・月"))
    s3_view_bytes = bytes_per_s * 3600 * p.s3_view_hours
    seg_req = 3600 * p.s3_view_hours / p.hls_segment_s
    sprite_req = p.s3_view_hours * 60  # 1 分ごとのスプライト
    cf_req = seg_req + sprite_req + p.s3_view_hours * 60  # プレイリスト
    L.append(Line("再生", "CloudFront データ転送", "cloudfront.out_gb", s3_view_bytes / GB, "camera", src,
                  f"{p.s3_view_hours:g} 時間 / 台・月"))
    L.append(Line("再生", "CloudFront リクエスト", "cloudfront.https_requests", cf_req, "camera", src,
                  f"セグメント {p.hls_segment_s:g} 秒ごと + スプライト"))
    L.append(Line("再生", "S3 → CloudFront 転送", "s3.to_cloudfront_gb", s3_view_bytes / GB * p.cloudfront_miss_ratio,
                  "camera", src, "無料"))
    L.append(Line("再生", "S3 GET(CloudFront のオリジン取得)", "s3.get", cf_req * p.cloudfront_miss_ratio, "camera",
                  src, f"キャッシュミス率 {p.cloudfront_miss_ratio:g}"))
    api_req = p.api_requests_per_view_hour * (p.kvs_view_hours + p.s3_view_hours)
    L.append(Line("再生", "API Gateway(HTTP API)", "apigw.http_requests", api_req, "camera", src,
                  f"{p.api_requests_per_view_hour:g} 回 / 視聴時間"))
    L.append(Line("再生", "CloudFront リクエスト(/api)", "cloudfront.https_requests", api_req, "camera", src, ""))
    lam("再生", "api", p.api_memory_mb, 512, api_req * p.api_duration_ms, api_req,
        api_req * p.log_bytes_per_invocation, f"{p.api_duration_ms:g} ms / リクエスト")
    L.append(Line("再生", "DynamoDB 読み込み(timeline)", "dynamodb.rru",
                  api_req * math.ceil(1440 * p.segment_item_kb / 4 / 24) / 2, "camera", src,
                  "1 リクエスト = 1 時間分の区間の件の Query(結果整合性)"))
    # --- 検索(台数によらない) ---
    q = p.questions_per_month
    bp = bedrock_price_prefix(p.bedrock_model_id)
    L.append(Line("検索", f"Bedrock 入力トークン({p.bedrock_model_id})", f"{bp}.input",
                  q * p.input_tokens_per_question / 1e6, "fixed", src, f"{q:g} 質問 × {p.input_tokens_per_question:g}"))
    L.append(Line("検索", "Bedrock 出力トークン", f"{bp}.output", q * p.output_tokens_per_question / 1e6, "fixed", src,
                  f"{q:g} 質問 × {p.output_tokens_per_question:g}"))
    if frames_per_min:
        day_bytes_all = frames_per_min * 1440 * rec * p.detections_per_frame * p.detection_row_bytes
        scan_per_query_mb = day_bytes_all * p.athena_days_per_query / 1e6
        queries = q * p.athena_queries_per_question
        # 最低課金(1 クエリ 10 MB)は台数によらない部分として fixed に置き、超えた分を台数比例にする
        L.append(Line("検索", "Athena スキャン(最低課金の分)", "athena.scan_tb", queries * 10 / 1e6, "fixed", src,
                      f"{queries:g} クエリ × 10 MB(安全側の仮定)"))
        L.append(Line("検索", "Athena スキャン(台数に比例する分)", "athena.scan_tb",
                      queries * scan_per_query_mb / 1e6, "camera", src,
                      f"1 クエリ {scan_per_query_mb:.2f} MB / 台({p.athena_days_per_query:g} 日分)。"
                      "10 MB 未満は上の最低課金に含まれる(台数が少ないと過大)"))
        L.append(Line("検索", "Glue Data Catalog リクエスト", "glue.catalog_requests", queries * p.glue_requests_per_query,
                      "fixed", src, f"{p.glue_requests_per_query:g} 回 / クエリ(推定)"))
        L.append(Line("検索", "S3 PUT / GET(Athena の結果)", "s3.put", queries * 2, "fixed", src, "結果とメタデータ"))
        L.append(Line("検索", "S3 GET(Athena が day ファイルを読む)", "s3.get", queries * p.athena_days_per_query * 3,
                      "camera", src, "1 ファイル約 3 回(フッターと列)"))
    lam("検索", "api(検索)", p.api_memory_mb, 512, q * p.search_duration_s * 1000, q, q * p.log_bytes_per_invocation,
        f"{p.search_duration_s:g} 秒 / 質問", scale="fixed")
    # --- 制御(IoT Core) ---
    gw = 1 / p.cameras_per_gateway
    L.append(Line("制御", "IoT Core 接続", "iot.connection_minutes", minutes * gw, "camera", src,
                  f"ゲートウェイ 1 台につきカメラ {p.cameras_per_gateway:g} 台"))
    status_msgs = SECONDS_PER_MONTH / p.status_interval_s
    units = m.iot_message_units(p.status_message_bytes * p.cameras_per_gateway)
    L.append(Line("制御", "IoT Core メッセージ(status)", "iot.messages", status_msgs * units * gw, "camera", src,
                  f"{p.status_interval_s:g} 秒ごと、{units} 単位(5 KB)"))
    shadow_units = max(1, math.ceil(p.shadow_update_bytes * p.cameras_per_gateway / 1024))
    L.append(Line("制御", "IoT Core シャドウ更新(reported)", "iot.shadow_ops", status_msgs * shadow_units * gw, "camera",
                  src, f"{p.status_interval_s:g} 秒ごとに last_seen を更新"))
    L.append(Line("制御", "IoT Core シャドウ取得(API)", "iot.shadow_ops", api_req / 4, "camera", src,
                  "カメラ一覧の表示(API の 1/4 と仮定)"))
    return L

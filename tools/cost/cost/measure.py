"""実測: デプロイ済みの環境(project 名)から、指定した期間の使用量を集めて明細にする。

`collect()` が AWS の読み取り API だけを呼んで生の値(dict)を作り、`to_lines()` がそれを 30 日・1 台あたりの明細に直す。
`to_lines()` は AWS を呼ばない純粋な関数なので、生の値を JSON に保存しておけば後から計算し直せる。

取れない量は「推定」として、根拠を明細の `basis` に書く。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import model as m
from .model import GB, Line
from .prices import bedrock_price_prefix

LAMBDA_KEYS = ("video_archiver", "detector", "api")


@dataclass
class Target:
    """project 名から導くリソース名(docs/contracts.md と infra/ の命名規則)。"""

    project: str
    account_id: str
    region: str = "ap-northeast-1"

    @property
    def table(self) -> str:
        return f"{self.project}-state"

    def bucket(self, kind: str) -> str:
        return f"{self.project}-{kind}-{self.account_id}"

    def function(self, key: str) -> str:
        return f"{self.project}-{key.replace('_', '-')}"

    @property
    def stream_prefix(self) -> str:
        return f"{self.project}-cam-"

    @property
    def thing_prefix(self) -> str:
        return f"{self.project}-gw-"

    @property
    def api_name(self) -> str:
        return f"{self.project}-api"

    @property
    def workgroup(self) -> str:
        return self.project


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 収集(AWS の読み取り API だけ)
# ---------------------------------------------------------------------------
class Collector:
    def __init__(self, session: Any, target: Target, start: datetime, end: datetime):
        self.s = session
        self.t = target
        self.start = start
        self.end = end
        self.warnings: list[str] = []

    def client(self, name: str, region: str | None = None):
        return self.s.client(name, region_name=region or self.t.region)

    # --- CloudWatch ---
    def metric_sum(self, namespace: str, metric: str, dims: dict[str, str], *, region: str | None = None,
                   stat: str = "Sum") -> float:
        cw = self.client("cloudwatch", region)
        span = (self.end - self.start).total_seconds()
        period = 3600 if span <= 60 * 86400 else 86400
        q = {
            "Id": "m0",
            "MetricStat": {
                "Metric": {"Namespace": namespace, "MetricName": metric,
                           "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]},
                "Period": period,
                "Stat": stat,
            },
        }
        total = 0.0
        kw: dict[str, Any] = {"MetricDataQueries": [q], "StartTime": self.start, "EndTime": self.end}
        while True:
            r = cw.get_metric_data(**kw)
            for res in r["MetricDataResults"]:
                total += sum(res.get("Values", []))
            if not r.get("NextToken"):
                break
            kw["NextToken"] = r["NextToken"]
        return total

    def _safe(self, name: str, fn, default=None):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001  取れないものは警告にして先へ進む
            self.warnings.append(f"{name}: {type(e).__name__}: {e}")
            return default

    # --- 各サービス ---
    def cameras(self) -> list[dict[str, Any]]:
        kv = self.client("kinesisvideo")
        out = []
        kw: dict[str, Any] = {"StreamNameCondition": {"ComparisonOperator": "BEGINS_WITH",
                                                      "ComparisonValue": self.t.stream_prefix}}
        while True:
            r = kv.list_streams(**kw)
            for s in r.get("StreamInfoList", []):
                out.append({"stream": s["StreamName"], "camera_id": s["StreamName"][len(self.t.project) + 1:],
                            "retention_hours": s.get("DataRetentionInHours", 0)})
            if not r.get("NextToken"):
                break
            kw["NextToken"] = r["NextToken"]
        return out

    def dynamodb_minutes(self, cameras: list[str]) -> dict[str, Any]:
        tbl = self.s.resource("dynamodb", region_name=self.t.region).Table(self.t.table)
        agg: dict[str, Any] = defaultdict(float)
        statuses: dict[str, int] = defaultdict(int)
        for cam in cameras:
            kw: dict[str, Any] = {
                "KeyConditionExpression": "camera_id = :c AND sk BETWEEN :a AND :b",
                "ExpressionAttributeValues": {":c": cam, ":a": iso(self.start), ":b": iso(self.end - timedelta(seconds=1))},
            }
            while True:
                r = tbl.query(**kw)
                for it in r["Items"]:
                    if not str(it["sk"])[:1].isdigit():
                        continue
                    agg["minutes"] += 1
                    for k in ("fragment_count", "bytes", "video_duration_ms", "video_attempts", "sprite_frames",
                              "detect_duration_ms", "detect_attempts", "detect_count", "frame_count"):
                        if k in it:
                            agg[k] += float(it[k])
                    for k in ("video_status", "detect_status"):
                        if k in it:
                            statuses[f"{k}:{it[k]}"] += 1
                    agg["item_bytes"] += len(json.dumps(it, default=str))
                if "LastEvaluatedKey" not in r:
                    break
                kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        out = dict(agg)
        out["statuses"] = dict(statuses)
        return out

    def lambda_usage(self) -> dict[str, Any]:
        lm = self.client("lambda")
        out = {}
        for key in LAMBDA_KEYS:
            name = self.t.function(key)
            cfg = self._safe(f"lambda {name}", lambda name=name: lm.get_function_configuration(FunctionName=name))
            if not cfg:
                continue
            dims = {"FunctionName": name}
            out[key] = {
                "function_name": name,
                "memory_mb": cfg.get("MemorySize", 128),
                "ephemeral_mb": (cfg.get("EphemeralStorage") or {}).get("Size", 512),
                "architecture": (cfg.get("Architectures") or ["x86_64"])[0],
                "invocations": self.metric_sum("AWS/Lambda", "Invocations", dims),
                "duration_ms": self.metric_sum("AWS/Lambda", "Duration", dims),
                "log_bytes": self.metric_sum("AWS/Logs", "IncomingBytes", {"LogGroupName": f"/aws/lambda/{name}"}),
                "env": (cfg.get("Environment") or {}).get("Variables", {}),
            }
        return out

    def kvs_usage(self, streams: list[str]) -> dict[str, float]:
        names = {
            "incoming_bytes": "PutMedia.IncomingBytes",
            "hls_out_bytes": "GetMP4MediaFragment.OutgoingBytes",
            "getclip_out_bytes": "GetClip.OutgoingBytes",
            "getmedia_out_bytes": "GetMedia.OutgoingBytes",
            "images_lowres": "GetImages.LowRes.ImagesCount",
            "images_highres": "GetImages.HighRes.ImagesCount",
        }
        out = {k: 0.0 for k in names}
        for s in streams:
            for k, metric in names.items():
                out[k] += self.metric_sum("AWS/KinesisVideo", metric, {"StreamName": s})
        return out

    def s3_listing(self, bucket: str, prefixes: list[str]) -> dict[str, Any]:
        s3 = self.client("s3")
        by_kind: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "bytes": 0})
        for pre in prefixes:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=pre):
                for o in page.get("Contents", []):
                    kind = o["Key"].rsplit("/", 1)[-1]
                    by_kind[kind]["count"] += 1
                    by_kind[kind]["bytes"] += o["Size"]
        return {"by_kind": {k: dict(v) for k, v in by_kind.items()},
                "count": sum(v["count"] for v in by_kind.values()),
                "bytes": sum(v["bytes"] for v in by_kind.values())}

    def media_prefixes(self, cameras: list[str]) -> list[str]:
        """期間内の時(hour)ごとの接頭辞。区間の時刻で置かれるので、期間の始まりと終わりの時で切れる。"""
        out = []
        t = self.start.replace(minute=0, second=0, microsecond=0)
        while t < self.end:
            out += [f"{c}/{t:%Y/%m/%d/%H}/" for c in cameras]
            t += timedelta(hours=1)
        return out

    def detections(self, cameras: list[str]) -> dict[str, Any]:
        s3 = self.client("s3")
        bucket = self.t.bucket("detections")
        days = []
        d = self.start.date()
        while datetime(d.year, d.month, d.day, tzinfo=UTC) < self.end:
            days.append(d)
            d += timedelta(days=1)
        files = {}
        for day in days:
            for cam in cameras:
                key = f"detections/dt={day:%Y-%m-%d}/{cam}.parquet"
                h = self._safe(f"s3 head {key}", lambda key=key: s3.head_object(Bucket=bucket, Key=key))
                if h:
                    files[key] = h["ContentLength"]
        noncurrent = {"count": 0, "bytes": 0}
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix="detections/"):
            for v in page.get("Versions", []):
                if not v.get("IsLatest"):
                    noncurrent["count"] += 1
                    noncurrent["bytes"] += v["Size"]
        return {"files": files, "days": [str(x) for x in days], "noncurrent": noncurrent}

    def cloudfront(self) -> dict[str, Any] | None:
        cf = self.client("cloudfront", "us-east-1")
        media_origin = f"{self.t.bucket('media')}.s3"
        dist_id = None
        for page in cf.get_paginator("list_distributions").paginate():
            for d in (page.get("DistributionList") or {}).get("Items", []) or []:
                if any(o["DomainName"].startswith(media_origin) for o in d["Origins"]["Items"]):
                    dist_id = d["Id"]
        if not dist_id:
            self.warnings.append("CloudFront: メディアバケットをオリジンに持つディストリビューションが見つからない")
            return None
        dims = {"DistributionId": dist_id, "Region": "Global"}
        return {
            "distribution_id": dist_id,
            "bytes_downloaded": self.metric_sum("AWS/CloudFront", "BytesDownloaded", dims, region="us-east-1"),
            "requests": self.metric_sum("AWS/CloudFront", "Requests", dims, region="us-east-1"),
        }

    def apigw(self) -> dict[str, Any] | None:
        api = self.client("apigatewayv2")
        for a in api.get_apis().get("Items", []):
            if a["Name"] == self.t.api_name:
                return {
                    "api_id": a["ApiId"],
                    "count": self.metric_sum("AWS/ApiGateway", "Count", {"ApiId": a["ApiId"]}),
                    "access_log_bytes": self.metric_sum("AWS/Logs", "IncomingBytes",
                                                        {"LogGroupName": f"/aws/apigateway/{self.t.api_name}"}),
                }
        self.warnings.append(f"API Gateway: {self.t.api_name} が見つからない")
        return None

    def dynamodb_metrics(self) -> dict[str, Any]:
        dims = {"TableName": self.t.table}
        desc = self.client("dynamodb").describe_table(TableName=self.t.table)["Table"]
        return {
            "rcu": self.metric_sum("AWS/DynamoDB", "ConsumedReadCapacityUnits", dims),
            "wcu": self.metric_sum("AWS/DynamoDB", "ConsumedWriteCapacityUnits", dims),
            "table_size_bytes": desc.get("TableSizeBytes", 0),
            "item_count": desc.get("ItemCount", 0),
        }

    def athena(self) -> dict[str, Any]:
        """ワークグループの ProcessedBytes。ディメンションが QueryState / QueryType 付きなので SEARCH で合計する。"""
        cw = self.client("cloudwatch")
        expr = (f"SUM(SEARCH('{{AWS/Athena,QueryState,QueryType,WorkGroup}} MetricName=\"ProcessedBytes\" "
                f"WorkGroup=\"{self.t.workgroup}\"', 'Sum', 3600))")
        r = cw.get_metric_data(MetricDataQueries=[{"Id": "a", "Expression": expr, "Period": 3600}],
                               StartTime=self.start, EndTime=self.end)
        return {"processed_bytes": sum(sum(x.get("Values", [])) for x in r["MetricDataResults"])}

    def search_logs(self) -> dict[str, Any]:
        """api Lambda のログの `search {...}` 行(lambdas/api/handler.py)から、質問ごとの usage を集める。"""
        logs = self.client("logs")
        group = f"/aws/lambda/{self.t.function('api')}"
        agg: dict[str, Any] = defaultdict(float)
        by_model: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        kw: dict[str, Any] = {"logGroupName": group, "filterPattern": '"search"',
                              "startTime": int(self.start.timestamp() * 1000), "endTime": int(self.end.timestamp() * 1000)}
        while True:
            r = logs.filter_log_events(**kw)
            for ev in r.get("events", []):
                usage = parse_search_log(ev["message"])
                if usage is None:
                    continue
                agg["questions"] += 1
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens",
                          "athena_bytes_scanned", "elapsed_ms", "tool_calls", "bedrock_calls"):
                    agg[k] += float(usage.get(k) or 0)
                mid = usage.get("model_id") or "unknown"
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens"):
                    by_model[mid][k] += float(usage.get(k) or 0)
            if not r.get("nextToken"):
                break
            kw["nextToken"] = r["nextToken"]
        out = dict(agg)
        out["by_model"] = {k: dict(v) for k, v in by_model.items()}
        return out

    def iot(self) -> dict[str, Any]:
        iot = self.client("iot")
        things = []
        kw: dict[str, Any] = {}
        while True:
            r = iot.list_things(**kw)
            things += [t["thingName"] for t in r.get("things", []) if t["thingName"].startswith(self.t.thing_prefix)]
            if not r.get("nextToken"):
                break
            kw["nextToken"] = r["nextToken"]
        # IoT Core のメトリクスはアカウント全体(Thing ごとには取れない)
        return {
            "gateways": things,
            "publish_in": self.metric_sum("AWS/IoT", "PublishIn.Success", {"Protocol": "MQTT"}),
            "publish_out": self.metric_sum("AWS/IoT", "PublishOut.Success", {"Protocol": "MQTT"}),
            "connect": self.metric_sum("AWS/IoT", "Connect.Success", {"Protocol": "MQTT"}),
            "rules": self.metric_sum("AWS/IoT", "RulesExecuted", {}),
        }

    def collect(self) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "project": self.t.project, "account_id": self.t.account_id, "region": self.t.region,
            "start": iso(self.start), "end": iso(self.end),
            "period_seconds": (self.end - self.start).total_seconds(),
        }
        cams = self._safe("kvs list_streams", self.cameras, []) or []
        raw["cameras"] = cams
        cam_ids = [c["camera_id"] for c in cams]
        raw["dynamodb_minutes"] = self._safe("dynamodb query", lambda: self.dynamodb_minutes(cam_ids), {})
        raw["lambda"] = self._safe("lambda", self.lambda_usage, {})
        raw["kvs"] = self._safe("kvs metrics", lambda: self.kvs_usage([c["stream"] for c in cams]), {})
        raw["s3_media"] = self._safe("s3 media", lambda: self.s3_listing(self.t.bucket("media"), self.media_prefixes(cam_ids)), {})
        raw["s3_detections"] = self._safe("s3 detections", lambda: self.detections(cam_ids), {})
        raw["cloudfront"] = self._safe("cloudfront", self.cloudfront)
        raw["apigw"] = self._safe("apigateway", self.apigw)
        raw["dynamodb"] = self._safe("dynamodb metrics", self.dynamodb_metrics, {})
        raw["athena"] = self._safe("athena", self.athena, {})
        raw["search"] = self._safe("search logs", self.search_logs, {})
        raw["iot"] = self._safe("iot", self.iot, {})
        raw["warnings"] = self.warnings
        return raw


SEARCH_RE = re.compile(r"\bsearch (\{.*\})\s*$", re.DOTALL)


def parse_search_log(message: str) -> dict[str, Any] | None:
    mm = SEARCH_RE.search(message)
    if not mm:
        return None
    try:
        rec = json.loads(mm.group(1))
    except json.JSONDecodeError:
        return None
    return rec.get("usage") if isinstance(rec, dict) else None


def collect(session: Any, project: str, start: datetime, end: datetime, region: str = "ap-northeast-1") -> dict[str, Any]:
    account = session.client("sts", region_name=region).get_caller_identity()["Account"]
    return Collector(session, Target(project, account, region), start, end).collect()


# ---------------------------------------------------------------------------
# 生の値 → 明細
# ---------------------------------------------------------------------------
@dataclass
class MeasureOptions:
    media_retention_days: float | None = None  # None なら video_archiver の環境変数 MEDIA_RETENTION_DAYS(なければ 30)
    detections_retention_days: float | None = None  # None なら media と同じとみなす(現行版に期限がないため)
    status_interval_s: float = 30.0
    glue_requests_per_query: float = 3.0
    athena_min_mb: float = 10.0


def to_lines(raw: dict[str, Any], opt: MeasureOptions | None = None) -> tuple[list[Line], list[str]]:
    opt = opt or MeasureOptions()
    notes: list[str] = []
    period = float(raw["period_seconds"])
    period_days = period / 86400
    n = max(len(raw.get("cameras") or []), 1)
    if not raw.get("cameras"):
        notes.append("KVS ストリームが見つからないので台数を 1 とした")

    def month(v: float) -> float:
        return m.to_month(v, period)

    def per_cam(v: float) -> float:
        return month(v) / n

    L: list[Line] = []
    lam = raw.get("lambda") or {}
    dmin = raw.get("dynamodb_minutes") or {}
    kvs = raw.get("kvs") or {}
    env = (lam.get("video_archiver") or {}).get("env", {})
    media_ret = opt.media_retention_days or float(env.get("MEDIA_RETENTION_DAYS", 30))
    det_ret = opt.detections_retention_days or media_ret
    retention_h = max([c.get("retention_hours", 1) for c in raw.get("cameras") or []] or [1])

    def lambda_lines(cat: str, key: str, label: str, *, share: float = 1.0, duration_override_ms: float | None = None,
                     invocations_override: float | None = None, scale: str = "camera") -> None:
        f = lam.get(key)
        if not f:
            notes.append(f"Lambda {key} が見つからないので明細を出していない")
            return
        arch = f["architecture"]
        dur = duration_override_ms if duration_override_ms is not None else f["duration_ms"] * share
        inv = invocations_override if invocations_override is not None else f["invocations"] * share
        conv = per_cam if scale == "camera" else month
        L.append(Line(cat, f"Lambda {label} 実行", f"lambda.gb_s.{arch}", conv(m.lambda_gb_seconds(f["memory_mb"], dur)),
                      scale, "実測", f"CloudWatch Duration 合計 × {f['memory_mb']} MB(関数の設定)"))
        if f["ephemeral_mb"] > 512:
            L.append(Line(cat, f"Lambda {label} エフェメラルストレージ", f"lambda.ephemeral_gb_s.{arch}",
                          conv(m.lambda_ephemeral_gb_seconds(f["ephemeral_mb"], dur)), scale, "実測",
                          f"{f['ephemeral_mb']} MB(512 MB 超の分)"))
        L.append(Line(cat, f"Lambda {label} リクエスト", f"lambda.requests.{arch}", conv(inv), scale, "実測",
                      "CloudWatch Invocations"))
        L.append(Line(cat, f"CloudWatch Logs({label})", "logs.vended_ingest_gb", conv(f["log_bytes"] * share / GB), scale,
                      "実測", "AWS/Logs IncomingBytes"))

    # --- 取り込み・一時保持 ---
    inc = float(kvs.get("incoming_bytes", 0))
    L.append(Line("取り込み", "KVS 取り込み", "kvs.ingest_gb", per_cam(inc / GB), "camera", "実測",
                  "PutMedia.IncomingBytes"))
    avg_stored = inc / period * 3600 * retention_h if period else 0
    L.append(Line("一時保持", "KVS 保存", "kvs.storage_gb_month", avg_stored / GB / n, "camera", "実測",
                  f"取り込み量 × 保持 {retention_h} 時間(DescribeStream)"))

    # --- 動画抽出 ---
    hls_total = float(kvs.get("hls_out_bytes", 0))
    archiver_bytes = min(float(dmin.get("bytes", 0)), hls_total) if hls_total else float(dmin.get("bytes", 0))
    L.append(Line("動画抽出", "KVS HLS 取り出し(clip.mp4 用)", "kvs.hls_out_gb", per_cam(archiver_bytes / GB), "camera",
                  "推定" if not hls_total else "実測",
                  "DynamoDB の bytes 合計(clip.mp4 ≒ 取り出した断片)。GetMP4MediaFragment.OutgoingBytes から再生分と分ける"))
    for k, pk, label in (("getclip_out_bytes", "kvs.getclip_out_gb", "GetClip"),
                         ("getmedia_out_bytes", "kvs.getmedia_out_gb", "GetMedia")):
        if kvs.get(k):
            L.append(Line("動画抽出", f"KVS {label} 取り出し", pk, per_cam(kvs[k] / GB), "camera", "実測", f"{label}.OutgoingBytes"))
    images = float(kvs.get("images_lowres", 0))
    det_frames = float(dmin.get("frame_count", 0))
    det_done = float((dmin.get("statuses") or {}).get("detect_status:DONE", 0))
    # 検出の GetImages は区間の終わりの 1 枚も返す(推論しないが課金される)ので、DONE の区間ごとに 1 枚足す
    det_images = det_frames + det_done if det_frames else 0.0
    if images and det_images > images:
        det_images = images
    sprite_images = max(images - det_images, 0)
    L.append(Line("動画抽出", "KVS GetImages(スプライト)", "kvs.getimages_lowres", per_cam(sprite_images), "camera", "実測",
                  "GetImages.LowRes.ImagesCount − 検出の分(frame_count + DONE の区間数)"))
    if kvs.get("images_highres"):
        L.append(Line("動画抽出", "KVS GetImages(1080p 超)", "kvs.getimages_highres", per_cam(kvs["images_highres"]),
                      "camera", "実測", "GetImages.HighRes.ImagesCount"))
    lambda_lines("動画抽出", "video_archiver", "video_archiver")
    if "video_archiver" in lam:
        L.append(Line("動画抽出", "EventBridge Scheduler(動画)", "scheduler.invocations",
                      per_cam(lam["video_archiver"]["invocations"]), "camera", "推定",
                      "Lambda の起動回数 = スケジュールの起動回数(再試行なしの設定)"))

    # --- 加工・保存 ---
    media = raw.get("s3_media") or {}
    media_per_day = float(media.get("bytes", 0)) / period_days if period_days else 0
    L.append(Line("加工・保存", "S3 保存(動画、スプライト、プレイリスト)", "s3.storage_gb_month",
                  m.steady_state_gb_month(media_per_day, media_ret) / n, "camera", "実測",
                  f"期間内のオブジェクトの合計(ListObjectsV2)÷ 日数 × 保存 {media_ret:g} 日(定常状態)"))
    L.append(Line("加工・保存", "S3 PUT(メディア)", "s3.put", per_cam(float(media.get("count", 0))), "camera", "推定",
                  "期間内のオブジェクト数 = PUT 回数(再試行の上書きは数えていない)"))
    ddb = raw.get("dynamodb") or {}
    if ddb:
        L.append(Line("加工・保存", "DynamoDB 書き込み(テーブル全体)", "dynamodb.wru", per_cam(ddb.get("wcu", 0)), "camera",
                      "実測", "ConsumedWriteCapacityUnits(動画の退避と検出の両方を含む)"))
        items = max(float(ddb.get("item_count", 0)), 1)
        avg_item = float(ddb.get("table_size_bytes", 0)) / items
        L.append(Line("加工・保存", "DynamoDB 保存(区間の件)", "dynamodb.storage_gb_month",
                      m.steady_state_gb_month(avg_item * 1440, media_ret), "camera", "推定",
                      f"平均 {avg_item:.0f} バイト / 件(DescribeTable)× 1,440 件 / 日 × {media_ret:g} 日"))

    # --- 物体検出 ---
    if det_frames or "detector" in lam:
        L.append(Line("物体検出", "KVS GetImages(推論用)", "kvs.getimages_lowres", per_cam(det_images), "camera", "推定",
                      "DynamoDB の frame_count 合計 + DONE の区間数(区間の終わりの 1 枚)"))
        lambda_lines("物体検出", "detector", "detector")
        if "detector" in lam:
            L.append(Line("物体検出", "EventBridge Scheduler(検出)", "scheduler.invocations",
                          per_cam(lam["detector"]["invocations"]), "camera", "推定", "Lambda の起動回数"))

    # --- 検出結果保存 ---
    det = raw.get("s3_detections") or {}
    files = det.get("files") or {}
    if files or "detector" in lam:
        days = max(len(det.get("days") or []), 1)
        per_day = sum(files.values()) / days / n if files else 0.0
        L.append(Line("検出結果保存", "S3 保存(検出結果の現行版)", "s3.storage_gb_month",
                      m.steady_state_gb_month(per_day, det_ret), "camera", "実測",
                      f"day ファイルの大きさ(HeadObject)の 1 日平均 × {det_ret:g} 日(現行版に期限がないので仮定)"))
        nc = det.get("noncurrent") or {}
        L.append(Line("検出結果保存", "S3 保存(検出結果の旧バージョン)", "s3.storage_gb_month",
                      float(nc.get("bytes", 0)) / GB / n, "camera", "実測",
                      "収集時点の旧バージョンの合計(1 日で消えるので、2 日以上動かした後なら定常状態)"))
        writes = float((dmin.get("statuses") or {}).get("detect_status:DONE", 0))
        L.append(Line("検出結果保存", "S3 PUT(day ファイルの書き直し)", "s3.put", per_cam(writes), "camera", "推定",
                      "detect_status=DONE の区間数(1 区間 1 回)"))
        L.append(Line("検出結果保存", "S3 GET(day ファイル)", "s3.get", per_cam(writes), "camera", "推定", "同上"))

    # --- 再生 ---
    viewer_hls = max(hls_total - archiver_bytes, 0)
    L.append(Line("再生", "KVS HLS 取り出し(視聴)", "kvs.hls_out_gb", per_cam(viewer_hls / GB), "camera", "実測",
                  "GetMP4MediaFragment.OutgoingBytes − 動画の退避の分"))
    cf = raw.get("cloudfront")
    gw = raw.get("apigw") or {}
    api_count = float(gw.get("count", 0))
    if cf:
        L.append(Line("再生", "CloudFront データ転送", "cloudfront.out_gb", per_cam(cf["bytes_downloaded"] / GB), "camera",
                      "実測", "BytesDownloaded(ビューワーの静的ファイルと /api を含む。全リージョン分を日本の単価で計算)"))
        L.append(Line("再生", "CloudFront リクエスト", "cloudfront.https_requests", per_cam(cf["requests"]), "camera", "実測",
                      "Requests(すべて HTTPS とみなす)"))
        origin_get = max(cf["requests"] - api_count, 0)
        L.append(Line("再生", "S3 GET(CloudFront のオリジン取得)", "s3.get", per_cam(origin_get), "camera", "推定",
                      "CloudFront のリクエスト − API の数(キャッシュミス率 1 とみなした上限。S3 のリクエストメトリクスは未設定)"))
        L.append(Line("再生", "S3 → CloudFront 転送", "s3.to_cloudfront_gb", per_cam(cf["bytes_downloaded"] / GB), "camera",
                      "推定", "無料"))
    if gw:
        L.append(Line("再生", "API Gateway(HTTP API)", "apigw.http_requests", per_cam(api_count), "camera", "実測",
                      "AWS/ApiGateway Count"))
        if gw.get("access_log_bytes"):
            L.append(Line("再生", "CloudWatch Logs(API Gateway のアクセスログ)", "logs.custom_ingest_gb",
                          per_cam(gw["access_log_bytes"] / GB), "camera", "実測", "AWS/Logs IncomingBytes"))
    search = raw.get("search") or {}
    api = lam.get("api")
    search_ms = float(search.get("elapsed_ms", 0))
    questions = float(search.get("questions", 0))
    if api:
        rest_ms = max(api["duration_ms"] - search_ms, 0)
        rest_inv = max(api["invocations"] - questions, 0)
        share = rest_ms / api["duration_ms"] if api["duration_ms"] else 1.0
        lambda_lines("再生", "api", "api", share=share, duration_override_ms=rest_ms, invocations_override=rest_inv)
    if ddb:
        L.append(Line("再生", "DynamoDB 読み込み(テーブル全体)", "dynamodb.rru", per_cam(ddb.get("rcu", 0)), "camera", "実測",
                      "ConsumedReadCapacityUnits(主に API の timeline)"))

    # --- 検索(質問数で決まるので台数によらない) ---
    for mid, u in (search.get("by_model") or {}).items():
        try:
            bp = bedrock_price_prefix(mid)
        except KeyError as e:
            notes.append(str(e))
            continue
        for k, kind in (("input_tokens", "input"), ("output_tokens", "output"),
                        ("cache_read_input_tokens", "cache_read"), ("cache_write_input_tokens", "cache_write")):
            if u.get(k):
                L.append(Line("検索", f"Bedrock {kind}({mid})", f"{bp}.{kind}", month(u[k] / 1e6), "fixed", "実測",
                              "api のログの search 行の usage"))
    athena_bytes = float((raw.get("athena") or {}).get("processed_bytes", 0)) or float(search.get("athena_bytes_scanned", 0))
    queries = float(search.get("tool_calls", 0))
    min_bytes = queries * opt.athena_min_mb * 1e6
    if queries or athena_bytes:
        L.append(Line("検索", "Athena スキャン(最低課金の分)", "athena.scan_tb", month(min_bytes / 1e12),
                      "fixed", "推定", f"{month(queries):,.0f} クエリ / 月 × {opt.athena_min_mb:g} MB(安全側の仮定)"))
        L.append(Line("検索", "Athena スキャン(最低課金を超えた分)", "athena.scan_tb",
                      per_cam(max(athena_bytes - min_bytes, 0) / 1e12), "camera", "実測",
                      "ワークグループの ProcessedBytes(1 質問のスキャン量は台数に比例するとみなす)"))
        L.append(Line("検索", "Glue Data Catalog リクエスト", "glue.catalog_requests",
                      month(queries * opt.glue_requests_per_query), "fixed", "推定",
                      f"{opt.glue_requests_per_query:g} 回 / クエリ(CloudTrail を見ないと数えられない)"))
    if api and questions:
        lambda_lines("検索", "api", "api(検索)", share=search_ms / api["duration_ms"] if api["duration_ms"] else 0,
                     duration_override_ms=search_ms, invocations_override=questions, scale="fixed")

    # --- 制御 ---
    io = raw.get("iot") or {}
    gws = len(io.get("gateways") or [])
    if not gws:
        notes.append(f"IoT の Thing({raw.get('project')}-gw-*)が見つからないので、接続とシャドウの明細を出していない")
    else:
        L.append(Line("制御", "IoT Core 接続", "iot.connection_minutes", per_cam(gws * period / 60), "camera", "推定",
                      f"ゲートウェイ {gws} 台が期間中ずっと接続していたとみなす"))
    if io:
        msgs = float(io.get("publish_in", 0)) + float(io.get("publish_out", 0))
        L.append(Line("制御", "IoT Core メッセージ", "iot.messages", per_cam(msgs), "camera", "実測",
                      "PublishIn.Success + PublishOut.Success(アカウント全体。5 KB 超の分割は数えていない)"))
        if io.get("rules"):
            L.append(Line("制御", "IoT Core ルール", "iot.rules", per_cam(io["rules"]), "camera", "実測", "RulesExecuted"))
    if gws:
        L.append(Line("制御", "IoT Core シャドウ更新(reported)", "iot.shadow_ops",
                      per_cam(gws * period / opt.status_interval_s), "camera", "推定",
                      f"{opt.status_interval_s:g} 秒ごとに last_seen を更新(docs/contracts.md)"))

    st = dmin.get("statuses") or {}
    notes.append(f"期間 {raw['start']} 〜 {raw['end']}({period_days:.2f} 日)、カメラ {n} 台、区間の件 {dmin.get('minutes', 0):,.0f} 件 "
                 f"{dict(sorted(st.items()))}")
    if dmin.get("video_duration_ms") and dmin.get("minutes"):
        notes.append(f"区間あたり: video_duration_ms 平均 {dmin['video_duration_ms'] / dmin['minutes']:.0f} ms、"
                     f"bytes 平均 {dmin.get('bytes', 0) / dmin['minutes'] / 1e6:.2f} MB、"
                     f"detect_duration_ms 平均 {dmin.get('detect_duration_ms', 0) / dmin['minutes']:.0f} ms")
    if period_days < 1:
        notes.append("期間が 1 日未満。日次でしか出ないメトリクス(S3 のサイズ)や、日をまたぐ挙動(旧バージョンの削除)が反映されない")
    for w in raw.get("warnings") or []:
        notes.append(f"取得できなかった: {w}")
    return L, notes

"""実測モードの動作確認。AWS は moto で模擬する(実環境には接続しない)。"""

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from cost import measure
from cost.measure import MeasureOptions, parse_search_log, to_lines
from cost.model import GB, compute
from cost.prices import PriceBook

P = "cc-test"
REGION = "ap-northeast-1"
# CloudWatch Logs(moto も)は未来や 14 日より前のイベントを受け付けないので、前日の 10:00 UTC にする
START = (datetime.now(UTC) - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
DT = f"{START:%Y-%m-%d}"
END = START + timedelta(hours=2)


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        yield boto3.Session(region_name=REGION)


def _zip() -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("handler.py", "def lambda_handler(e, c):\n    return 1\n")
    return b.getvalue()


def setup_env(s: boto3.Session) -> str:
    acct = s.client("sts").get_caller_identity()["Account"]
    # KVS
    s.client("kinesisvideo").create_stream(StreamName=f"{P}-cam-001", DataRetentionInHours=1)
    # DynamoDB
    ddb = s.client("dynamodb")
    ddb.create_table(TableName=f"{P}-state", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "camera_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                     AttributeDefinitions=[{"AttributeName": "camera_id", "AttributeType": "S"},
                                           {"AttributeName": "sk", "AttributeType": "S"}])
    tbl = s.resource("dynamodb").Table(f"{P}-state")
    tbl.put_item(Item={"camera_id": "cam-001", "sk": "CURSOR#VIDEO", "processed_until": "2026-10-24T12:00:00Z"})
    for i in range(120):
        t = START + timedelta(minutes=i)
        tbl.put_item(Item={
            "camera_id": "cam-001", "sk": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "video_status": "DONE", "detect_status": "DONE",
            "fragment_count": 60, "bytes": 4_500_000, "video_duration_ms": 1900, "video_attempts": 1,
            "frame_count": 60, "detect_count": 120, "detect_duration_ms": 15000,
        })
    # 期間外の件(数えない)
    tbl.put_item(Item={"camera_id": "cam-001", "sk": END.strftime("%Y-%m-%dT%H:%M:%SZ"), "video_status": "DONE", "bytes": 999})
    # S3: メディア(2 時間 × 3 ファイル)と検出結果(旧バージョン 1 つ)
    s3 = s.client("s3")
    for kind in ("media", "detections"):
        s3.create_bucket(Bucket=f"{P}-{kind}-{acct}", CreateBucketConfiguration={"LocationConstraint": REGION})
    for i in range(120):
        t = START + timedelta(minutes=i)
        pre = f"cam-001/{t:%Y/%m/%d/%H/%M}/"
        s3.put_object(Bucket=f"{P}-media-{acct}", Key=pre + "clip.mp4", Body=b"x" * 4500)
        s3.put_object(Bucket=f"{P}-media-{acct}", Key=pre + "sprite.jpg", Body=b"x" * 80)
        s3.put_object(Bucket=f"{P}-media-{acct}", Key=pre + "index.m3u8", Body=b"x" * 3)
    s3.put_object(Bucket=f"{P}-media-{acct}", Key=f"cam-001/{END:%Y/%m/%d/%H/%M}/clip.mp4", Body=b"x" * 7)  # 期間外
    det = f"{P}-detections-{acct}"
    s3.put_bucket_versioning(Bucket=det, VersioningConfiguration={"Status": "Enabled"})
    s3.put_object(Bucket=det, Key=f"detections/dt={DT}/cam-001.parquet", Body=b"y" * 100)
    s3.put_object(Bucket=det, Key=f"detections/dt={DT}/cam-001.parquet", Body=b"y" * 200)
    # Lambda
    role = s.client("iam").create_role(RoleName="r", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    lm = s.client("lambda")
    for name, mem in (("video-archiver", 512), ("detector", 3008), ("api", 512)):
        lm.create_function(FunctionName=f"{P}-{name}", Runtime="python3.13", Role=role, Handler="handler.lambda_handler",
                           Code={"ZipFile": _zip()}, MemorySize=mem, Architectures=["arm64"],
                           EphemeralStorage={"Size": 1024 if name != "api" else 512},
                           Environment={"Variables": {"MEDIA_RETENTION_DAYS": "30"}})
    # CloudWatch のメトリクス(期間内に 1 点ずつ)
    cw = s.client("cloudwatch")
    ts = START + timedelta(minutes=30)

    def put(ns, name, dims, value):
        cw.put_metric_data(Namespace=ns, MetricData=[{"MetricName": name, "Timestamp": ts, "Value": value,
                                                      "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]}])

    stream = {"StreamName": f"{P}-cam-001"}
    put("AWS/KinesisVideo", "PutMedia.IncomingBytes", stream, 540_000_000.0)  # 2 時間
    put("AWS/KinesisVideo", "GetMP4MediaFragment.OutgoingBytes", stream, 540_000_000.0 + 10_000_000)
    put("AWS/KinesisVideo", "GetImages.LowRes.ImagesCount", stream, 120 * 61 + 120 * 61)
    for name, inv, dur in (("video-archiver", 120, 120 * 1900), ("detector", 120, 120 * 15000), ("api", 50, 50 * 200 + 8000)):
        put("AWS/Lambda", "Invocations", {"FunctionName": f"{P}-{name}"}, inv)
        put("AWS/Lambda", "Duration", {"FunctionName": f"{P}-{name}"}, dur)
        put("AWS/Logs", "IncomingBytes", {"LogGroupName": f"/aws/lambda/{P}-{name}"}, inv * 1500)
    put("AWS/DynamoDB", "ConsumedWriteCapacityUnits", {"TableName": f"{P}-state"}, 120 * 18)
    put("AWS/DynamoDB", "ConsumedReadCapacityUnits", {"TableName": f"{P}-state"}, 500)
    put("AWS/IoT", "PublishIn.Success", {"Protocol": "MQTT"}, 240)
    # 検索のログ(lambdas/api/handler.py の形式)
    logs = s.client("logs")
    group = f"/aws/lambda/{P}-api"
    logs.create_log_group(logGroupName=group)
    logs.create_log_stream(logGroupName=group, logStreamName="s")
    usage = {"input_tokens": 8000, "output_tokens": 500, "total_tokens": 8500, "model_id": "jp.amazon.nova-2-lite-v1:0",
             "bedrock_calls": 2, "tool_calls": 1, "athena_bytes_scanned": 4_000_000, "elapsed_ms": 8000}
    msg = "[INFO]\t2026-10-24T10:30:00.000Z\treq-1\tsearch " + json.dumps({"question": "q", "sql": "s", "usage": usage})
    logs.put_log_events(logGroupName=group, logStreamName="s",
                        logEvents=[{"timestamp": int(ts.timestamp() * 1000), "message": msg},
                                   {"timestamp": int(ts.timestamp() * 1000), "message": "[INFO] other line"}])
    # IoT と API Gateway
    s.client("iot").create_thing(thingName=f"{P}-gw-001")
    s.client("iot").create_thing(thingName="someone-else-gw-001")
    s.client("apigatewayv2").create_api(Name=f"{P}-api", ProtocolType="HTTP")
    return acct


def test_collect_and_lines(aws):
    setup_env(aws)
    raw = measure.collect(aws, P, START, END, REGION)
    assert [c["camera_id"] for c in raw["cameras"]] == ["cam-001"]
    d = raw["dynamodb_minutes"]
    assert d["minutes"] == 120 and d["bytes"] == 120 * 4_500_000 and d["frame_count"] == 120 * 60
    assert d["statuses"]["detect_status:DONE"] == 120
    assert raw["lambda"]["detector"]["memory_mb"] == 3008
    assert raw["lambda"]["detector"]["ephemeral_mb"] == 1024
    assert raw["lambda"]["video_archiver"]["invocations"] == 120
    assert raw["kvs"]["incoming_bytes"] == 540_000_000
    assert raw["s3_media"]["count"] == 360
    assert raw["s3_media"]["by_kind"]["clip.mp4"]["bytes"] == 120 * 4500
    assert raw["s3_detections"]["files"] == {f"detections/dt={DT}/cam-001.parquet": 200}
    assert raw["s3_detections"]["noncurrent"] == {"count": 1, "bytes": 100}
    assert raw["search"]["questions"] == 1 and raw["search"]["input_tokens"] == 8000
    assert raw["iot"]["gateways"] == [f"{P}-gw-001"]
    assert raw["apigw"]["api_id"]
    json.dumps(raw, default=str)  # 保存できる

    lines, notes = to_lines(raw)
    by = {x.label: x for x in lines}
    f = 30 * 24 / 2  # 2 時間 → 30 日
    assert by["KVS 取り込み"].qty == pytest.approx(540_000_000 * f / GB)
    assert by["KVS 取り込み"].source == "実測"
    # 保持 1 時間の平均保存量 = 1 時間あたりの取り込み量
    assert by["KVS 保存"].qty == pytest.approx(270_000_000 / GB)
    # 動画の退避の取り出し = DynamoDB の bytes、残りが視聴
    assert by["KVS HLS 取り出し(clip.mp4 用)"].qty == pytest.approx(540_000_000 * f / GB)
    assert by["KVS HLS 取り出し(視聴)"].qty == pytest.approx(10_000_000 * f / GB)
    # GetImages: 検出 = 60 枚 + 1 枚 / 区間、残りがスプライト
    assert by["KVS GetImages(推論用)"].qty == pytest.approx(120 * 61 * f)
    assert by["KVS GetImages(スプライト)"].qty == pytest.approx(120 * 61 * f)
    assert by["Lambda detector 実行"].qty == pytest.approx(3008 / 1024 * 120 * 15 * f)
    # S3 の保存は保存日数の定常状態: 1 日あたり(4583 × 120 バイト × 12)× 30 日
    per_day = 120 * 4583 * 12
    assert by["S3 保存(動画、スプライト、プレイリスト)"].qty == pytest.approx(per_day * 30 / GB)
    assert by["S3 PUT(メディア)"].qty == pytest.approx(360 * f)
    # 検索は台数によらない。api の実行時間から検索の分を引いて再生に回す
    assert by["Bedrock input(jp.amazon.nova-2-lite-v1:0)"].scale == "fixed"
    assert by["Bedrock input(jp.amazon.nova-2-lite-v1:0)"].qty == pytest.approx(8000 * f / 1e6)
    assert by["Lambda api(検索) 実行"].qty == pytest.approx(0.5 * 8 * f)
    assert by["Lambda api 実行"].qty == pytest.approx(0.5 * 50 * 0.2 * f)
    assert by["IoT Core 接続"].source == "推定"
    assert any("期間" in n for n in notes)

    r = compute(lines, PriceBook.load(), 1)
    assert r.total > 0 and all(v >= 0 for v in r.by_category.values())


def test_missing_resources_are_warnings(aws):
    raw = measure.collect(aws, "cc-none", START, END, REGION)
    assert raw["cameras"] == []
    lines, notes = to_lines(raw)
    assert any("台数を 1" in n for n in notes)
    assert compute(lines, PriceBook.load(), 1).total >= 0


def test_retention_option_overrides():
    raw = {"period_seconds": 86400, "start": "a", "end": "b", "cameras": [{"camera_id": "c", "retention_hours": 1}],
           "s3_media": {"bytes": GB, "count": 1}, "lambda": {}, "kvs": {}, "dynamodb_minutes": {}}
    lines, _ = to_lines(raw, MeasureOptions(media_retention_days=7))
    media = next(x for x in lines if x.label.startswith("S3 保存(動画"))
    assert media.qty == pytest.approx(7)


def test_parse_search_log():
    u = {"input_tokens": 1}
    assert parse_search_log("[INFO]\tt\tid\tsearch " + json.dumps({"usage": u}) + "\n") == u
    assert parse_search_log("search not-json") is None
    assert parse_search_log("other") is None

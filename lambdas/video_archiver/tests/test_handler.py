"""handler を moto(DynamoDB、S3)と KVS のスタブで通しで動かす。"""

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

import archiver
import handler
from archiver import Clip
from common import kvs
from common.timeutil import epoch_ms


class Ctx:
    aws_request_id = "req-xyz"

    def get_remaining_time_in_millis(self):
        return 300_000


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("STATE_TABLE", "st")
    monkeypatch.setenv("MEDIA_BUCKET", "media")
    monkeypatch.setenv("STREAM_NAME_PREFIX", "cc-")
    monkeypatch.setenv("LAG_SECONDS", "30")
    monkeypatch.setenv("MAX_MINUTES", "10")
    monkeypatch.setenv("MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MEDIA_RETENTION_DAYS", "7")
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        ddb.create_table(
            TableName="st",
            KeySchema=[{"AttributeName": "camera_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "camera_id", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3").create_bucket(Bucket="media", CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"})
        handler._archiver = None
        yield ddb.Table("st")
        handler._archiver = None


def test_handler_end_to_end(env, monkeypatch):
    now = datetime.now(timezone.utc)
    target_end = (now - timedelta(seconds=30)).replace(second=0, microsecond=0)
    first = target_end - timedelta(minutes=5)
    # 1 区間目と 3 区間目だけ録画がある
    frs = []
    for m in (0, 2):
        base = first + timedelta(minutes=m)
        frs += [kvs.Fragment(str(m * 100 + s), epoch_ms(base) + s * 1000, 0, 1000, 50) for s in range(60)]

    # 対象の最後の区間より後の断片(ウォーターマークが対象の区間をすべて越える)
    frs.append(kvs.Fragment("999", epoch_ms(target_end) + 5000, 0, 1000, 50))
    calls = []

    def lf(stream, start, end, selector="PRODUCER_TIMESTAMP"):
        assert stream == "cc-cam-001"
        calls.append((start, end))
        return [f for f in frs if epoch_ms(start) <= f.producer_ms < epoch_ms(end)]

    # run_pipeline がウォーターマーク用に 1 回だけ ListFragments を呼び、各区間の一覧はそのキャッシュから返る
    monkeypatch.setattr(kvs, "list_fragments", lf)

    def cb(stream, start, end, frags):
        return Clip(b"I" * 4 + b"F" * 2 * len(frags), 4, [[4 + 2 * i, 2, 1000] for i in range(len(frags))], frags)

    arch = archiver.Archiver(bucket="media", clip_builder=cb, get_images=lambda *a, **k: [])
    handler._archiver = arch

    res = handler.lambda_handler({"camera_id": "cam-001"}, Ctx())
    assert res["stopped"] == "caught_up"
    assert [m["status"] for m in res["minutes"]] == ["DONE", "EMPTY", "DONE", "EMPTY", "EMPTY"]
    assert len(calls) == 1 and calls[0][0] == first

    t = env
    cur = t.get_item(Key={"camera_id": "cam-001", "sk": "CURSOR#VIDEO"})["Item"]
    assert cur["processed_until"] == target_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    it = t.get_item(Key={"camera_id": "cam-001", "sk": first.strftime("%Y-%m-%dT%H:%M:%SZ")})["Item"]
    assert it["video_status"] == "DONE" and it["fragment_count"] == 60 and it["bytes"] == 124
    assert it["segments"] == [[epoch_ms(first), epoch_ms(first) + 60_000]]
    assert it["expires_at"] == int((first + timedelta(days=7)).timestamp())
    assert it["files"]["clip"].startswith("cam-001/")
    empty = t.get_item(Key={"camera_id": "cam-001", "sk": (first + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})["Item"]
    assert empty["video_status"] == "EMPTY" and empty["segments"] == []
    keys = [o["Key"] for o in boto3.client("s3").list_objects_v2(Bucket="media")["Contents"]]
    assert len(keys) == 6

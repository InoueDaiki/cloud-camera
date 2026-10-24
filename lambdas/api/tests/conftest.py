import json
import os
import sys
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import aws  # noqa: E402

TABLE = "test-state"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("PROJECT", "test")
    monkeypatch.setenv("STATE_TABLE", TABLE)
    monkeypatch.setenv("MEDIA_BUCKET", "test-media")
    monkeypatch.setenv("STREAM_NAME_PREFIX", "test-")
    monkeypatch.setenv("GLUE_DATABASE", "test_db")
    monkeypatch.setenv("GLUE_TABLE", "detections")
    monkeypatch.setenv("ATHENA_WORKGROUP", "test-wg")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")
    monkeypatch.setenv("IOT_DATA_ENDPOINT", "example-ats.iot.ap-northeast-1.amazonaws.com")
    monkeypatch.setenv("IOT_THING_PREFIX", "test-")
    monkeypatch.setenv("CAMERAS", json.dumps({"cam-001": "gw-001", "cam-002": "gw-001"}))
    aws.reset_caches()
    yield
    aws.reset_caches()


@pytest.fixture
def ddb():
    with mock_aws():
        res = boto3.resource("dynamodb", region_name="ap-northeast-1")
        t = res.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "camera_id", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "camera_id", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield t


def put_minute(t, cam, sk, status="DONE", segments=None, hls=None, files=None, pad=0):
    item = {"camera_id": cam, "sk": sk, "video_status": status}
    if segments is not None:
        item["segments"] = [[Decimal(a), Decimal(b)] for a, b in segments]
    if hls is not None:
        item["hls"] = {"init_length": Decimal(hls[0]),
                       "parts": [[Decimal(o), Decimal(l), Decimal(d)] for o, l, d in hls[1]]}
    if files is not None:
        item["files"] = files
    if pad:
        item["pad"] = "x" * pad  # ページ送りを起こすための詰め物
    t.put_item(Item=item)


def event(method, path, query=None, body=None):
    return {
        "version": "2.0",
        "rawPath": path,
        "queryStringParameters": query,
        "requestContext": {"http": {"method": method, "path": path}},
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }

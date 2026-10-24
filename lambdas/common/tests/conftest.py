import os
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

TABLE = "test-state"


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    # 実 AWS に絶対に行かないように、ダミーの認証情報にする
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.setenv("STATE_TABLE", TABLE)


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "camera_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "camera_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield t


class Clock:
    """テスト用の時計。"""

    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 24, 10, 30, 20, tzinfo=timezone.utc))


class FakeContext:
    def __init__(self, remaining_ms: int = 60_000, request_id: str = "req-1"):
        self.remaining = remaining_ms
        self.aws_request_id = request_id

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining


@pytest.fixture
def fake_context():
    return FakeContext


os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-1")

import json

import pytest
from conftest import event

import handler
from app import athena, aws, search


class FakeBedrock:
    """Converse のスタブ。scripted は [content のリスト] を順に返す。"""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []

    def converse(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=str)))
        content = self.scripted.pop(0)
        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": "tool_use",
            "usage": {"inputTokens": 1000, "outputTokens": 100, "totalTokens": 1100},
        }


def tool(name, tid, **inp):
    return {"toolUse": {"toolUseId": tid, "name": name, "input": inp}}


SQL = ("SELECT camera_id, min(ts) AS ts, label, max(confidence) AS confidence FROM detections "
       "WHERE dt BETWEEN '2026-10-23' AND '2026-10-24' AND label = 'person' GROUP BY camera_id, label")


@pytest.fixture
def fakes(monkeypatch):
    state = {"bedrock": None, "athena_sql": []}

    def fake_client(service, endpoint_url=None):
        assert service == "bedrock-runtime"
        return state["bedrock"]

    def fake_run(cfg, sql, **kw):
        state["athena_sql"].append(sql)
        return athena.QueryResult(
            columns=["camera_id", "ts", "label", "confidence"],
            types=["varchar", "timestamp", "varchar", "real"],
            rows=[["cam-001", "2026-10-24 00:12:34.500", "person", "0.91"]],
            bytes_scanned=12345, execution_ms=300, query_execution_id="q1")

    monkeypatch.setattr(aws, "client", fake_client)
    monkeypatch.setattr(athena, "run", fake_run)
    return state


def post(question):
    r = handler.lambda_handler(event("POST", "/api/search", body={"question": question}), None)
    return r["statusCode"], json.loads(r["body"])


def test_search_flow(fakes):
    fakes["bedrock"] = FakeBedrock([
        [{"text": "検索します"}, tool("query_detections", "t1", sql=SQL)],
        [tool("submit_answer", "t2", answer="10月24日 9時12分に人が映っています。",
              hits=[{"camera_id": "cam-001", "ts": "2026-10-24T00:12:34.500Z", "label": "person"},
                    {"camera_id": "cam-001", "ts": "2026-10-24T05:00:00Z"}])],  # 結果にない時刻は捨てる
    ])
    status, body = post("今日の午前中に人が映ったのはいつ?")
    assert status == 200, body
    assert body["answer"].startswith("10月24日")
    assert body["hits"] == [{"camera_id": "cam-001", "ts": "2026-10-24T00:12:34.500Z",
                             "ts_jst": "2026-10-24T09:12:34+09:00", "label": "person", "confidence": 0.91}]
    assert body["sql"].endswith("LIMIT 100")  # LIMIT を強制した SQL が実行された
    assert fakes["athena_sql"] == [body["sql"]]
    u = body["usage"]
    assert (u["input_tokens"], u["output_tokens"], u["tool_calls"], u["bedrock_calls"]) == (2000, 200, 1, 2)
    assert u["athena_bytes_scanned"] == 12345 and u["model_id"] == "test-model"

    calls = fakes["bedrock"].calls
    sys_text = calls[0]["system"][0]["text"]
    assert "日本時間" in sys_text and "UTC" in sys_text and "person" in sys_text and "dt" in sys_text
    assert "toothbrush" in sys_text  # COCO の全ラベル
    assert calls[0]["modelId"] == "test-model"
    assert calls[0]["toolConfig"]["toolChoice"] == {"any": {}}
    # モデルに返した結果: ts は ISO(UTC)と日本時間
    tr = calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["json"]
    assert tr["rows"][0]["ts"] == "2026-10-24T00:12:34.500Z"
    assert tr["rows"][0]["ts_jst"] == "2026-10-24 09:12:34"


def test_rejected_sql_is_returned_to_model(fakes):
    fakes["bedrock"] = FakeBedrock([
        [tool("query_detections", "t1", sql="SELECT * FROM detections")],
        [tool("query_detections", "t2", sql="DELETE FROM detections")],
        [tool("submit_answer", "t3", answer="わかりません", hits=[])],
    ])
    status, body = post("人は?")
    assert status == 200
    assert fakes["athena_sql"] == []  # 1 回も実行していない
    assert all(q["error"].startswith("rejected") for q in body["queries"])
    res = fakes["bedrock"].calls[1]["messages"][-1]["content"][0]["toolResult"]
    assert res["status"] == "error" and "dt" in res["content"][0]["text"]
    assert body["sql"] is None and body["hits"] == []


def test_tool_call_limit_forces_answer(fakes):
    many = [[tool("query_detections", f"t{i}", sql=SQL)] for i in range(search.MAX_QUERIES)]
    fakes["bedrock"] = FakeBedrock(many + [[tool("submit_answer", "z", answer="打ち切り", hits=[])]])
    status, body = post("ずっと検索して")
    assert status == 200
    assert body["usage"]["tool_calls"] == search.MAX_QUERIES
    calls = fakes["bedrock"].calls
    assert calls[-1]["toolConfig"]["toolChoice"] == {"tool": {"name": "submit_answer"}}
    assert len(fakes["athena_sql"]) == search.MAX_QUERIES


def test_text_only_answer(fakes):
    fakes["bedrock"] = FakeBedrock([[{"text": "検索できない質問です"}]])
    status, body = post("こんにちは")
    assert status == 200 and body["answer"] == "検索できない質問です"


def test_bad_question(fakes):
    r = handler.lambda_handler(event("POST", "/api/search", body={"question": ""}), None)
    assert r["statusCode"] == 400
    r = handler.lambda_handler(event("POST", "/api/search", body={"question": "あ" * 501}), None)
    assert r["statusCode"] == 400


def test_bedrock_error_shape(monkeypatch):
    from botocore.exceptions import ClientError

    class Boom:
        def converse(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no access"}}, "Converse")

    monkeypatch.setattr(aws, "client", lambda s, e=None: Boom())
    status, body = post("人は?")
    assert status == 502
    assert body["error"]["code"] == "BEDROCK_AccessDeniedException"

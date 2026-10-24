"""HTTP の入出力(API Gateway HTTP API ペイロード 2.0)とエラーの形。

エラーはすべて {"error": {"code": "...", "message": "..."}} で返す。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any


class ApiError(Exception):
    """クライアントへそのまま返すエラー。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def bad_request(message: str, code: str = "BAD_REQUEST") -> ApiError:
    return ApiError(400, code, message)


def not_found(message: str, code: str = "NOT_FOUND") -> ApiError:
    return ApiError(404, code, message)


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes
    path_params: dict[str, str]

    def json(self) -> Any:
        if not self.body:
            raise bad_request("リクエストボディが空です")
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise bad_request(f"JSON として読めません: {e}") from e

    @classmethod
    def from_event(cls, event: dict) -> "Request":
        http = (event.get("requestContext") or {}).get("http") or {}
        method = (http.get("method") or event.get("httpMethod") or "GET").upper()
        path = event.get("rawPath") or http.get("path") or "/"
        query = dict(event.get("queryStringParameters") or {})
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            body = base64.b64decode(raw)
        else:
            body = raw.encode("utf-8") if isinstance(raw, str) else raw
        return cls(method, path, query, headers, body, {})


def json_response(status: int, payload: Any, headers: dict | None = None) -> dict:
    h = {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"}
    if headers:
        h.update(headers)
    return {
        "statusCode": status,
        "headers": h,
        "body": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "isBase64Encoded": False,
    }


def text_response(status: int, text: str, content_type: str, headers: dict | None = None) -> dict:
    h = {"content-type": content_type}
    if headers:
        h.update(headers)
    return {"statusCode": status, "headers": h, "body": text, "isBase64Encoded": False}


def error_response(status: int, code: str, message: str) -> dict:
    return json_response(status, {"error": {"code": code, "message": message}})

"""Help Me Tidy 前端 HTTP 層：依 FRONTEND_API.md 處理 request_id、revision 與錯誤格式。

request_id 規則：
- 同一操作（路徑 + 內容 + expected_revision 相同）重送時沿用同一個 ID
- 逾時／網路錯誤／429／503／REQUEST_IN_PROGRESS：保留 ID，使用者再按一次即沿用原 ID
- 收到明確結果（成功或其他錯誤）：丟棄 ID，下一次操作產生新 ID
"""
import hashlib
import json
import mimetypes
import os
import uuid
from contextlib import ExitStack

import requests

BASE = os.getenv("TIDY_API_BASE", "http://127.0.0.1:8000/api/v1")
TIMEOUT = 130  # Agent 上限 120 秒，多留緩衝

# 這些情況不丟棄 request_id（結果未知或應原樣重試）
KEEP_ID = {"REQUEST_IN_PROGRESS", "RATE_LIMITED", "SERVICE_UNAVAILABLE", "NETWORK"}


class ApiError(Exception):
    def __init__(self, code, message, retryable=False, details=None, http=0, retry_after=None):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.details, self.http, self.retry_after = details or {}, http, retry_after


def call(S, op, method, path, body=None, files=None):
    """S 為前端 session dict；成功時回傳 JSON 並同步 S['revision']。"""
    sig = json.dumps(
        [op, path, body, [(os.path.basename(f), os.path.getsize(f)) for f in files or []]],
        sort_keys=True, ensure_ascii=False, default=str)
    key = hashlib.sha1(sig.encode()).hexdigest()
    rid = S["pending"].setdefault(key, "req_" + uuid.uuid4().hex[:12])
    payload = {"request_id": rid, **(body or {})}
    url = BASE + path
    try:
        if files:
            with ExitStack() as stack:
                parts = [("files", (os.path.basename(f), stack.enter_context(open(f, "rb")),
                                    mimetypes.guess_type(f)[0] or "application/octet-stream"))
                         for f in files]
                r = requests.post(url, data={k: str(v) for k, v in payload.items()},
                                  files=parts, timeout=TIMEOUT)
        elif method == "GET":
            r = requests.get(url, timeout=30)
        else:
            r = requests.request(method, url, json=payload, timeout=TIMEOUT)
    except (requests.Timeout, requests.ConnectionError):
        raise ApiError("NETWORK", "連線逾時或中斷，操作可能已執行。", retryable=True)

    if r.status_code >= 400:
        try:
            e = r.json()["error"]
        except Exception:
            e = {"code": f"HTTP_{r.status_code}", "message": "伺服器回應格式錯誤", "retryable": False}
        if e.get("code") not in KEEP_ID:
            S["pending"].pop(key, None)
        ra = r.headers.get("Retry-After")
        raise ApiError(e.get("code"), e.get("message", ""), e.get("retryable", False),
                       e.get("details"), r.status_code, int(ra) if ra and ra.isdigit() else None)

    S["pending"].pop(key, None)
    data = r.json()
    if isinstance(data, dict) and isinstance(data.get("revision"), int):
        S["revision"] = data["revision"]
    return data


def get_binary(path, max_bytes=10 * 1024 * 1024):
    """下載圖片（若後端提供）。成功回 (bytes, mime)，任何失敗回 None。"""
    try:
        r = requests.get(BASE + path, timeout=30)
    except requests.RequestException:
        return None
    mime = r.headers.get("content-type", "").split(";")[0]
    if r.status_code != 200 or mime not in ("image/jpeg", "image/png") or len(r.content) > max_bytes:
        return None
    return r.content, mime

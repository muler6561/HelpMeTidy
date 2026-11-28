"""前端自測用假後端（記憶體內）。執行：uvicorn mock_backend:app --port 8000

示範劇本：分析 → 確認 → 計畫 v1（課本→抽屜、筆電→書架上層）→ 完成筆電步驟
→ 回饋含「放不下」→ 計畫 v2（課本改放書架中層，筆電完成狀態保留）。
特殊輸入：plans 的需求留空 → needs_input；回饋含「fail」→ failed；含「在哪」→ answered。
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

app = FastAPI()
P = "/api/v1"
DB, CACHE = {}, {}
RN = {"region_01": "第一層抽屜", "region_02": "書架中層", "region_03": "書桌桌面", "region_04": "書架上層"}


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def err(status, code, msg, rid=None, retryable=False, **d):
    return JSONResponse({"request_id": rid, "error": {"code": code, "message": msg, "retryable": retryable, "details": d}},
                        status_code=status)


def ar(status, message, questions=None, plan=None, location=None, error=None):
    return {"status": status, "message": message, "questions": questions or [], "plan": plan,
            "location": location, "error": error}


def run(sid, rid, sig, expected, fn, status=200):
    s = DB.get(sid)
    if not s:
        return err(404, "SESSION_NOT_FOUND", "session 不存在", rid)
    if (sid, rid) in CACHE:
        csig, out, code = CACHE[(sid, rid)]
        return JSONResponse(out, status_code=code) if csig == sig else err(409, "IDEMPOTENCY_CONFLICT", "同 ID 不同內容", rid)
    if expected != s["revision"]:
        return err(409, "REVISION_CONFLICT", "資料已有更新，請重新載入。", rid, current_revision=s["revision"])
    out = fn(s)
    if isinstance(out, JSONResponse):
        return out
    s["revision"] += 1
    out.update(request_id=rid, session_id=sid, revision=s["revision"])
    CACHE[(sid, rid)] = (sig, out, status)
    return JSONResponse(out, status_code=status)


def log(s, rid, user, assistant):
    for role, text in (("user", user), ("assistant", assistant)):
        s["messages"].append({"message_id": "msg_" + uuid.uuid4().hex[:5], "role": role, "text": text, "created_at": now(), "request_id": rid})


def sig_of(b):
    return json.dumps({k: v for k, v in b.items() if k != "request_id"}, sort_keys=True, ensure_ascii=False)


@app.post(P + "/sessions")
async def create(req: Request):
    b = await req.json()
    sid = "ses_" + uuid.uuid4().hex[:6]
    DB[sid] = {"revision": 0, "scene": None, "plan": None, "locations": {}, "requirement": None, "images": [], "messages": []}
    return JSONResponse({"request_id": b["request_id"], "session_id": sid, "revision": 0}, status_code=201)


@app.post(P + "/sessions/{sid}/images")
async def images(sid: str, request_id: str = Form(...), expected_revision: int = Form(...), files: list[UploadFile] = File(...)):
    for f in files:
        if f.content_type not in ("image/jpeg", "image/png"):
            return err(415, "UNSUPPORTED_IMAGE_TYPE", "只接受 JPEG／PNG。", request_id)
    sig = json.dumps([f.filename for f in files])

    data = [await f.read() for f in files]
    for f in files:
        await f.seek(0)

    def fn(s):
        s["images"] = [{"image_id": "img_" + uuid.uuid4().hex[:4], "mime_type": f.content_type, "size_bytes": 0, "_name": f.filename, "_bytes": data[i]} for i, f in enumerate(files)]
        return {"images": [{k: v for k, v in i.items() if not k.startswith("_")} for i in s["images"]]}
    return run(sid, request_id, sig, expected_revision, fn, 201)


@app.post(P + "/sessions/{sid}/analysis")
async def analysis(sid: str, req: Request):
    b = await req.json()

    def fn(s):
        s["requirement"] = b["user_message"]
        if any("notroom" in i.get("_name", "") for i in s["images"] if i["image_id"] in b["image_ids"]):
            return {"scene": {"image_ids": b["image_ids"], "confirmed": False, "items": [], "regions": [], "relations": [],
                              "questions": [{"question_id": "q_x", "text": "這看起來不是房間照片。", "related_ids": []}]}}
        D = lambda w=None, d=None, h=None: {"width_cm": w, "depth_cm": d, "height_cm": h}
        s["scene"] = {"image_ids": b["image_ids"], "confirmed": False,
                      "items": [{"item_id": "item_01", "name": "課本", "observed_region_id": "region_03", "observation": "桌面左側一疊課本", "needs_confirmation": False},
                                {"item_id": "item_02", "name": "疑似筆電", "observed_region_id": "region_03", "observation": "被遮擋，種類待確認", "needs_confirmation": True}],
                      "regions": [{"region_id": "region_01", "name": "第一層抽屜", "availability": "unknown", "dimensions": D(), "needs_confirmation": True},
                                  {"region_id": "region_02", "name": "書架中層", "availability": "available", "dimensions": D(30, 25, 28), "needs_confirmation": False},
                                  {"region_id": "region_03", "name": "書桌桌面", "availability": "available", "dimensions": D(), "needs_confirmation": False},
                                  {"region_id": "region_04", "name": "書架上層", "availability": "available", "dimensions": D(), "needs_confirmation": False}],
                      "relations": [{"item_id": "item_01", "region_id": "region_03", "relation": "on"}],
                      "questions": [{"question_id": "q_01", "text": "被遮擋的是筆電嗎？抽屜是否可用？", "related_ids": ["item_02", "region_01"]}]}
        log(s, b["request_id"], b["user_message"], "已完成照片分析")
        return {"scene": s["scene"]}
    return run(sid, b["request_id"], sig_of(b), b["expected_revision"], fn)


@app.put(P + "/sessions/{sid}/scene/confirmation")
async def confirm(sid: str, req: Request):
    b = await req.json()

    def fn(s):
        if not s["scene"]:
            return err(409, "SCENE_NOT_CONFIRMED", "尚未分析。", b["request_id"])
        old = {x["item_id"] for x in s["scene"]["items"]} | {x["region_id"] for x in s["scene"]["regions"]}
        new = {x["item_id"] for x in b["scene"]["items"]} | {x["region_id"] for x in b["scene"]["regions"]}
        if old != new:
            return err(422, "INVALID_SCENE", "不可任意變更 ID。", b["request_id"])
        s["scene"] = {**b["scene"], "confirmed": True}
        return {"scene": s["scene"]}
    return run(sid, b["request_id"], sig_of(b), b["expected_revision"], fn)


def make_plan(s, drawer_ok, reason):
    old = s["plan"]
    done = {x["step_id"]: x for x in (old["steps"] if old else []) if x["status"] == "completed"}
    t1 = "region_01" if drawer_ok else "region_02"
    steps = [{"step_id": "step_01", "instruction": f"將課本移到{RN[t1]}", "item_ids": ["item_01"], "target_region_id": t1, "status": "pending", "completed_at": None},
             {"step_id": "step_02", "instruction": "將筆電移到書架上層", "item_ids": ["item_02"], "target_region_id": "region_04", "status": "pending", "completed_at": None}]
    for x in steps:
        d = done.get(x["step_id"])
        if d and d["target_region_id"] == x["target_region_id"]:
            x.update(status="completed", completed_at=d["completed_at"])
    s["plan"] = {"plan_id": "plan_01", "plan_version": (old["plan_version"] + 1) if old else 1,
                 "based_on_revision": s["revision"], "change_reason": reason,
                 "assignments": [{"item_id": "item_01", "region_id": t1, "reason": "方便取用", "fit_status": "unknown" if drawer_ok else "confirmed"},
                                 {"item_id": "item_02", "region_id": "region_04", "reason": "有插座", "fit_status": "unknown"}],
                 "steps": steps,
                 "pending_confirmations": ["尚未確認第一層抽屜是否放得下課本。"] if drawer_ok else []}
    return s["plan"]


@app.post(P + "/sessions/{sid}/plans")
async def plans(sid: str, req: Request):
    b = await req.json()

    def fn(s):
        if not (s["scene"] or {}).get("confirmed"):
            return err(409, "SCENE_NOT_CONFIRMED", "請先確認辨識。", b["request_id"])
        if s["plan"]:
            return err(409, "PLAN_ALREADY_EXISTS", "已有計畫。", b["request_id"])
        if not b["user_message"].strip():
            return ar("needs_input", "請先補充需求。", [{"question_id": "q_01", "text": "你最想先整理哪些物品？", "related_ids": []}])
        log(s, b["request_id"], b["user_message"], "先把課本與筆電移到指定位置。")
        return ar("plan_ready", "先把課本與筆電移到指定位置。", plan=make_plan(s, True, "初次規劃"))
    return run(sid, b["request_id"], sig_of(b), b["expected_revision"], fn)


@app.post(P + "/sessions/{sid}/messages")
async def messages(sid: str, req: Request):
    b = await req.json()
    t = b["user_message"]

    def fn(s):
        if "fail" in t:
            return ar("failed", "照片分析服務暫時無法回應，已保留進度。",
                      error={"code": "MODEL_TIMEOUT", "message": "已達本輪執行時限。", "retryable": True, "details": {}})
        if "放不下" in t and s["plan"]:
            log(s, b["request_id"], t, "已改放書架中層。")
            return ar("plan_ready", "已改放書架中層，筆電步驟維持完成。", plan=make_plan(s, False, "抽屜放不下，改用書架中層"))
        if "在哪" in t:
            loc = s["locations"].get("item_01")
            return ar("answered", "課本最後確認放在書架中層。" if loc else "沒有課本的確認位置紀錄。", location=loc)
        return ar("answered", "收到。")
    return run(sid, b["request_id"], sig_of(b), b["expected_revision"], fn)


@app.post(P + "/sessions/{sid}/steps/{step_id}/completion")
async def completion(sid: str, step_id: str, req: Request):
    b = await req.json()

    def fn(s):
        p = s["plan"]
        if not p or b["plan_version"] != p["plan_version"]:
            return err(409, "PLAN_VERSION_CONFLICT", "計畫版本已變更。", b["request_id"])
        st = next((x for x in p["steps"] if x["step_id"] == step_id), None)
        if not st:
            return err(422, "INVALID_REFERENCE", "步驟不存在。", b["request_id"])
        st.update(status="completed", completed_at=now())
        locs = [{"item_id": i, "region_id": st["target_region_id"], "confirmed_at": st["completed_at"],
                 "source": "step_completion", "step_id": step_id} for i in st["item_ids"]]
        for x in locs:
            s["locations"][x["item_id"]] = x
        return {"step": st, "locations": locs}
    return run(sid, b["request_id"], sig_of(b), b["expected_revision"], fn)


@app.post(P + "/sessions/{sid}/locations/confirmations")
async def loc_confirm(sid: str, req: Request):
    b = await req.json()

    def fn(s):
        x = {"item_id": b["item_id"], "region_id": b["region_id"], "confirmed_at": now(), "source": "user_confirmation", "step_id": None}
        s["locations"][b["item_id"]] = x
        return {"location": x}
    return run(sid, b["request_id"], sig_of(b), b["expected_revision"], fn)


@app.get(P + "/sessions/{sid}")
async def get_session(sid: str):
    s = DB.get(sid)
    if not s:
        return err(404, "SESSION_NOT_FOUND", "session 不存在")
    return {"session_id": sid, "revision": s["revision"], "scene": s["scene"],
            "images": [{k: v for k, v in i.items() if not k.startswith("_")} for i in s["images"]], "requirements": {"entries": [], "questions": []}, "current_plan": s["plan"],
            "locations": list(s["locations"].values()), "pending_questions": [], "messages": s["messages"]}


@app.get(P + "/sessions/{sid}/items/{item_id}/location")
async def item_location(sid: str, item_id: str):
    s = DB.get(sid)
    if not s:
        return err(404, "SESSION_NOT_FOUND", "session 不存在")
    if item_id not in {x["item_id"] for x in (s["scene"] or {}).get("items", [])}:
        return err(404, "ITEM_NOT_FOUND", "物品不存在")
    loc = s["locations"].get(item_id)
    sug = None
    if not loc and s["plan"]:
        sug = next((a["region_id"] for a in s["plan"]["assignments"] if a["item_id"] == item_id), None)
    return {"session_id": sid, "revision": s["revision"], "item_id": item_id, "location": loc, "suggested_region_id": sug}


@app.patch(P + "/sessions/{sid}/items/{item_id}")
async def rename_item(sid: str, item_id: str, req: Request):
    b = await req.json()

    def fn(s):
        it = next((x for x in (s["scene"] or {}).get("items", []) if x["item_id"] == item_id), None)
        if not it:
            return err(404, "ITEM_NOT_FOUND", "物品不存在", b["request_id"])
        if not b["name"].strip() or len(b["name"]) > 30:
            return err(422, "INVALID_REQUEST", "名稱需為 1–30 字", b["request_id"])
        it["name"] = b["name"].strip()
        return {"item": it}
    return run(sid, b["request_id"], sig_of(b), b["expected_revision"], fn)


@app.get(P + "/sessions/{sid}/images/{image_id}")
async def get_image(sid: str, image_id: str):
    s = DB.get(sid)
    img = next((i for i in (s or {}).get("images", []) if i["image_id"] == image_id), None)
    if not img:
        return err(404, "ITEM_NOT_FOUND", "圖片不存在")
    return Response(img["_bytes"], media_type=img["mime_type"])

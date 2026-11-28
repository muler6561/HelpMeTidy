"""Help Me Tidy 前端（Gradio 6）：對話式介面。
執行：TIDY_API_BASE=http://127.0.0.1:8000/api/v1 python app.py
"""
import html
import json
import math
import os
import random
import re
import shutil
import time

import gradio as gr
from PIL import Image

from api_client import BASE, ApiError, call, get_binary

AVAIL = {"available": "可用", "unavailable": "不可用", "unknown": "未知"}
AVAIL_CH = list(AVAIL.values())
AVAIL_REV = {v: k for k, v in AVAIL.items()}
FIT = {"confirmed": "✅ 已確認放得下", "unknown": "⚠️ 容量未知"}
DIMS = [("width_cm", "寬"), ("depth_cm", "深"), ("height_cm", "高")]
NOT_ROOM_CODES = {"NOT_ROOM_IMAGE", "INVALID_IMAGE_CONTENT"}
CACHE_DIR = os.path.abspath(os.getenv("TIDY_CACHE_DIR", ".tidy_cache"))  # 本機照片備份，供還原對話時顯示
EXT = {"image/jpeg": ".jpg", "image/png": ".png"}

NOT_ROOM_MSG = ("🤔 這張照片看起來不是房間或收納空間，我沒辦法從中辨識出可以整理的區域。\n\n"
                "請改傳**能看到家具與收納處**的房間照片（例如書桌、書架、抽屜、衣櫃），"
                "盡量拍到整面、光線充足。")

HEAD = """
<meta name="google" content="notranslate">
<script>
document.documentElement.lang = "zh-TW";
document.documentElement.setAttribute("translate", "no");
(function () {
  function tab(name) {
    return function () {
      var t = Array.prototype.slice.call(document.querySelectorAll('button[role="tab"]'));
      return t.filter(function (b) { return b.textContent.trim() === name && b.offsetWidth > 0; })[0];
    };
  }
  function btnText(t) {
    return Array.prototype.slice.call(document.querySelectorAll('button')).filter(function (b) { return b.textContent.trim() === t; })[0];
  }
  var STEPS = [
    {find: function () { return document.querySelector('#mm-box'); }, title: '① 輸入文字',
     text: '用一兩句話說明想怎麼整理，例如：「課本要放順手的地方，抽屜不要塞太滿」。'},
    {find: function () { return document.querySelector('#mm-box .upload-button'); }, title: '② 加入照片',
     text: '按迴紋針上傳 1–5 張房間照片（要拍到書桌、書架、抽屜等收納空間），再按 Enter 送出。'},
    {find: tab('房間資料'), title: '③ 補充房間資料（選填）',
     text: '填房間尺寸、不可移動的家具等，AI 會一起參考。'},
    {find: tab('辨識結果'), title: '④ 檢查辨識結果',
     text: 'AI 看到的物品與收納區域。看錯的直接改，也可以標記哪些物品「常用」，沒問題就按「確認辨識並產生計畫」。'},
    {find: tab('待辦清單'), title: '⑤ 照清單整理',
     text: '整理完一項就勾選，可以跳著勾；沒勾的項目會列出來提醒你。'},
    {find: tab('物品位置'), title: '⑥ 查詢與管理物品',
     text: '輸入物品名稱查位置，也能修改名稱、更新物品放在哪裡。'},
    {find: function () { return btnText('＋ 新對話'); }, title: '⑦ 對話紀錄',
     text: '左側會保存你的對話，點選就能回到先前的整理；「＋ 新對話」可重新開始。'},
    {find: function () { return document.querySelector('#help-btn'); }, title: '想再看一次？',
     text: '隨時按右下角的「?」重看這份教學。'}
  ];
  var idx = 0, spot = null, card = null;
  function get(i) { var e = STEPS[i].find(); return e && e.offsetWidth > 0 ? e : null; }
  function clear() {
    if (spot) spot.remove();
    if (card) card.remove();
    spot = card = null;
    window.removeEventListener('resize', place);
    window.removeEventListener('keydown', onKey);
  }
  function finish() {
    clear();
    try { localStorage.setItem('tidyTourDone', '1'); } catch (e) {}
  }
  function onKey(e) { if (e.key === 'Escape') finish(); }
  function place() {
    if (!card) return;
    var el = get(idx);
    if (!el) { next(1); return; }
    var r = el.getBoundingClientRect(), pad = 6;
    spot.style.cssText = 'left:' + (r.left - pad) + 'px;top:' + (r.top - pad) + 'px;width:' +
      (r.width + pad * 2) + 'px;height:' + (r.height + pad * 2) + 'px;';
    var cw = card.offsetWidth, ch = card.offsetHeight;
    var below = r.bottom + ch + 24 < window.innerHeight;
    var top = below ? r.bottom + 16 : Math.max(12, r.top - ch - 16);
    var left = Math.min(Math.max(12, r.left + r.width / 2 - cw / 2), window.innerWidth - cw - 12);
    card.style.top = top + 'px';
    card.style.left = left + 'px';
  }
  function next(d) {
    idx += d;
    while (idx >= 0 && idx < STEPS.length && !get(idx)) idx += (d || 1);
    if (idx >= STEPS.length || idx < 0) { finish(); return; }
    render();
  }
  function render() {
    var s = STEPS[idx];
    card.innerHTML = '<div class="tt-step">' + (idx + 1) + ' / ' + STEPS.length + '</div>' +
      '<div class="tt-title">' + s.title + '</div><div class="tt-text">' + s.text + '</div>' +
      '<div class="tt-actions"><button class="tt-skip">略過</button><span style="flex:1"></span>' +
      (idx > 0 ? '<button class="tt-prev">上一步</button>' : '') +
      '<button class="tt-next">' + (idx === STEPS.length - 1 ? '完成' : '下一步') + '</button></div>';
    card.querySelector('.tt-skip').onclick = finish;
    card.querySelector('.tt-next').onclick = function () { idx === STEPS.length - 1 ? finish() : next(1); };
    var p = card.querySelector('.tt-prev');
    if (p) p.onclick = function () { next(-1); };
    place();
  }
  window.tidyTour = function () {
    clear();
    idx = 0;
    spot = document.createElement('div'); spot.className = 'tt-spot';
    card = document.createElement('div'); card.className = 'tt-card';
    document.body.appendChild(spot); document.body.appendChild(card);
    window.addEventListener('resize', place);
    window.addEventListener('keydown', onKey);
    if (!get(0)) { next(1); } else { render(); }
  };
})();
</script>
"""

CSS = """
:root {--bg:#faf9f5; --ink:#2b2a27; --muted:#7a766d; --accent:#c96442; --line:#e8e5dc;}
body, .gradio-container {background: var(--bg) !important; color: var(--ink);}
html, body, gradio-app {background: var(--bg) !important;}
.gradio-container {max-width: none !important; width: 100% !important; padding: 0 24px !important;}
.gradio-container .main, .gradio-container .contain {max-width: none !important;}
#chat-col .form {background: transparent !important; border: none !important; box-shadow: none !important; padding: 0 !important;}
.hist-row {gap: 4px !important; flex-wrap: nowrap !important;}
.hist-row button {text-align: left; justify-content: flex-start; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;}
.hist-cur {background: #ece9df !important; font-weight: 600;}
footer {display: none !important;}
#hdr h1 {font-weight: 500; font-size: 1.45rem; margin: 6px 0 0;}
#chat {border: none !important; background: transparent !important; box-shadow: none !important;}
#chat .placeholder-content, #chat .placeholder {text-align: center;}
#mm-box {border-radius: 26px !important; border: 1px solid var(--line) !important; background: #fff !important;
         box-shadow: 0 2px 18px rgba(60,50,30,.07) !important;}
#panel {background: #fff; border: 1px solid var(--line); border-radius: 22px; padding: 10px 18px 18px;}
#panel button[role="tab"] {font-weight: 500;}
#todo .wrap {display: flex; flex-direction: column; gap: 8px;}
#todo label {width: 100%; background: var(--bg); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px;}
#help-btn {position: fixed; right: 26px; bottom: 26px; z-index: 50; width: 44px !important; min-width: 44px !important;
           height: 44px; border-radius: 50% !important; padding: 0 !important; font-size: 1.1rem;
           background: #fff !important; color: var(--accent) !important; border: 1px solid var(--line) !important;
           box-shadow: 0 2px 12px rgba(0,0,0,.12);}
.tt-spot {position: fixed; z-index: 9998; border: 3px solid #c96442; border-radius: 16px; pointer-events: none;
          box-shadow: 0 0 0 9999px rgba(30,28,24,.55); transition: all .2s;}
.tt-card {position: fixed; z-index: 9999; width: 320px; background: #fff; border-radius: 16px; padding: 16px 18px;
          box-shadow: 0 8px 32px rgba(0,0,0,.28); font-size: 14px; line-height: 1.55; color: #2b2a27;}
.tt-step {font-size: 12px; color: #9a958a; margin-bottom: 2px;}
.tt-title {font-size: 16px; font-weight: 600; margin-bottom: 6px;}
.tt-actions {display: flex; gap: 8px; margin-top: 14px; align-items: center;}
.tt-card button {border: 1px solid #e8e5dc; background: #fff; border-radius: 10px; padding: 6px 14px; cursor: pointer;}
.tt-card .tt-next {background: #c96442; color: #fff; border-color: #c96442;}
.tt-card .tt-skip {border: none; color: #9a958a;}
"""


# 動態欄位被重繪前，先讓目前聚焦的欄位失焦，避免瀏覽器對已移除的欄位送出事件
BLUR_JS = "() => { if (document.activeElement) document.activeElement.blur(); }"


class Notice(Exception):
    """需使用者先處理的提示（不呼叫 API）。drop_files=True 時，送回輸入框前移除有問題的照片。"""

    def __init__(self, msg, drop_files=False):
        super().__init__(msg)
        self.drop_files = drop_files


def esc(t):
    return html.escape(str(t if t is not None else "")).replace("|", "\\|").replace("\n", " ")


def blank(v):
    return v is None or str(v).strip() == ""


def new_state():
    return {"sid": None, "revision": None, "scene": None, "scene_confirmed": False, "plan": None,
            "plan_started": False, "image_ids": [], "uploaded_key": None, "requirement": "",
            "pending": {}, "chat": [], "locations": {}, "ver": random.randint(0, 10**9),
            "history": [], "usage": {}, "title": ""}


def say(S, role, content):
    S["chat"].append({"role": role, "content": content})


# ---------- 查找與格式 ----------
def find(S, kind, _id):
    key = "items" if kind == "item" else "regions"
    return next((x for x in (S["scene"] or {}).get(key, []) if x[kind + "_id"] == _id), None)


def names(S):
    d = {}
    for kind in ("item", "region"):
        for x in (S["scene"] or {}).get(kind + "s", []):
            d[x[kind + "_id"]] = x.get("name") or x[kind + "_id"]
    return lambda i: esc(d.get(i, i))


def fmt(v):
    return "" if v is None else f"{v:g}"


def parse_num(text, label, prev, maxv):
    """即時檢查數字。回傳 (新值, 提醒)；新值 None 代表留白。"""
    s = str(text if text is not None else "").strip().replace(",", "")
    if s == "":
        return None, None
    try:
        v = float(s)
    except ValueError:
        return prev, f"{label}「{s}」不是數字，已還原。請輸入阿拉伯數字，例如 40。"
    if math.isnan(v) or math.isinf(v):
        return prev, f"{label}不是有效數字，已還原。"
    msg = None
    if v < 0:
        v, msg = abs(v), f"{label}不能是負數，已改成 {abs(v):g}。"
    if v == 0:
        return prev, f"{label}必須大於 0，已還原。"
    if v > maxv:
        return prev, f"{label} {v:g} 超出合理範圍（上限 {maxv:g}），已還原。"
    return round(v, 1), msg


def check_name(S, kind, _id, text):
    """名稱即時檢查，回傳 (名稱, 提醒)。"""
    s = (text or "").strip()
    cur = find(S, kind, _id)["name"]
    if not s:
        return cur, "名稱不能空白，已還原。"
    if len(s) > 30:
        return cur, "名稱太長（上限 30 字），已還原。"
    key = "items" if kind == "item" else "regions"
    if any(x["name"] == s and x[kind + "_id"] != _id for x in S["scene"][key]):
        return cur, f"已經有叫「{s}」的項目，請換個名稱，已還原。"
    return s, None


# ---------- 右側面板文字 ----------
def plan_info_md(S):
    p = S["plan"]
    if not p:
        return "_尚無計畫。確認辨識結果後，AI 會產生整理步驟。_"
    out = [f"**計畫 v{p['plan_version']}**　{esc(p.get('change_reason'))}"]
    if p.get("pending_confirmations"):
        out += ["", "**待確認**"] + [f"- {esc(c)}" for c in p["pending_confirmations"]]
    return "\n".join(out)


def plan_table_md(S):
    p = S["plan"]
    if not p:
        return ""
    n = names(S)
    out = ["| 物品 | 放到 | 理由 | 容量 |", "|---|---|---|---|"]
    for a in p["assignments"]:
        out.append(f"| {n(a['item_id'])} | {n(a['region_id'])} | {esc(a.get('reason'))} | "
                   f"{FIT.get(a.get('fit_status'), esc(a.get('fit_status')))} |")
    return "\n".join(out)


def remind_md(S, selected):
    steps = (S["plan"] or {}).get("steps", [])
    if not steps:
        return ""
    sel = set(selected or [])
    left = [(i, s) for i, s in enumerate(steps, 1) if s["step_id"] not in sel and s["status"] != "completed"]
    unsent = [s for s in steps if s["step_id"] in sel and s["status"] != "completed"]
    out = []
    if unsent:
        out.append(f"📝 已勾 {len(unsent)} 項，**按下方按鈕才會記錄**。")
    if left:
        out.append(f"⏳ **還有 {len(left)} 項沒勾：**\n" + "\n".join(f"- {i}. {esc(s['instruction'])}" for i, s in left))
    elif not unsent:
        out.append("🎉 全部步驟都完成了！")
    return "\n\n".join(out)


def meta_md(S):
    return ""


def render(S):
    steps = (S["plan"] or {}).get("steps", [])
    choices = [(f"{i}. {s['instruction']}", s["step_id"]) for i, s in enumerate(steps, 1)]
    done = [s["step_id"] for s in steps if s["status"] == "completed"]
    return (plan_info_md(S), gr.update(choices=choices, value=done), remind_md(S, done),
            plan_table_md(S), meta_md(S), str(S["ver"]))


def scene_md(S):
    sc = S["scene"]
    out = ["我看完照片了：", "", "**收納區域**　" + "、".join(esc(r["name"]) for r in sc["regions"]), "",
           "**物品**　" + "、".join(esc(i["name"]) + ("❓" if i["needs_confirmation"] else "") for i in sc["items"])]
    if sc.get("questions"):
        out += ["", "**想請你確認**"] + [f"- {esc(q['text'])}" for q in sc["questions"]]
    out += ["", "👉 請到右側「辨識結果」檢查，沒問題就按「確認辨識並產生計畫」。"]
    return "\n".join(out)


# ---------- 錯誤處理 ----------
def explain(S, e):
    if e.code in NOT_ROOM_CODES:
        S["uploaded_key"] = None
        return NOT_ROOM_MSG
    if e.code == "REVISION_CONFLICT":
        try:
            reload_session(S)
        except ApiError:
            pass
        return "⚠️ 資料已有更新，已重新載入最新狀態。請確認後再重新操作（你的輸入已保留）。"
    if e.code == "NETWORK":
        return "⚠️ 連線逾時，**操作不一定失敗**。請再按一次，系統會沿用原請求取回結果，不會重複執行。"
    if e.code == "REQUEST_IN_PROGRESS":
        return "⏳ 上一次操作仍在處理中，請稍候再試一次。"
    if e.http in (429, 503):
        w = f"約 {e.retry_after} 秒" if e.retry_after else "一小段時間"
        return f"⚠️ 服務忙碌或暫時無法使用（{esc(e.code)}）。請等待{w}後再試（會沿用原請求）。"
    if e.http == 404:
        return f"⚠️ 找不到資源（{esc(e.code)}）：{esc(e.message)}"
    if e.http in (413, 415):
        return f"⚠️ 圖片無法使用（{esc(e.code)}）：{esc(e.message)}請重新選擇 1–5 張 10 MiB 以內的 JPEG／PNG。"
    if e.code == "IDEMPOTENCY_CONFLICT":
        return "⚠️ 請求識別碼衝突（前端邏輯錯誤），請重新整理頁面。"
    return f"⚠️ {esc(e.code)}：{esc(e.message)}"


def apply_agent(S, res):
    st, p = res["status"], res.get("plan")
    if p is not None:
        S["plan"] = p
        S["goto"] = "todo"
    if st != "failed":
        S["plan_started"] = True
    msg = esc(res.get("message"))
    if st == "needs_input":
        qs = "\n".join(f"- {esc(q['text'])}" for q in res.get("questions", []))
        return f"{msg}\n\n{qs}\n\n直接在下方回答即可。"
    if st == "plan_ready":
        return f"✅ 計畫已更新（v{p['plan_version']}）：{msg}\n\n步驟在右側「待辦清單」。"
    if st == "answered":
        loc = res.get("location")
        if loc:
            return f"{msg}\n\n📍 **最後確認位置**：{names(S)(loc['region_id'])}（{esc(loc['confirmed_at'])}）"
        return f"{msg}\n\n_（沒有已確認位置；文字中若出現位置，僅為建議，並非實際位置）_"
    e = res.get("error") or {}
    hint = "可再送出一次重試" if e.get("retryable") else "此錯誤不建議直接重試"
    return f"⚠️ 本輪未完成：{msg}\n\n原因：{esc(e.get('code'))}（{hint}）。已保存的進度不受影響。"


def _safe(x):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(x))  # ID 來自伺服器，不可直接當路徑


def cache_path(sid, image_id, mime):
    return os.path.join(CACHE_DIR, _safe(sid), _safe(image_id) + EXT.get(mime, ".jpg"))


def cache_uploads(sid, files, refs):
    """上傳成功後，依回傳順序把照片備份到本機（ImageRef 與檔案順序一致為假設）。"""
    for f, ref in zip(files, refs):
        p = cache_path(sid, ref["image_id"], ref.get("mime_type"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        shutil.copyfile(f, p)


def restore_photos(S, refs):
    """回傳 (可顯示的照片路徑, 無法還原的張數)。先找本機備份，找不到再向後端要（需後端提供圖片下載）。"""
    paths, missing = [], 0
    for ref in refs:
        p = cache_path(S["sid"], ref["image_id"], ref.get("mime_type"))
        if not os.path.exists(p):
            got = get_binary(f"/sessions/{S['sid']}/images/{ref['image_id']}")
            if got:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as fh:
                    fh.write(got[0])
        if os.path.exists(p):
            paths.append(p)
        else:
            missing += 1
    return paths, missing


def reload_session(S):
    r = call(S, "reload", "GET", f"/sessions/{S['sid']}")
    S["scene"] = r.get("scene")
    S["scene_confirmed"] = bool((r.get("scene") or {}).get("confirmed"))
    S["plan"] = r.get("current_plan")
    S["plan_started"] = bool(S["plan"])
    S["locations"] = {x["item_id"]: x for x in r.get("locations", [])}
    photos, missing = restore_photos(S, r.get("images", []))
    msgs, chat, placed = r.get("messages") or [], [], False
    for m in msgs:
        if m["role"] == "user" and not placed:  # 照片屬於第一則使用者訊息
            chat += [{"role": "user", "content": {"path": p}} for p in photos]
            placed = True
        chat.append({"role": m["role"], "content": esc(m["text"])})
    if not placed:
        chat += [{"role": "user", "content": {"path": p}} for p in photos]
    S["chat"] = chat
    S["requirement"] = next((m["text"] for m in msgs if m["role"] == "user"), S["requirement"])
    if missing:
        say(S, "assistant", f"（有 {missing} 張照片無法還原：這台電腦沒有備份，後端也沒有提供圖片下載。）")


# ---------- 對話 ----------
def check_files(files):
    if len(files) > 5:
        raise Notice("一次最多 5 張照片。", drop_files=True)
    for f in files:
        name = os.path.basename(f)
        if os.path.splitext(f)[1].lower() not in (".jpg", ".jpeg", ".png"):
            raise Notice(f"「{name}」不是 JPEG／PNG 圖片。", drop_files=True)
        if os.path.getsize(f) > 10 * 1024 * 1024:
            raise Notice(f"「{name}」超過 10 MiB，請壓縮後再傳。", drop_files=True)
        try:
            with Image.open(f) as im:
                w, h = im.size
        except Exception:
            raise Notice(f"「{name}」無法開啟，請換一張照片。", drop_files=True)
        if min(w, h) < 300:
            raise Notice(f"「{name}」解析度太低（至少 300 像素），AI 看不清楚，請換一張。", drop_files=True)


def room_text(l, w, h, fixed, fav, other):
    lines = []
    if not any(blank(v) for v in (l, w, h)):
        lines.append(f"- 房間大小：{l}×{w}×{h} 公尺（長×寬×高）")
    else:
        lines += [f"- 房間{n}：{v} 公尺" for n, v in (("長", l), ("寬", w), ("高", h)) if not blank(v)]
    for label, v in (("不可移動的物品／區域", fixed), ("最常用的物品", fav), ("其他限制", other)):
        if not blank(v):
            lines.append(f"- {label}：{str(v).strip()}")
    return ("\n【房間資料】\n" + "\n".join(lines)) if lines else ""


def h_chat(S, mm, room):
    mm = mm if isinstance(mm, dict) else {"text": str(mm or ""), "files": []}
    text = (mm.get("text") or "").strip()
    files = [f if isinstance(f, str) else f.get("path") for f in (mm.get("files") or [])]
    if not text and not files:
        raise Notice("請輸入內容或上傳照片。")
    if S["scene"] is None:
        if not files:
            raise Notice("請先按輸入框左邊的迴紋針上傳 1–5 張房間照片，並描述你的需求。")
        check_files(files)
        if not text:
            raise Notice("請再描述一下需求（例如哪些東西太擋路、哪些要放指定位置）。")
    elif not S["scene_confirmed"]:
        raise Notice("請先到右側「辨識結果」檢查，並按「確認辨識並產生計畫」。")
    elif files:
        raise Notice("目前不支援在分析後增補照片；要重來請按右上角「新對話」。")

    for f in files:
        say(S, "user", {"path": f})
    if text:
        say(S, "user", esc(text))

    if S["scene"] is None:
        S["requirement"] = text + room_text(*room)
        if not S["sid"]:
            S["sid"] = call(S, "create", "POST", "/sessions", {})["session_id"]
            S["title"] = " ".join(text.split())[:22]
            S["history"].append({"sid": S["sid"], "title": S["title"], "ts": time.time()})
        key = [(os.path.basename(f), os.path.getsize(f)) for f in files]
        if S["uploaded_key"] != key:
            r = call(S, "upload", "POST", f"/sessions/{S['sid']}/images", {"expected_revision": S["revision"]}, files=files)
            S["image_ids"], S["uploaded_key"] = [i["image_id"] for i in r["images"]], key
            cache_uploads(S["sid"], files, r["images"])
        r = call(S, "analysis", "POST", f"/sessions/{S['sid']}/analysis",
                 {"expected_revision": S["revision"], "image_ids": S["image_ids"], "user_message": S["requirement"]})
        if not r["scene"].get("regions"):  # 沒辨識到任何收納區域＝不是房間照片
            S["uploaded_key"], S["keep_text"] = None, text
            return NOT_ROOM_MSG
        S["scene"], S["scene_confirmed"], S["goto"] = r["scene"], False, "scene"
        S["usage"] = default_usage(S["scene"], room[4])
        return scene_md(S)
    if not S["plan_started"]:
        msg = S["requirement"] + (f"\n補充：{text}" if text else "")
        return apply_agent(S, call(S, "plan", "POST", f"/sessions/{S['sid']}/plans",
                                   {"expected_revision": S["revision"], "user_message": msg}))
    return apply_agent(S, call(S, "message", "POST", f"/sessions/{S['sid']}/messages",
                               {"expected_revision": S["revision"], "user_message": text}))


def default_usage(scene, fav_text):
    """依「房間資料」裡填的常用物品，預先勾選名稱相符的物品。"""
    toks = [t for t in re.split(r"[、,，;；\s]+", str(fav_text or "")) if t]
    return {i["item_id"]: any(t in i["name"] or i["name"] in t for t in toks) for i in scene["items"]}


def usage_text(S):
    items, u = S["scene"]["items"], S.get("usage", {})
    fav = [i["name"] for i in items if u.get(i["item_id"])]
    if not fav:
        return ""
    rest = [i["name"] for i in items if not u.get(i["item_id"])]
    out = "\n【物品使用頻率】\n- 常用（每天使用，請放在容易拿取處）：" + "、".join(fav)
    return out + ("\n- 不常用：" + "、".join(rest) if rest else "")


def sig(S):
    return json.dumps([S["scene"], S["scene_confirmed"], S["locations"], bool(S["plan"])], sort_keys=True, default=str)


def finish(S, before, box):
    if sig(S) != before:
        S["ver"] += 1
    for h in S["history"]:
        if h["sid"] == S["sid"]:
            h["ts"] = time.time()
    goto = S.pop("goto", None)
    return (S, S["chat"], box, gr.Tabs(selected=goto) if goto else gr.update(), *render(S), S["history"])


def chat_send(S, mm, *room):
    mark, before = len(S["chat"]), sig(S)
    try:
        reply, box = h_chat(S, mm, room), {"text": "", "files": []}
    except Notice as n:
        del S["chat"][mark:]
        reply, box = str(n), ({**mm, "files": []} if n.drop_files and isinstance(mm, dict) else mm)
    except ApiError as e:
        del S["chat"][mark:]
        reply, box = explain(S, e), mm  # 輸入保留，方便沿用原請求重送
    keep = S.pop("keep_text", None)
    if keep:  # 不是房間照片：清掉照片、保留需求文字
        box = {"text": keep, "files": []}
    say(S, "assistant", reply)
    return finish(S, before, box)


def panel(fn):
    """面板動作：結果以對話泡泡呈現。"""
    def wrapper(S, *args):
        before = sig(S)
        try:
            msg = fn(S, *args)
            if msg:
                say(S, "assistant", msg)
        except Notice as n:
            say(S, "assistant", str(n))
        except ApiError as e:
            say(S, "assistant", explain(S, e))
        return finish(S, before, gr.update())
    return wrapper


def need(S, confirmed=False):
    if not S["sid"]:
        return "請先在對話窗上傳照片並描述需求。"
    if confirmed and not S["scene_confirmed"]:
        return "請先確認辨識結果。"


def h_confirm(S):
    if (m := need(S)) or not S["scene"]:
        raise Notice(m or "請先分析照片。")
    if S["plan_started"]:
        raise Notice("已建立計畫，辨識不能再修改；請用對話回報調整。")
    left = sum(1 for x in S["scene"]["items"] + S["scene"]["regions"] if x["needs_confirmation"])
    r = call(S, "confirm", "PUT", f"/sessions/{S['sid']}/scene/confirmation",
             {"expected_revision": S["revision"], "scene": S["scene"]})
    S["scene"], S["scene_confirmed"] = r["scene"], True
    say(S, "assistant", "✅ 辨識已確認" + (f"（還有 {left} 項沒勾「已確認」，仍已送出）" if left else "") + "，正在產生計畫…")
    return apply_agent(S, call(S, "plan", "POST", f"/sessions/{S['sid']}/plans",
                               {"expected_revision": S["revision"], "user_message": S["requirement"] + usage_text(S)}))


def h_complete(S, sel):
    if m := need(S, True):
        raise Notice(m)
    p = S["plan"]
    if not p:
        raise Notice("還沒有計畫。")
    todo = [s for s in p["steps"] if s["step_id"] in set(sel or []) and s["status"] != "completed"]
    if not todo:
        raise Notice("沒有新勾選的步驟。整理好物品後先勾選，再按確認。")
    n, lines = names(S), []
    try:
        for s in todo:
            r = call(S, "complete", "POST", f"/sessions/{S['sid']}/steps/{s['step_id']}/completion",
                     {"expected_revision": S["revision"], "plan_version": p["plan_version"]})
            s.update(r["step"])
            for x in r["locations"]:
                S["locations"][x["item_id"]] = x
                lines.append(f"- {n(x['item_id'])} → {n(x['region_id'])}")
    except ApiError:
        if lines:
            say(S, "assistant", "✅ 已先記錄：\n" + "\n".join(lines))
        raise
    done = [s["step_id"] for s in p["steps"] if s["status"] == "completed"]
    return "✅ 已記錄物品位置：\n" + "\n".join(lines) + "\n\n" + remind_md(S, done)


def h_todo_preview(S, sel):
    return remind_md(S, sel)


def h_search(S, q):
    q = (q or "").strip().lower()
    if not q:
        return ""
    if not S["sid"] or not S["scene"]:
        return "還沒有物品資料。請先在對話窗上傳照片。"
    hits = [i for i in S["scene"]["items"] if i["name"].lower() in q or q in i["name"].lower()][:8]
    if not hits:
        return f"找不到名稱含「{esc(q)}」的物品。可以換個關鍵字，或直接在對話窗問 AI。"
    n, out = names(S), []
    try:
        for it in hits:
            r = call(S, "query", "GET", f"/sessions/{S['sid']}/items/{it['item_id']}/location")
            loc = r.get("location")
            if loc:
                out.append(f"📍 **{esc(it['name'])}**：{n(loc['region_id'])}（{esc(loc['confirmed_at'])}）")
            elif r.get("suggested_region_id"):
                out.append(f"❔ **{esc(it['name'])}**：沒有確認紀錄　💡 建議位置 {n(r['suggested_region_id'])}（尚未確認，不代表實際位置）")
            else:
                out.append(f"❔ **{esc(it['name'])}**：沒有任何位置紀錄")
    except ApiError as e:
        return explain(S, e)
    return "\n\n".join(out)


def h_save_item(S, iid, new_name, new_region):
    if m := need(S, True):
        raise Notice(m)
    it, cur = find(S, "item", iid), S["locations"].get(iid)
    name, warn = check_name(S, "item", iid, new_name)
    if warn:
        raise Notice(f"⚠️ {warn}")
    msgs = []
    if new_region and (not cur or cur["region_id"] != new_region):
        r = call(S, "locconfirm", "POST", f"/sessions/{S['sid']}/locations/confirmations",
                 {"expected_revision": S["revision"], "item_id": iid, "region_id": new_region})
        S["locations"][iid] = r["location"]
        msgs.append(f"位置改為 {names(S)(new_region)}")
    if name != it["name"]:
        try:
            call(S, "rename", "PATCH", f"/sessions/{S['sid']}/items/{iid}",
                 {"expected_revision": S["revision"], "name": name})
        except ApiError as e:
            if e.http in (404, 405) and e.code not in ("SESSION_NOT_FOUND", "ITEM_NOT_FOUND"):
                raise Notice("⚠️ 後端目前還不支援修改物品名稱（需新增 `PATCH /sessions/{sid}/items/{item_id}`，見 README）。"
                             + ("　位置已更新。" if msgs else ""))
            raise
        it["name"] = name
        msgs.append(f"名稱改為「{esc(name)}」")
    if not msgs:
        return "沒有變更。"
    return f"✅ 「{esc(it['name'])}」已更新：" + "、".join(msgs)


def reset_in_place(S):
    """清空目前對話（保留對話紀錄清單）。"""
    new = new_state()
    new["history"], new["ver"] = S["history"], S["ver"] + 1
    S.clear()
    S.update(new)


def new_chat(S):
    reset_in_place(S)
    S["goto"] = "room"
    return finish(S, "", {"text": "", "files": []})


def forget(S, sid):
    S["history"] = [h for h in S["history"] if h["sid"] != sid]
    shutil.rmtree(os.path.join(CACHE_DIR, _safe(sid)), ignore_errors=True)


def h_open(S, sid):
    """從左側對話紀錄切回先前的對話。"""
    if sid == S["sid"]:
        return None
    reset_in_place(S)
    S["sid"] = sid
    try:
        reload_session(S)
    except ApiError as e:
        if e.http == 404:
            forget(S, sid)
            S["sid"] = None
            raise Notice("找不到這段對話（伺服器上可能已被清除），已從紀錄移除。")
        S["sid"] = None
        raise
    S["goto"] = "todo" if S["plan"] else ("scene" if S["scene"] else "room")
    S["usage"] = {}
    return None


def h_forget(S, sid):
    """從紀錄移除並刪除本機的照片備份；不會刪除伺服器上的資料。"""
    forget(S, sid)
    if sid == S["sid"]:
        reset_in_place(S)
        S["goto"] = "room"
    S["ver"] += 1
    return None


def on_load(hist, S):
    S["history"] = list(hist or [])
    S["ver"] += 1
    return S, str(S["ver"])


# ---------- 辨識結果：逐列元件（修改即時檢查、即時生效）----------
def region_row(S, r, locked):
    """回傳名稱欄位；名稱的即時檢查由 scene_panel 統一註冊（需要同步更新物品的下拉選單）。"""
    rid = r["region_id"]
    with gr.Row():
        nm = gr.Textbox(value=r["name"], label="區域名稱", scale=3, max_lines=1, interactive=not locked)
        av = gr.Dropdown(AVAIL_CH, value=AVAIL.get(r["availability"], "未知"), label="可用狀態", scale=2,
                         interactive=not locked)
        ok = gr.Checkbox(value=not r["needs_confirmation"], label="已確認", interactive=not locked, scale=1,
                         min_width=100)
    with gr.Row():
        boxes = [gr.Textbox(value=fmt((r.get("dimensions") or {}).get(key)), label=f"{lab} (cm)", placeholder="未知",
                            max_lines=1, interactive=not locked, scale=1) for key, lab in DIMS]

    def on_avail(S, v, rid=rid):
        find(S, "region", rid)["availability"] = AVAIL_REV[v]

    def on_dim(S, text, rid, key, lab):
        reg = find(S, "region", rid)
        dims = reg.setdefault("dimensions", {})
        v, msg = parse_num(text, f"{reg['name']}的{lab}", dims.get(key), 5000)
        if msg:
            gr.Warning(msg)
        dims[key] = v
        return fmt(v)

    def on_ok(S, v, rid=rid):
        find(S, "region", rid)["needs_confirmation"] = not v

    av.input(on_avail, [state, av], None)
    for box, (key, lab) in zip(boxes, DIMS):
        fn = lambda S, t, rid=rid, key=key, lab=lab: on_dim(S, t, rid, key, lab)
        box.blur(fn, [state, box], box)
        box.submit(fn, [state, box], box)
    ok.input(on_ok, [state, ok], None)
    return nm


def item_row(S, it, locked):
    iid = it["item_id"]
    regs = [(r["name"], r["region_id"]) for r in S["scene"]["regions"]]
    with gr.Row():
        nm = gr.Textbox(value=it["name"], label="物品名稱", scale=3, max_lines=1, interactive=not locked)
        ob = gr.Dropdown(regs, value=it.get("observed_region_id"), label="照片中的位置", scale=3,
                         interactive=not locked)
    with gr.Row():
        fav = gr.Checkbox(value=bool(S["usage"].get(iid)), label="常用（經常拿取）", interactive=not locked, scale=1,
                          min_width=140)
        ok = gr.Checkbox(value=not it["needs_confirmation"], label="已確認", interactive=not locked, scale=1,
                         min_width=100)
    if it.get("observation"):
        gr.Markdown(f"<small style='color:#9a958a'>AI 觀察：{esc(it['observation'])}</small>")

    def on_name(S, text, iid=iid):
        v, msg = check_name(S, "item", iid, text)
        if msg:
            gr.Warning(msg)
        else:
            find(S, "item", iid)["name"] = v
        return v

    def on_obs(S, v, iid=iid):
        find(S, "item", iid)["observed_region_id"] = v

    def on_ok(S, v, iid=iid):
        find(S, "item", iid)["needs_confirmation"] = not v

    def on_fav(S, v, iid=iid):
        S["usage"][iid] = bool(v)

    fav.input(on_fav, [state, fav], None)
    nm.blur(on_name, [state, nm], nm)
    nm.submit(on_name, [state, nm], nm)
    ob.input(on_obs, [state, ob], None)
    ok.input(on_ok, [state, ok], None)
    return ob


# ---------- 介面 ----------
GREETING = ("### 你好，今天想整理哪裡？\n\n"
            "按下方輸入框左邊的迴紋針上傳房間照片，再說說你的需求。\n\n"
            "<small>照片請拍到書桌、書架、抽屜等收納空間</small>")

with gr.Blocks(title="幫我整理 Help Me Tidy") as demo:
    state = gr.State(new_state())
    history = gr.BrowserState([], storage_key="tidy_history", secret="help-me-tidy-demo")  # 對話紀錄存在這個瀏覽器
    ver = gr.Textbox(visible=False)
    meta = gr.Markdown(visible=False)

    with gr.Sidebar(position="left", open=True, width=280):
        gr.Markdown("### 🧹 幫我整理")
        b_new = gr.Button("＋ 新對話", variant="primary")
        gr.Markdown("**對話紀錄**")

        @gr.render(inputs=[state], triggers=[ver.change])
        def history_panel(S):
            hs = sorted(S["history"], key=lambda h: -h["ts"])
            if not hs:
                gr.Markdown("<small style='color:#9a958a'>還沒有對話。上傳照片開始第一段整理吧。</small>")
                return
            for h in hs:
                with gr.Row(elem_classes="hist-row"):
                    b = gr.Button(h["title"] or "（未命名）", size="sm", scale=6, min_width=60,
                                  elem_classes=["hist-cur"] if h["sid"] == S["sid"] else [])
                    d = gr.Button("✕", size="sm", scale=1, min_width=36)
                b.click(panel(lambda S, sid=h["sid"]: h_open(S, sid)), [state], common, trigger_mode="once")
                d.click(panel(lambda S, sid=h["sid"]: h_forget(S, sid)), [state], common, trigger_mode="once")

    with gr.Row(equal_height=False):
        with gr.Column(scale=6, elem_id="chat-col"):
            chat = gr.Chatbot(height="68vh", show_label=False, elem_id="chat", placeholder=GREETING)
            mm = gr.MultimodalTextbox(show_label=False, file_types=["image"], file_count="multiple", lines=1, max_lines=6,
                                      placeholder="描述你的需求，例如：課本要放順手的地方", elem_id="mm-box")
        with gr.Column(scale=5, elem_id="panel"):
            with gr.Tabs() as tabs:
                with gr.Tab("房間資料", id="room"):
                    gr.Markdown("選填。填得越準，建議越貼近你的房間；會和第一則訊息一起送出。")
                    gr.Markdown("**房間大小**（公尺）")
                    with gr.Row():
                        r_l = gr.Textbox(label="長", placeholder="例：4", max_lines=1)
                        r_w = gr.Textbox(label="寬", placeholder="例：3", max_lines=1)
                        r_h = gr.Textbox(label="高", placeholder="例：2.5", max_lines=1)
                    r_fixed = gr.Textbox(label="不可移動的物品／區域", placeholder="例：書桌、衣櫃", max_lines=2)
                    r_fav = gr.Textbox(label="最常用的物品", placeholder="例：課本、筆電", max_lines=2)
                    r_other = gr.Textbox(label="其他限制", placeholder="例：書要放書架", max_lines=2)
                with gr.Tab("辨識結果", id="scene"):
                    @gr.render(inputs=[state], triggers=[demo.load, ver.change])
                    def scene_panel(S):
                        sc = S["scene"]
                        if not sc:
                            gr.Markdown("尚無資料。\n\n在左側上傳房間照片並送出後，AI 辨識的結果會顯示在這裡，你可以修改再確認。")
                            return
                        locked = S["scene_confirmed"]
                        gr.Markdown("✅ 已確認。要改物品名稱請到「物品位置」。" if locked
                                    else "檢查 AI 看到的內容，錯的直接改，改完即時生效。")
                        if sc.get("questions") and not locked:
                            gr.Markdown("**AI 想請你確認**\n" + "\n".join(f"- {esc(q['text'])}" for q in sc["questions"]))
                        gr.Markdown("#### 收納區域")
                        rboxes = [region_row(S, r, locked) for r in sc["regions"]]
                        gr.Markdown("#### 物品")
                        idds = [item_row(S, it, locked) for it in sc["items"]]
                        for r, rbox in zip(sc["regions"], rboxes):
                            def on_rname(S, text, rid=r["region_id"], n=len(idds)):
                                v, msg = check_name(S, "region", rid, text)
                                if msg:
                                    gr.Warning(msg)
                                else:
                                    find(S, "region", rid)["name"] = v
                                regs = [(x["name"], x["region_id"]) for x in S["scene"]["regions"]]
                                return (v, *[gr.update(choices=regs) for _ in range(n)])  # 只更新下拉，不重繪表單
                            rbox.blur(on_rname, [state, rbox], [rbox, *idds])
                            rbox.submit(on_rname, [state, rbox], [rbox, *idds])
                        if not locked:
                            b = gr.Button("確認辨識並產生計畫", variant="primary")
                            b.click(None, None, None, js=BLUR_JS)
                            b.click(panel(h_confirm), [state], common, trigger_mode="once")
                with gr.Tab("待辦清單", id="todo"):
                    plan_info = gr.Markdown("_尚無計畫。確認辨識結果後，AI 會產生整理步驟。_")
                    todo = gr.CheckboxGroup(choices=[], label="整理步驟（整理好一項就勾一項，可以跳著勾）", elem_id="todo")
                    remind = gr.Markdown()
                    b_done = gr.Button("我已把勾選的物品移到指定位置，確認完成", variant="primary")
                    with gr.Accordion("配置與理由", open=False):
                        plan_table = gr.Markdown()
                with gr.Tab("物品位置", id="loc"):
                    q = gr.Textbox(label="查詢物品", placeholder="輸入物品名稱，例如：課本（Enter 查詢）", max_lines=1)
                    q_out = gr.Markdown()
                    gr.Markdown("#### 管理物品")

                    @gr.render(inputs=[state], triggers=[demo.load, ver.change])
                    def mgmt_panel(S):
                        sc = S["scene"]
                        if not sc or not S["scene_confirmed"]:
                            gr.Markdown("確認辨識結果後，可以在這裡修改物品名稱，並記錄物品現在放在哪裡。")
                            return
                        regs = [(r["name"], r["region_id"]) for r in sc["regions"]]
                        for it in sc["items"]:
                            loc = S["locations"].get(it["item_id"])
                            with gr.Row():
                                nm = gr.Textbox(value=it["name"], label="名稱", scale=3, max_lines=1, interactive=True)
                                lc = gr.Dropdown(choices=regs, value=loc["region_id"] if loc else None, scale=3, interactive=True,
                                                 label="目前位置" if loc else "目前位置（尚未確認）")
                                sv = gr.Button("儲存", scale=1, min_width=70)
                            sv.click(None, None, None, js=BLUR_JS)
                            sv.click(panel(lambda S, n, l, iid=it["item_id"]: h_save_item(S, iid, n, l)),
                                     [state, nm, lc], common, trigger_mode="once")

    b_help = gr.Button("?", elem_id="help-btn")

    common = [state, chat, mm, tabs, plan_info, todo, remind, plan_table, meta, ver, history]
    room_in = [r_l, r_w, r_h, r_fixed, r_fav, r_other]

    # 房間大小：離開欄位（或按 Enter）即檢查
    for box, label in ((r_l, "長"), (r_w, "寬"), (r_h, "高")):
        def fix_room(text, label=label):
            v, msg = parse_num(text, f"房間{label}", None, 50)
            if msg:
                gr.Warning(msg)
            return fmt(v)
        box.blur(fix_room, box, box)
        box.submit(fix_room, box, box)

    mm.submit(chat_send, [state, mm, *room_in], common, trigger_mode="once")
    b_done.click(panel(h_complete), [state, todo], common, trigger_mode="once")
    todo.input(h_todo_preview, [state, todo], remind)
    q.submit(h_search, [state, q], q_out)
    b_new.click(new_chat, [state], common)
    demo.load(on_load, [history, state], [state, ver])
    b_help.click(None, None, None, js="() => window.tidyTour()")
    demo.load(None, None, None, js="() => { try { if (!localStorage.getItem('tidyTourDone')) setTimeout(window.tidyTour, 900); } catch (e) { setTimeout(window.tidyTour, 900); } }")

if __name__ == "__main__":
    print("API base:", BASE)
    demo.queue(default_concurrency_limit=4).launch(
        theme=gr.themes.Base(primary_hue="orange", neutral_hue="stone", font=["system-ui", "sans-serif"]),
        css=CSS, head=HEAD, allowed_paths=[CACHE_DIR])

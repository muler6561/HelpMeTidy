# 幫我整理 Help Me Tidy — 前端（組員 A）

Gradio 6 對話式介面。依 `FRONTEND_API.md`、`BACKEND_API.md` 實作，欄位對齊 `backend/schemas.py`。

## 交付內容
`frontend/` 資料夾：`app.py`、`api_client.py`、`requirements.txt`、`README.md`（`mock_backend.py` 與 `requirements-dev.txt` 只供沒有後端時自測，可不部署）。

## 執行
```bash
pip install -r requirements.txt
TIDY_API_BASE=http://127.0.0.1:8000/api/v1 python app.py      # → http://127.0.0.1:7860
```
PowerShell：`$env:TIDY_API_BASE="http://127.0.0.1:8000/api/v1"`。自測：`pip install -r requirements-dev.txt` 後先跑 `python -m uvicorn mock_backend:app --port 8000`。
埠號被占用時用 `GRADIO_SERVER_PORT=7899 python app.py`。

## 前端使用的接口
與 `FRONTEND_API.md` 第 3 節相同的 10 個接口，另用到 `SessionState.messages`、`locations`、`current_plan` 還原對話。每個寫入帶 `request_id` 與 `expected_revision`；逾時／429／503 重送沿用原 `request_id`。

## 需要 B、C 確認（超出現行契約）
| # | 事項 | 前端現況 |
|---|---|---|
| 1 | **非房間照片的判斷**屬 Agent（`analyze_scene`）。建議判定不是房間時回傳 `regions: []`，或 HTTP 422 `NOT_ROOM_IMAGE` | 兩種都已處理：提示改傳房間照片，保留需求文字。前端只檢查格式、10 MiB、解析度≥300px |
| 2 | **改物品名稱**：確認接口只能用在計畫前。需新增 `PATCH /sessions/{sid}/items/{item_id}`，body `{request_id, expected_revision, name}`，回 `{request_id, session_id, revision, item}` | 已實作；後端回 404／405 時提示「尚不支援」，改位置不受影響 |
| 3 | **常用物品**：`Item` 沒有使用頻率欄位。前端把勾選結果附在 `POST /plans` 的 `user_message`（「【物品使用頻率】常用…」），請 Agent 解析成 `usage_frequency` | 重新載入後前端不會還原勾選（資料不在 SessionState） |
| 4 | **對話紀錄**：目前沒有「列出 session」接口。前端把 session 清單存在使用者瀏覽器（localStorage），換瀏覽器或清除資料就看不到；跨裝置需要後端提供 session 清單與身分驗證（`FRONTEND_API.md` 已列待確認） | 刪除紀錄只移除瀏覽器上的項目，不刪伺服器資料 |
| 5 | **還原照片**：契約沒有圖片下載接口（`SessionState.images` 只有 id／mime／大小）。建議 C 新增 `GET /sessions/{sid}/images/{image_id}`（回 JPEG／PNG 位元組；後端本來就保存了圖片） | 已處理兩層：①上傳時在前端主機備份一份到 `.tidy_cache/`（可用 `TIDY_CACHE_DIR` 改位置；請加進 `.gitignore`，刪除對話紀錄時一併刪除）；②備份不存在時改向上述接口要。兩者都沒有時，顯示「N 張照片無法還原」 |

## 假後端測試用輸入
檔名含 `notroom` 的圖片 → 模擬非房間；對話含 `fail` → failed；含「放不下」→ 計畫 v2（已完成步驟保留）；含「在哪」→ answered。

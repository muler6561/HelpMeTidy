# Help Me Tidy｜前端接口規格

## 1. 分工與展示流程

- A：照片選取、需求輸入、辨識確認、計畫與進度呈現、回饋與位置查詢。
- Agent 開發者：照片分析、需求解析、規劃、回饋修正、工具選擇與執行上限。
- C：HTTP 整合層、圖片儲存、ID 配發、資料讀寫、版本與重複請求防護。

首次展示：建立 session → 上傳照片 → 分析照片與需求 → 使用者確認辨識 → 建立計畫 → 確認一項步驟完成 → 回報「抽屜放不下，書架中層可用」→ 顯示新計畫並保留未受影響的完成進度 → 查詢最後確認位置。

## 2. 共通約定

- Base path：`/api/v1`。一般資料使用 JSON，圖片上傳使用 multipart。
- ID 是不透明字串，前端不得從名稱產生 ID；範例短 ID 僅供展示。
- 每次新操作產生唯一 `request_id`。網路重送同一操作時沿用相同 ID 與內容；修改內容須使用新 ID。
- 除建立 session 外，所有寫入及 Agent 操作帶 `expected_revision`，值取自最近一次收到的 `revision`。後端拒絕過期版本，前端先重新載入再讓使用者決定是否重做。
- `revision` 是整個 session 的資料版本；`plan_version` 是計畫版本，兩者不可互換。
- 時間使用帶時區的 ISO 8601。未知欄位使用 `null`，集合為空時使用 `[]`。
- v1 採同步回應，畫面自行顯示載入中並避免重複送出；不新增第五種 Agent 狀態。Agent 總執行上限為 120 秒，HTTP 部署時限需另經 A、C 確認。
- 逾時不代表操作未執行。以原請求 ID 重送取得原結果或 `REQUEST_IN_PROGRESS`，並可重新載入 session；不要換新 ID 自動重試。
- 回傳文字以純文字呈現，若支援 Markdown 必須過濾 HTML；前端不持有模型 API 金鑰。

## 3. 前端使用的接口

下表 body 中的欄位皆必填，除非註明選填。路徑中的 `{session_id}` 以下簡寫為 `{sid}`。

| 方法與路徑 | 功能 | 請求 body | 成功結果 |
| --- | --- | --- | --- |
| `POST /sessions` | 開始一次整理對話 | `{request_id}` | HTTP 201：`{request_id, session_id, revision: 0}` |
| `POST /sessions/{sid}/images` | 上傳供本次 session 分析的圖片 | multipart：`request_id`、`expected_revision`、`files` | HTTP 201：`{request_id, session_id, revision, images: ImageRef[]}` |
| `POST /sessions/{sid}/analysis` | 分析指定圖片及原始需求，取得待確認辨識結果 | `{request_id, expected_revision, image_ids: string[], user_message: string}` | HTTP 200：`{request_id, session_id, revision, scene: SceneAnalysis}` |
| `PUT /sessions/{sid}/scene/confirmation` | 確認或修正辨識；保留原有物品與區域 ID | `{request_id, expected_revision, scene: SceneAnalysis}` | HTTP 200：`{request_id, session_id, revision, scene: SceneAnalysis}` |
| `POST /sessions/{sid}/plans` | 從已確認現況及需求建立第一版計畫 | `{request_id, expected_revision, user_message: string}` | HTTP 200：`AgentResult` |
| `POST /sessions/{sid}/messages` | 補充回答、修改需求、回報放不下或詢問位置 | `{request_id, expected_revision, user_message: string}` | HTTP 200：`AgentResult` |
| `POST /sessions/{sid}/steps/{step_id}/completion` | 使用者明確確認步驟完成；同步確認該移動步驟的目的位置 | `{request_id, expected_revision, plan_version: integer}` | HTTP 200：`{request_id, session_id, revision, step: PlanStep, locations: LocationRecord[]}` |
| `POST /sessions/{sid}/locations/confirmations` | 不透過計畫，直接確認物品目前位置 | `{request_id, expected_revision, item_id: string, region_id: string}` | HTTP 200：`{request_id, session_id, revision, location: LocationRecord}` |
| `GET /sessions/{sid}` | 重新載入、斷線後恢復畫面 | 無 | HTTP 200：`SessionState` |
| `GET /sessions/{sid}/items/{item_id}/location` | 精確查詢一件物品最後確認的位置 | 無 | HTTP 200：`{session_id, revision, item_id, location: LocationRecord或null, suggested_region_id: string或null}` |

圖片預設限制提案：每次 1–5 張 JPEG／PNG，每張最多 10 MiB；由後端驗證實際檔案內容。`ImageRef` 包含 `image_id`、`mime_type`、`size_bytes`，不含本機路徑或 base64。v1 前端可用選取檔案的本機預覽；重新載入後的遠端圖片預覽不在本次契約內。

分析與確認接口只用於初始計畫前。確認時提交最新 scene 的完整副本，可修改名稱、觀察位置、可用狀態、尺寸及需確認標記；不可任意換 ID。新增辨識物品、增補照片與刪除物品暫不納入 v1；辨識不足以規劃時先詢問使用者。確認成功後由後端設定 `confirmed: true`。

`plans` 只用於第一版計畫；後續修正及 `needs_input` 的回答一律走 `messages`。完成按鈕只能針對目前計畫的步驟；本版只支援物品移動步驟，每步都有確定的目標區域。畫面需明確提示「已移到指定位置」，再送出完成請求。

## 4. AgentResult 與畫面對應

以下欄位固定存在：`request_id`、`session_id`、`revision`、`status`、`message`、`questions`、`plan`、`location`、`error`。`plan` 是完整最新計畫或 `null`，不是局部差異；前端依 ID 呈現，不能依陣列索引保留完成狀態。

| status | 意義 | 前端行為 |
| --- | --- | --- |
| `needs_input` | 資訊不足、指涉不明或需求衝突 | 顯示問題，保留現有計畫，等待使用者用 messages 回答 |
| `plan_ready` | 已驗證並保存的新計畫 | 顯示版本、配置理由、待確認事項及步驟完成狀態 |
| `answered` | 已回答問題或完成無須重規劃的互動 | 顯示回答；位置須區分已確認與僅建議 |
| `failed` | Agent 無法完成本輪 | 顯示原因及是否可重試，保留已保存的進度 |

`Question` 格式為 `{question_id, text, related_ids: string[]}`，本版使用自由文字回答。`needs_input` 至少有一題；`plan_ready` 必須有 plan；只有 `failed` 的 error 非 null。位置查無紀錄是正常的 `answered`，不是系統失敗。

### needs_input 範例

```json
{
  "request_id": "req_01", "session_id": "ses_01", "revision": 3,
  "status": "needs_input", "message": "請先補充抽屜的可用狀態。",
  "questions": [{"question_id": "q_01", "text": "第一層抽屜目前有空間嗎？", "related_ids": ["region_01"]}],
  "plan": null, "location": null, "error": null
}
```

### plan_ready 範例

```json
{
  "request_id": "req_02", "session_id": "ses_01", "revision": 4,
  "status": "plan_ready", "message": "先將課本移到書架中層。",
  "questions": [],
  "plan": {
    "plan_id": "plan_01", "plan_version": 1, "based_on_revision": 3,
    "change_reason": "初次規劃",
    "assignments": [{"item_id": "item_01", "region_id": "region_02", "reason": "方便取用", "fit_status": "unknown"}],
    "steps": [{"step_id": "step_01", "instruction": "將課本移到書架中層", "item_ids": ["item_01"], "target_region_id": "region_02", "status": "pending", "completed_at": null}],
    "pending_confirmations": ["尚未確認書架中層是否放得下課本。"]
  },
  "location": null, "error": null
}
```

### answered 範例

```json
{
  "request_id": "req_03", "session_id": "ses_01", "revision": 5,
  "status": "answered", "message": "課本最後確認放在書架中層。",
  "questions": [], "plan": null,
  "location": {"item_id": "item_01", "region_id": "region_02", "confirmed_at": "2026-11-20T10:00:00+08:00", "source": "step_completion", "step_id": "step_01"},
  "error": null
}
```

此範例省略附帶計畫，因此 `plan: null` 不代表刪除畫面上的既有計畫。若沒有已確認位置，`location` 為 null，message 必須說明沒有紀錄；建議位置不得冒充實際位置。

### failed 範例

```json
{
  "request_id": "req_04", "session_id": "ses_01", "revision": 5,
  "status": "failed", "message": "照片分析服務暫時無法回應，已保留進度。",
  "questions": [], "plan": null, "location": null,
  "error": {"code": "MODEL_TIMEOUT", "message": "已達本輪執行時限。", "retryable": true, "details": {}}
}
```

## 5. HTTP 錯誤與恢復

請求格式、版本或資源錯誤使用非 2xx，格式固定為：

```json
{
  "request_id": "req_05",
  "error": {"code": "REVISION_CONFLICT", "message": "資料已有更新，請重新載入。", "retryable": false, "details": {"current_revision": 6}}
}
```

| HTTP | code 範例 | 前端處理 |
| --- | --- | --- |
| 400 | `INVALID_REQUEST` | 修正缺少或錯誤的欄位 |
| 404 | `SESSION_NOT_FOUND`、`ITEM_NOT_FOUND` | 顯示資源不存在，不當成無位置紀錄 |
| 409 | `REVISION_CONFLICT`、`PLAN_VERSION_CONFLICT`、`SCENE_NOT_CONFIRMED`、`PLAN_ALREADY_EXISTS` | 載入最新狀態並顯示原因 |
| 409 | `IDEMPOTENCY_CONFLICT` | 同一 request_id 被用於不同內容，修正客戶端邏輯 |
| 409 | `REQUEST_IN_PROGRESS` | 本次操作尚未完成，等待後以同 ID 查回結果 |
| 413／415 | `IMAGE_TOO_LARGE`／`UNSUPPORTED_IMAGE_TYPE` | 請使用者重新選擇圖片 |
| 422 | `INVALID_SCENE`、`INVALID_REFERENCE` | 顯示具體資料問題，停止送出 |
| 429／503 | `RATE_LIMITED`／`SERVICE_UNAVAILABLE` | 依 `Retry-After`（秒）等待後重試同一請求 |

已進入 Agent 執行但無法完成的規劃／互動，用 HTTP 200 + `status: failed`。分析接口不回 AgentResult，模型服務失敗用 503 與上述錯誤 envelope。這兩種失敗前端都必須處理。

## 6. 前端驗收與待確認

- 四種 AgentResult 都能呈現；未知容量可見，不能顯示成「確定放得下」。
- 完成步驟後再修正計畫，未受影響的完成狀態不消失。
- 改名仍沿用 ID；重新載入可還原需求、計畫與進度。
- 沒有位置紀錄、僅建議位置、最後確認位置三種情況能清楚區分。
- 重複點擊、網路逾時、過期 revision 都有明確恢復方式。

待 A、C 確認：路由與分工、圖片限制、同步等待的部署可行性、是否需要登入與跨裝置資料隔離、照片保存期限。v1 預設單使用者展示；多使用者部署前必須另定身分驗證與 session 存取權限。

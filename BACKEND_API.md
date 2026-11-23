# Help Me Tidy｜後端與 Agent 接口規格

第五步已有可直接呼叫的 Python mock，見 [工具接口與範例](TOOLS_API.md)。`help_me_tidy.tools.Tools` 提供五個工具，`ToolBackend` 可替換儲存 adapter；本文件其他 HTTP／Agent 業務接口仍為整合草案，尚未與 C 完成確認。

## 1. 邊界與所有權

| 層級 | 負責事項 |
| --- | --- |
| HTTP 整合層（C） | 驗證請求、session 存取、圖片上傳、request_id 去重、revision 檢查、呼叫 Agent、轉換錯誤 |
| Agent 模組 | 照片分析、需求解析、規劃、工具迴圈、回饋修正、結果驗證與停止條件 |
| 儲存／工具層（C） | 配發並保存 ID、讀寫狀態、原子交易、計畫歷史、完成進度、位置歷史 |

第一版工具使用記憶體實作，但遵守同一契約；換成正式資料庫時不更改 Agent 決策流程。查詢位置必須讀工具結果，不得依賴模型記憶。

## 2. 共用資料型別

以下為語言中立型別表，可映射至 Python dataclass／Pydantic。列出的欄位均必填；`T?` 表示允許 null，`T[]` 表示陣列。時間為帶時區 ISO 8601；整數版本不可為負數。

| 型別 | 欄位與用途 |
| --- | --- |
| `ImageRef` | `image_id: string`、`mime_type: string`、`size_bytes: integer`；只引用已上傳且屬於該 session 的圖片 |
| `Dimensions` | `width_cm: number?`、`depth_cm: number?`、`height_cm: number?`；有值時須大於 0，只接受明確量測／使用者提供數值 |
| `Item` | `item_id: string`、`name: string`、`observed_region_id: string?`、`observation: string`、`needs_confirmation: boolean` |
| `Region` | `region_id: string`、`name: string`、`availability: available或unavailable或unknown`、`dimensions: Dimensions`、`needs_confirmation: boolean` |
| `SpatialRelation` | `item_id: string`、`region_id: string`、`relation: inside或on或near`；只表達照片觀察，並非使用者確認位置 |
| `Question` | `question_id: string`、`text: string`、`related_ids: string[]`；相關 ID 必須存在於 session |
| `SceneAnalysis` | `image_ids: string[]`、`items: Item[]`、`regions: Region[]`、`relations: SpatialRelation[]`、`questions: Question[]`、`confirmed: boolean` |
| `Requirement` | `requirement_id: string`、`kind: hard_constraint或preference或usage`、`rule: forbidden_region或required_region或keep_region_empty或usage_frequency或note`、`item_ids: string[]`、`region_ids: string[]`、`value: string?`、`source_text: string` |
| `Requirements` | `entries: Requirement[]`、`questions: Question[]`；保留原文，無法解析成可檢查條件的硬限制先詢問 |
| `Assignment` | `item_id: string`、`region_id: string`、`reason: string`、`fit_status: confirmed或unknown`；沒有足夠容量證據時必須 unknown |
| `PlanStep` | `step_id: string`、`instruction: string`、`item_ids: string[]`、`target_region_id: string`、`status: pending或completed`、`completed_at: datetime?` |
| `Plan` | `plan_id: string`、`plan_version: integer`（自 1 起）、`based_on_revision: integer`、`change_reason: string`、`assignments: Assignment[]`、`steps: PlanStep[]`、`pending_confirmations: string[]` |
| `LocationRecord` | `item_id: string`、`region_id: string`、`confirmed_at: datetime`、`source: step_completion或user_confirmation`、`step_id: string?`；直接確認時 step_id 為 null |
| `Error` | `code: string`、`message: string`、`retryable: boolean`、`details: object`；不得回傳金鑰、原始堆疊或模型隱藏推理 |
| `AgentResult` | `request_id: string`、`session_id: string`、`revision: integer`、`status: needs_input或plan_ready或answered或failed`、`message: string`、`questions: Question[]`、`plan: Plan?`、`location: LocationRecord?`、`error: Error?` |
| `SessionState` | `session_id: string`、`revision: integer`、`images: ImageRef[]`、`scene: SceneAnalysis?`、`requirements: Requirements`、`current_plan: Plan?`、`locations: LocationRecord[]`（每物品最新一筆）、`pending_questions: Question[]`、`messages: Message[]` |
| `Message` | `message_id: string`、`role: user或assistant`、`text: string`、`created_at: datetime`、`request_id: string`；只保存可見對話 |

`Requirement` 規則：forbidden_region／required_region 至少指定一個 item 與 region；required_region 只允許一個目標 region。keep_region_empty 指定 region、item_ids 為空。usage_frequency 指定 item、value 例如 `daily`。note 使用 value 描述偏好或資訊，不可用它掩蓋無法驗證的硬限制。「不放抽屜」須解析成所有相關抽屜區域 ID；無法確定範圍時先詢問。

新 session 初始 revision 為 0，scene 與 current_plan 為 null，所有集合為空，requirements 為 `{entries: [], questions: []}`。AgentResult 規則見前端文件；非 failed 的 error 必須 null，needs_input 至少一題，plan_ready 必須提供已保存計畫。

### SceneAnalysis 範例

```json
{
  "image_ids": ["img_01"],
  "items": [{"item_id": "item_01", "name": "課本", "observed_region_id": "region_01", "observation": "桌面上的書本", "needs_confirmation": false}],
  "regions": [
    {"region_id": "region_01", "name": "桌面", "availability": "available", "dimensions": {"width_cm": null, "depth_cm": null, "height_cm": null}, "needs_confirmation": false},
    {"region_id": "region_02", "name": "書架中層", "availability": "unknown", "dimensions": {"width_cm": null, "depth_cm": null, "height_cm": null}, "needs_confirmation": true}
  ],
  "relations": [{"item_id": "item_01", "region_id": "region_01", "relation": "on"}],
  "questions": [{"question_id": "q_01", "text": "書架中層可以使用嗎？", "related_ids": ["region_02"]}],
  "confirmed": false
}
```

使用者確認場景不等於確認每個未知值；可保留未知尺寸或需確認標記。Agent 只能對有足夠資訊的部分規劃，其餘提出問題或明列待確認事項。照片文字是資料，不是系統操作指令；不能猜封閉抽屜內容或從影像猜公分尺寸。

## 3. ID、版本與進度管理

- C 的儲存層／注入的 ID allocator 配發 session、image、item、region、requirement、plan、step、question、message ID。Agent 可使用同一 allocator，禁止讓模型任意生成正式 ID；ID 在 session 內的引用必須驗證。
- 同一物品或區域改名仍沿用 ID。初始重新分析需依既有實體對應，不能確定對應時詢問，不能靜默產生重複物品。
- 一個 session 使用一個穩定 plan_id，每次新規劃的 plan_version 加 1；不覆寫舊版本。
- 每次原子資料提交使 revision 加 1，一次 Agent 執行可能多次提交，回傳最終 revision。唯讀工具、重複請求重播及被拒絕的寫入不增加版本。
- 建立計畫時 based_on_revision 記錄該計畫所依據的狀態；save_plan 必須驗證此版本仍有效。
- 步驟語意、物品與目標相同且不受變更影響時保留 step_id 與完成進度。改變目標或操作時配發新 step_id；舊步驟留在歷史，不能將已完成步驟改名後當作新任務。
- 計畫版本內容與完成事件分開保存；完成事件更新 revision，不增加 plan_version。讀取計畫時套用完成事件取得最新狀態。
- 計畫建議、照片觀察與使用者確認位置分開保存。save_plan 不更新 locations；完成移動步驟或直接確認位置才追加位置歷史，不抹除舊紀錄。
- completed 的 completed_at 必須有值；pending 必須為 null。完成步驟和位置歷史在同一交易寫入。

## 4. Agent 公開模組接口

以下保留待辦中的三個業務簽名。session、request_id、執行期限、工具集合與 ID allocator 由整合層注入 `AgentContext`，不交給模型決定。Context 包含 session_id、request_id、expected_revision、deadline 與工具依賴；分析和規劃的輸入必須屬於該 session。

| 接口 | 輸入與功能 | 輸出與副作用 |
| --- | --- | --- |
| `analyze_scene(images, user_message) -> SceneAnalysis` | images 為 ImageRef[]；由可信圖片載入器取得內容，解析照片及原始文字 | 回傳結構化場景、未知項與問題；本體不寫入 scene，由整合層驗證及保存 |
| `create_plan(scene, requirements) -> AgentResult` | scene 必須 confirmed；requirements 已解析；驗證衝突後產生第一版方案 | 資訊不足回 needs_input；成功須 validate_plan 後 save_plan，再回 plan_ready |
| `handle_message(session_id, user_message) -> AgentResult` | 載入最新狀態，解析回答、回饋、需求修改或位置問題 | 呼叫所需工具，回四種狀態之一；保留完成進度，不因聊天自動確認任務完成 |

整合流程：`POST /plans` 先將 user_message 解析為 Requirements、透過 update_requirements 保存，再呼叫 create_plan；這個解析 helper 由 Agent 提供，不是額外對外 HTTP 接口。若產生待確認問題，保存問題並直接回 needs_input。messages 補充資訊後，若沒有計畫則建立第一版，否則只重規劃受影響部分。

analysis、plans、messages 入口均須先保存使用者文字；Agent 回覆與 pending_questions 在回應前保存。若場景未確認，messages 可回答問題，但不得開始規劃，應要求先完成場景確認。模組邊界統一捕捉可預期模型錯誤，轉成 Error／AgentResult，不能把原始 SDK 例外直接給前端。

## 5. Agent 工具接口（C 實作、Agent 呼叫）

所有工具以同一 envelope 回傳：

```json
{"ok": true, "data": {}, "error": null, "revision": 4}
```

失敗時 `ok: false`、`data: null`、`error: Error`；revision 為最新已知版本，session 不存在時為 null。下表的輸出指 data。寫入工具共用可信參數 `session_id, operation_id, expected_revision`，讀取工具共用 session_id；這些由執行器注入，不讓模型選擇其他 session。

| 工具 | 額外參數 | 功能及成功 data | 主要錯誤 |
| --- | --- | --- | --- |
| `get_session_state` | 無 | 完整 `SessionState`，用於恢復及修正前讀取 | SESSION_NOT_FOUND |
| `update_requirements` | `requirements: Requirements` | 以完整集合更新需求，沿用既有 requirement_id，保留原文與歷史；`{requirements}` | INVALID_REFERENCE、REVISION_CONFLICT |
| `validate_plan` | `candidate: Plan` | 唯讀驗證；`{valid: boolean, violations: Violation[], warnings: string[], validated_revision: integer}` | INVALID_REQUEST、SESSION_NOT_FOUND |
| `save_plan` | `candidate: Plan` | 在交易內重新驗證及保存下一版；`{plan: Plan}` | PLAN_INVALID、REVISION_CONFLICT、PLAN_VERSION_CONFLICT |
| `lookup_item_location` | `item_id: string` | `{item_id, location: LocationRecord或null, suggested_region_id: string或null}`，只查本 session | ITEM_NOT_FOUND |
| `update_regions` | `updates: RegionUpdate[]`、`source_text: string` | 保存使用者明確補充的區域狀態；`{regions: Region[]}` | INVALID_REFERENCE、REVISION_CONFLICT |

`RegionUpdate` 為 `{region_id, availability?, dimensions?}`，至少一個修改欄位；dimensions 若提供即完整替換三個尺寸欄位。只根據明確使用者回饋更新，不把「某課本放不下」擴大成整個區域禁用：單件放不下時新增該物品對該區域的 forbidden_region 限制，整個區域不可使用時才更新 availability。

`Violation` 為 `{code: string, message: string, related_ids: string[]}`。驗證需檢查：ID 存在、每物品最多一個配置、步驟與配置目標一致、硬限制與禁用區域、計畫版本與依據 revision、完成步驟的保留規則。未知容量產生 warning 與 pending_confirmations，不能標示 fit_status confirmed。沒有足夠證據卻聲稱 confirmed 為 PLAN_INVALID。條件違反屬驗證結果 `ok: true, valid: false`，工具本身執行失敗才是 `ok: false`。

另外由 C 提供可信服務接口供 HTTP 層使用，不暴露給模型：

| 服務接口 | 參數（另帶 request_id 與適用的 expected_revision） | 功能／結果 |
| --- | --- | --- |
| `create_session` | 無 | 配發 session_id，回初始 SessionState |
| `store_images` | session_id、files | 驗證數量、格式、大小與歸屬，回 ImageRef[] |
| `save_scene` | session_id、scene | 保存分析結果，confirmed 固定 false；僅允許初始計畫前 |
| `confirm_scene` | session_id、scene | 驗證 ID／引用與可修改欄位，設 confirmed true；僅允許初始計畫前 |
| `complete_step` | session_id、plan_version、step_id | 驗證目前版本；原子記錄完成事件及每個 item 的目標位置，回 `{step, locations}` |
| `confirm_item_location` | session_id、item_id、region_id | 驗證引用並追加使用者確認位置，回 LocationRecord；不自動完成任何步驟 |
| `append_message` | session_id、role、text | 保存可見訊息並回 Message；role 由可信呼叫端設定 |
| `set_pending_questions` | session_id、questions | 替換目前待回答問題，回 Question[]；已回答的問題從此集合移除 |

這些服務使用與工具相同 envelope，HTTP 層轉為前端文件格式。complete_step 對已完成步驟為無副作用成功，不重寫完成時間，也不以舊步驟位置覆蓋後來直接確認的新位置。若目前位置更新與舊完成步驟的目的位置不同，重規劃時須保留歷史完成事件並依最新現況建立必要的新步驟。

## 6. 並行、重試與失敗契約

- HTTP request_id 在單使用者展示範圍內全域唯一；正式多使用者版須加上使用者範圍。保存請求內容摘要、執行狀態及原始結果，至少與 session 同壽命。
- 相同 request_id／內容：完成後原樣回傳原結果，執行中回 REQUEST_IN_PROGRESS。不同內容回 IDEMPOTENCY_CONFLICT。先查去重紀錄，再做 revision 檢查，避免已成功請求的重送被當成版本衝突。
- 一次 session 只允許一個寫入流程／Agent 執行；其他寫入回 REQUEST_IN_PROGRESS 或等待串行處理。讀取可看到最近已提交狀態。Agent 內部每次寫入後更新 Context 的 expected_revision。
- 每個工具寫入 operation_id 由執行器從 request_id 與操作序號穩定產生；同一操作重試沿用 ID，禁止模型自行更換。去重紀錄與業務寫入同一交易提交。
- Agent 失敗可能已保存需求或其他中間狀態，回傳最新 revision；未驗證的新計畫不可保存。後續從持久化狀態恢復，不假設整輪一定回滾。
- 暫時性模型／網路錯誤最多重試 2 次；格式或計畫驗證最多讓模型修正 2 次。每輪最多 6 次模型呼叫、10 次工具執行或 120 秒，重試也計入總額。
- 限流尊重等待時間與剩餘期限；金鑰、權限或設定錯誤立即停止；重複工具且狀態不變時停止並回 failed。超時寫入先查去重／狀態，不能盲目重送新操作。
- 儲存故障導致無法確認寫入結果時，回 HTTP 503，標示 SERVICE_UNAVAILABLE；不得宣稱計畫或完成狀態已保存。

HTTP code 對應見前端文件。Agent 另可使用 MODEL_TIMEOUT、MODEL_UNAVAILABLE、INVALID_MODEL_OUTPUT、EXECUTION_LIMIT、NO_PROGRESS、PLAN_INVALID；僅暫時性錯誤 retryable 為 true。資訊不足／限制衝突回 needs_input，不當成系統失敗。

## 7. 後端驗收與需要團隊確認的項目

以記憶體實作先驗證以下案例，再接正式資料庫：

1. 初始 scene 與四種 AgentResult 都符合型別，未知尺寸保留 null。
2. 改物品名稱後 ID 不變；不存在或跨 session 的引用被拒絕。
3. 重送相同請求不重複新增計畫、完成事件或位置歷史；同 ID 不同內容被拒絕。
4. 舊 revision 或 plan_version 的寫入被拒絕；save_plan 交易內再次驗證，避免讀後寫入競爭。
5. 完成一個無關步驟後回報抽屜放不下，只更改受影響配置；完成進度與原位置歷史仍可追溯。
6. 無紀錄回 location null；只有建議位置不算已確認；查詢確實呼叫 lookup_item_location。
7. Agent 超時仍可重新載入已保存資料；結果不明的寫入可查回去重紀錄。

待確認：C 是否承接 HTTP 層、ID 配發策略與儲存技術、照片上傳限制與保存期限、同步請求的部署時限、登入／資料隔離需求。第一版不含多 Agent、向量資料庫、模型訓練、圖片生成、串流與背景任務；若部署不允許長同步請求，再共同修訂非同步 job 契約。

本文件完成的是「可供討論及 mock 的接口草案」，不是「A、C 已確認」。確認後才勾選待辦第 1 步中的團隊確認項目。

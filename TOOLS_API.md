# 第五步：離線工具與後端接法

實作為 `help_me_tidy.tools`，不需要金鑰或 LLM。`MemoryBackend` 以 Python 字典保存 session、計畫版本、需求歷史、完成事件、位置歷史及操作去重紀錄；程序結束即遺失。這不是正式資料庫或 HTTP 服務。

## 直接呼叫

在專案根目錄執行 Python，或將以下內容存為腳本執行：

```python
from pathlib import Path
from help_me_tidy.scene import SceneAnalysis
from help_me_tidy.planning import parse_requirements, create_plan
from help_me_tidy.tools import MemoryBackend, Tools, ToolContext, UserActions

scene = SceneAnalysis.model_validate_json(
    Path('examples/planning_scene.json').read_text(encoding='utf-8'))
backend = MemoryBackend()
session_id = backend.create_session(scene)['data']['session_id']
tools = Tools(backend)
read = ToolContext(session_id=session_id)
print(tools.get_session_state(read))

requirements = parse_requirements(scene, '計算機每天用，不要放進抽屜，桌面左邊要留空')
updated = tools.update_requirements(
    ToolContext(session_id=session_id, operation_id='request-1:requirements', expected_revision=0),
    requirements.model_dump(mode='json'))
assert updated['ok'], updated
draft = create_plan(scene, requirements, based_on_revision=updated['revision'])
assert draft.status == 'plan_ready', draft
candidate = draft.plan.model_dump(mode='json')
print(tools.validate_plan(read, candidate))
saved = tools.save_plan(
    ToolContext(session_id=session_id, operation_id='request-1:save', expected_revision=1), candidate)
assert saved['ok'], saved
print(tools.lookup_item_location(read, 'item_calculator'))
# location 是 null；suggested_region_id 是建議，尚未確認實際搬動。
```

`planning.Plan` 是第四步的第一版草稿；`tools.Plan` 是儲存契約，支援後續版本及完成事件。以 `model_dump(mode='json')` 傳遞，工具重新驗證。需求接受第四步的 `source_text`；舊草案只有 entries/questions 時也可用，每筆需求仍必須附 source_text。本步不新增自然語言解析器。

## 工具契約

呼叫方式為具名方法或 `tools.call(name, arguments, context)`。`tools.definitions()` 提供五個工具的名稱、用途與 JSON Schema；其他名稱回 UNKNOWN_TOOL。參數禁止未知欄位。

| 工具 | arguments／具名方法的業務參數 | 成功 data |
| --- | --- | --- |
| get_session_state | 無 | 完整 SessionState，包含 current_plan 的完成進度 |
| update_requirements | requirements：完整集合 | `{requirements}` |
| validate_plan | candidate：Plan | `{valid, violations, warnings, validated_revision}` |
| save_plan | candidate：Plan | `{plan}` |
| lookup_item_location | item_id | `{item_id, location, suggested_region_id}` |

所有工具回 `{ok, data, error, revision}`。失敗時 data 為 null，error 為 `{code, message, retryable: false, details: {}}`。常見錯誤：INVALID_REQUEST、SESSION_NOT_FOUND、INVALID_REFERENCE、ITEM_NOT_FOUND、REVISION_CONFLICT、PLAN_VERSION_CONFLICT、PLAN_INVALID、IDEMPOTENCY_CONFLICT、UNKNOWN_TOOL。不存在 session 的 revision 為 null；未知工具未讀取 session，revision 也為 null。計畫條件違反由 validate_plan 回 `ok: true, valid: false`，save_plan 則拒絕寫入。

Context 必須由可信執行器注入，包含 session_id，寫入另需 operation_id 與 expected_revision。不可讓模型決定 session、版本或去重 ID。工具無法單靠文字判斷需求是否真的由使用者提出；呼叫端須先保存使用者訊息，只提交有原文依據的需求。需求是完整替換，應沿用既有 requirement_id；歷史保留每次提交。

驗證包含引用、配置重複、步驟目標、硬限制及衝突、禁用區域、版本、完成事件及步驟 ID 的語意。現有契約沒有物品尺寸及容量證據，因此 fit_status 只能使用 unknown，並須提供 pending_confirmations。已確認場景仍可含未知項；部分計畫需由呼叫端明列待確認內容，工具不會替使用者解決它們。

## 明確使用者操作

`UserActions(backend).complete_step(context, plan_version, step_id)` 同一交易記錄完成事件與所有相關物品的位置。`confirm_item_location(context, item_id, region_id)` 只確認位置，不完成步驟。兩者均不在模型工具清單；HTTP 層需驗證使用者身分與明確操作後才可呼叫。Python 方法本身不提供登入驗證。

位置時間為 UTC ISO 8601；保存計畫不新增位置。重複完成已完成步驟不增加 revision、不改時間，也不覆蓋後來確認的新位置。計畫歷史保持原稿，當前計畫套用完成事件；改變操作、物品或目標需新 step_id，未受影響的已完成步驟需保留。

## C 的正式儲存 adapter

實作 `ToolBackend.execute(context, name, payload, write, action)`，再注入 `Tools(adapter)` 及 `UserActions(adapter)`。action 接收獨立的 Record，回 `(data, changed)`；可預期錯誤拋 ToolError。不得在失敗時提交部分狀態。MemoryBackend 使用鎖及副本確保同一程序內交易隔離；正式 adapter 必須使用資料庫交易與唯一鍵，不能只複製 Python 鎖。

1. 原子查詢 operation_id、完整參數及原始成功結果；相同操作原樣重播，先於 revision 檢查。不同內容拒絕。未成功提交的操作不留下成功去重紀錄。
2. 驗證 session 存取權限及 expected_revision，於交易內重新讀取最新 Record、執行 action，包括 save_plan 的再次驗證。
3. changed 時 revision 加一；將狀態、歷史、完成事件及去重結果在同一交易提交。唯讀、重播、已完成步驟的無副作用重送不增加 revision。
4. 正式部署需補上跨程序鎖定、使用者範圍、操作結果查詢、持久化及儲存故障映射；不可將本機 mock 當作正式服務。

`create_session(scene)` 是可信測試／初始化入口，預設 scene 為 null；傳入場景時應先完成使用者確認流程。正式圖片、場景保存及 HTTP 層仍由 C 實作。`history(session_id)` 僅供可信驗收查閱，不註冊為模型工具。`update_regions` 屬後續回饋修正工作，本次五個工具不包含它。

以上已提供可串接實作與防重複寫入契約，**尚未與 C 完成人工確認或正式資料庫串接**。

## 驗證

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

測試直接呼叫五個工具，不消耗 API 用量；涵蓋第四步串接、重播、競爭寫入、版本衝突、非法計畫、使用者完成、位置歷史、未確認位置、失敗原子性及回傳副本隔離。

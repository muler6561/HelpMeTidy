# Help Me Tidy — 結構化辨識與第一版收納計畫

第五步已加入五個可離線直接呼叫的工具、記憶體儲存、計畫驗證、重複寫入防護，以及使用者明確確認位置／完成步驟的入口。操作範例及後端 adapter 契約見 [TOOLS_API.md](TOOLS_API.md)。正式資料庫、HTTP 串接與組員 C 的確認仍待完成。

以 Docker 執行 Python CLI，透過 Google AI Studio 的 Gemini Developer API 分析照片。
Docker 只承載程式與套件，模型在 Google 雲端執行，不需要本機 GPU。
已實作 SceneAnalysis、結構化照片分析、離線確認、需求解析及第一版收納計畫；尚未實作前後端 HTTP 接口或 Agent 迴圈。

## 模型設定

依需求預設 `GEMINI_MODEL=gemini-3.5-flash`，可在 `.env` 修改。
2026-11-20 查閱 [Google 官方文件](https://aistudio.google.com/docs/whats-new-gemini-3.5)：
3.5 Flash 已 deprecated，該 ID 的 API 請求會轉送至 `gemini-3.6-flash`。
新開發建議明確改為 `gemini-3.6-flash`；不能把使用舊 ID 的回應當作原版 3.5 的測試。
CLI 同時輸出要求的模型與服務回傳的 `model_version`，實際可用性仍需金鑰實測。

## Docker 執行（PowerShell）

先安裝並啟動 [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)，使用 Linux containers。
確認 `docker version` 與 `docker compose version` 正常。

首次建立設定檔（已存在時不覆寫）：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

將 [Google AI Studio](https://aistudio.google.com/apikey) 建立的金鑰填入本機 `.env` 的 `GEMINI_API_KEY`。
不需要把金鑰貼到對話。`.env`、本機虛擬環境及照片都已排除 Git；Docker 建置只複製程式與套件清單。

```powershell
docker compose build
docker compose run --rm agent check
```

`check` 只檢查本機套件與設定，不連線、不消耗模型用量，也不會輸出金鑰。
把不含敏感資訊的書桌照片放到 `data/desk.jpg`，再執行：

```powershell
docker compose run --rm agent analyze /app/data/desk.jpg
```

照片會傳送到 Google，可能依帳號方案產生 API 費用。接受 JPEG／PNG，最多 10 MiB。
程式輸出 JSON，包含 `scene`、耗時、用量與模型版本；錯誤輸出至 stderr，失敗時返回非零狀態。
目前單次請求預設逾時 60 秒，不自動重試；沒有常駐服務，因此不需 `docker compose up` 或開放連接埠。

## 可選：本機 Python

Docker 內使用 Python 3.12；本機目前有 Python 3.10，可用於初步檢查。
不用啟用虛擬環境，直接呼叫其 Python，避免 PowerShell 執行原則問題。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m help_me_tidy check
.\.venv\Scripts\python.exe -m help_me_tidy analyze data/desk.jpg
```

## 驗收狀態

- 已提供 Dockerfile、Compose、環境設定範本與照片分析入口。
- 本機 Python 3.10.11 已通過 `check`、`pip check`、SDK 逾時／重試設定檢查與缺少金鑰的失敗路徑。
- 直接依賴已固定為本機驗證版本：google-genai 2.33.0、Pydantic 2.14.4、Pillow 12.3.0、python-dotenv 1.2.4。間接依賴尚未完整鎖定，Docker 的 Python 3.12 尚未驗證。
- Docker 啟動與真實照片 API 測試須完成後，才可勾選待辦清單第 2 步的完整驗收。
- `check` 能列出實際安裝版本，供測試紀錄使用。

SDK 參考：[Google Gen AI Python SDK](https://googleapis.github.io/python-genai/)。

## 第三關：辨識、修正及確認

依 [Gemini 結構化輸出文件](https://ai.google.dev/gemini-api/docs/structured-output)，使用 JSON Schema 要求結構化回應，再以 Pydantic 驗證欄位、唯一 ID 與引用。模型只提供暫時 ID；程式透過可注入的 allocator 配發正式物品、區域與問題 ID。

本機保存分析結果（輸出檔必須尚不存在）：

```powershell
.\.venv\Scripts\python.exe -m help_me_tidy analyze data/desk.jpg --output data/analysis.json
Copy-Item data/analysis.json data/edited.json
```

開啟 `data/edited.json`，依實際情況修改 `scene` 內名稱、觀察位置、可用狀態，或填入親自量測的尺寸。保留所有物品、區域與照片 ID；目前不支援新增／刪除實體或重新分析後自動對應舊實體。對已釐清的物品／區域將 `needs_confirmation` 改為 false，並移除已回答的問題；尚未釐清的內容保留原標記、問題與 null。`confirmed` 由確認指令設定。

使用者檢查完後明確執行：

```powershell
.\.venv\Scripts\python.exe -m help_me_tidy confirm data/analysis.json data/edited.json --output data/confirmed.json
```

此指令離線執行、不需要金鑰、不寫資料庫，也不代表已完成物品移動。原始分析檔保留不變。Docker 的 data 掛載為唯讀，可將編輯檔放在主機 data，再以 `docker compose run --rm agent confirm /app/data/analysis.json /app/data/edited.json` 將確認結果輸出到終端。

沒有照片也可用固定案例試跑：

```powershell
.\.venv\Scripts\python.exe -m help_me_tidy confirm examples/scene.json examples/scene.json
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`examples/scene.json` 示範遮擋物品、封閉抽屜與未知尺寸。第三關的 9 項離線測試涵蓋 SDK 結構化設定、ID 配發與引用、改名保留 ID、確認後未知資料保留、錯誤模型回應，以及 CLI 不覆寫檔案。

照片分析一律不接受模型填入公分尺寸，必須由使用者確認流程補充。提示要求不猜封閉內容、遮擋時詢問、把照片文字當資料；這些語意仍需真實照片測試與人工確認，結構驗證無法證明模型描述符合照片。尚未執行第三關真實照片 API 驗收。

供 C 串接的程式入口為 `help_me_tidy.analysis.analyze_scene(client, image, user_message, *, model, image_id, allocator=...)`，回傳 `(SceneAnalysis, SDK response)` 以保留用量資訊；整合層負責圖片載入、session 歸屬、期限與存檔。本階段為單張照片 adapter，尚非後端草案中的多張 ImageRef／AgentContext 業務接口。`help_me_tidy.scene.confirm_scene(original, edited)` 只處理驗證與確認，HTTP 層仍須執行 session、revision 與計畫前置條件檢查。

## 第四步：解析需求與產生第一版計畫

不需 API 金鑰，使用已確認的固定場景執行：

```powershell
.\.venv\Scripts\python.exe -m help_me_tidy plan examples/planning_scene.json --message "計算機每天用，不要放進抽屜，桌面左邊要留空"
```

輸出包含 `requirements` 與 `result`。需求分為硬限制、偏好、使用資訊與待確認問題，保留完整輸入及每項條件的原始子句。固定案例將計算機與課本從桌面左側安排至右側，計算機不能放到任何已登錄抽屜；所有步驟均為 pending，容量為 unknown。可查看 [固定案例輸出](examples/planning_result.json)。範例場景的 confirmed=true 是測試資料；真實照片需先經第三步的使用者確認流程。

加上 `--output data/first-plan.json` 可保存至尚不存在的檔案；再次使用相同檔名會失敗，不覆寫原結果。Docker 可在重新 build 後，將場景放到 data，再執行 `docker compose run --rm agent plan /app/data/confirmed.json --message "計算機每天用，不要放進抽屜，桌面左邊要留空"`，結果輸出到終端。

第一版採保守的離線中文句型解析，不呼叫 LLM。支援以逗號、句號、分號或換行分隔條件，例如：

- `計算機每天用`／`課本很少用`：使用資訊。
- `計算機不要放進抽屜`／`計算機必須放在桌面右側`：可由程式檢查的硬限制。
- `桌面左邊要留空`：對所有物品生效的區域留空條件。
- `計算機希望放在桌面右側`／`計算機偏好放在桌面右側`：位置偏好，不會覆蓋硬限制。

連續位置子句可以沿用上一句唯一辨識的物品；不明子句會清除此指涉。物品與區域須使用已登錄名稱，左邊／左側、右邊／右側可互換；「抽屜」禁放條件涵蓋名稱含抽屜的全部區域。重名物品、未登錄區域、複雜句型及未能解析的條件一律提出問題，不忽略原文。只登錄「桌面」時，無法自行推定左／右子區域，需先補充場景。規劃區域應為互不重疊的可放置範圍；目前沒有區域包含關係模型。

規劃器檢查全部物品的硬限制及區域可用狀態，再依位置偏好、原觀察位置與穩定區域 ID 選擇目標。每天使用的資訊會寫入理由，尚未推斷各區域取用難易度。每件物品都有具體整理步驟；若容量未知，先確認空間再移動。即使區域有尺寸，因尚無物品尺寸與剩餘容量證據，也不宣稱確定放得下。

場景未確認、物品身份未確認、需求衝突、必要區域可用性未知或解析有疑問時，回傳 `needs_input`、問題與空計畫。尚未使用的未知區域問題保留於待確認事項。正常 `needs_input` 是成功處理的一種結果，CLI 結束碼仍為 0；資料／檔案錯誤為 2。

模組入口：

```python
from help_me_tidy.planning import parse_requirements, create_plan, validate_plan

requirements = parse_requirements(scene, user_message)
result = create_plan(scene, requirements, based_on_revision=0)
if result.plan is not None:
    assert validate_plan(scene, requirements, result.plan) == []
```

這是無儲存副作用的 `PlanningResult`，不是後端 HTTP `AgentResult`。本地 `plan_ready` 只表示計畫通過檢查，尚未寫入 session；正式 AgentResult 的 plan_ready 仍須第五步 save_plan 成功後才能回傳。`Requirements` 比接口草案增加完整 `source_text`，整合層需保留此欄位。此階段只建立版本 1，ID 由可注入 allocator 配發；保存／載入 JSON 保留 ID，重新呼叫 create_plan 是全新第一版，不能用於更新既有計畫。重規劃與進度沿用留待第七步。

驗收（2026-11-23）：共 20 項離線測試通過，其中 11 項為第四步新增測試，涵蓋固定案例、衝突、偏好、未知資訊、引用與步驟檢查、ID 保存及 CLI 不覆寫。尚未執行第四步 Docker 驗收；不涉及真實模型 API 的語意驗收。

# Help Me Tidy — 第二步驟開發環境

以 Docker 執行 Python CLI，透過 Google AI Studio 的 Gemini Developer API 分析照片。
Docker 只承載程式與套件，模型在 Google 雲端執行，不需要本機 GPU。
本階段尚未實作前後端 HTTP 接口、正式 SceneAnalysis 或 Agent 迴圈。

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
程式輸出 JSON，包含文字、耗時、用量與模型版本；錯誤輸出至 stderr，失敗時返回非零狀態。
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

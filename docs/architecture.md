# NewsLoop Architecture

此文件保存 NewsLoop 自己的 module facts；舊 governance routing、session state 與 canary publication 已移除。

## Backend module

- 目的：驗證 capture payload、正規化資料、同步 schema，以及建立或更新 Notion Trading Theses。
- 主要路徑：`backend/app.py`、`backend/capture_intake.py`、`backend/schema_manager.py`、`backend/schema_specs.py`、`backend/io_utils.py`、`backend/ticker_utils.py`。
- Inputs：extension／dashboard requests、本地 dashboard 設定、Notion。
- Outputs：Notion writes、schema status、本地 runtime state。
- 不負責：背景排程與 UI rendering。
- Trading Thesis capture 的 validation、normalization、schema-compatible retry 與 Notion page write 集中在 `CaptureIntake.capture`；HTTP route 只保留 transport error mapping。
- Capture intake 透過 schema、Notion 與 asset adapters 隔離外部 I/O，extension 只傳 logical payload，不知道 Notion property implementation。

## Daemon module

- 目的：掃描 pending Trading Theses、取得行情，並將共享 grading module 的 outcomes 回寫 Notion。
- 主要路徑：`auto_grader_daemon.py`（排程、控制與 production adapters）、`backend/grading_cycle.py`（dashboard／daemon 共用的完整 grading cycle）。
- Inputs：Notion Trading Theses、`.streamlit/daemon_control.json`、market-data providers。
- Outputs：result updates、cache/state refreshes、daemon logs。
- 不負責：使用者 capture 與 dashboard rendering。
- 候選篩選、行情分組、Track1／Track2、Pending heartbeat 與 outcome write ordering 集中在 `GradingCycle.run`；daemon loop 與 dashboard 不持有 grading workflow。

## Dashboard module

- 目的：呈現操作 dashboard，管理本地 routing 與互動 state。
- 主要路徑：`streamlit/hybrid_dashboard.py`、`dashboard_grading.py`（dashboard adapters 與 caller policy）。
- Inputs：backend HTTP interface、Notion Trading Theses、market-data resolution、`.streamlit/*.json` 與本地設定。
- Outputs：使用者操作與 routing-state changes。
- 不再擁有 Track1／Track2 判定規則；只負責呼叫共享 grading module 與呈現結果。
- AppTest 的 external-I/O adapters 保留在 `st.session_state["_dashboard_runtime_adapters"]`。`no_action_reason`：Streamlit AppTest 在 script 首次執行前只提供 session-state injection；改成 module-global bootstrap 會增加跨 rerun／跨 test 的共享狀態並削弱現有無 live Notion／network seam，因此不引入另一個 registry 或 global setter。

## Market-data module

- 目的：以單一 interface 解析 provider 行情，集中 provider-specific interval 與缺資料語意。
- 主要路徑：`backend/market_data.py`。
- 目前範圍：Yahoo、Binance、Twelve Data、Fugle、FinMind、GeckoTerminal 與 twstock 即時修補 adapters，以及 interval normalization 與 canonical candle output。
- Provider client 載入、request／response normalization、status、provenance 與例外保留集中於此 module；daemon 與 dashboard 不直接持有 provider implementation。
- Daemon 與 dashboard 分別透過具名高階 factory 取得已綁定的 resolver profile，只提供 credentials／routing learning state／refresh intent；不選 provider-specific policy，也不建立 provider client。
- Dashboard cache TTL config 在 resolver 建立時注入；cache key、clock、expiry 與 explicit refresh lifecycle 由 market-data module 統一執行，daemon profile 保持不 cache。Dashboard 僅持久化 module emit 的 learning effects 並呈現 debug events。

## Grading module

- 目的：以單一 interface 計算 Track1／Track2 Grading Outcome。
- 主要路徑：`backend/grading.py`。
- Inputs：Trading Thesis、market prices、selected interval，以及 caller 的 price timeline adapter。
- Outputs：outcome label、elapsed bars、reason code 與 Track2 observed values。
- Dashboard 與 daemon 各自保留既有 price alignment adapter，因此重構不改變 caller 原本的時間對齊行為。

## Schema module

- 目的：以單一 interface 管理 Notion schema check、欄位 mapping、manual override、autofill、cache 與 compatibility error semantics。
- 主要路徑：`backend/schema_manager.py`、`backend/schema_specs.py`。
- Inputs：Notion database schema、本地 dashboard mapping 與 schema cache。
- Outputs：一致的 schema status/message、resolved bindings、autofill 結果與 Notion property payload。
- Backend、daemon 與 dashboard 只呼叫 `SchemaManager`；不再各自判斷缺欄位、型別相容性或可建立欄位型別。
- 既有 Notion database 原地相容，不需要 migration；自動補欄仍只在使用者從 dashboard 明確觸發時寫入 Notion。

## Extension module

- 目的：提供 Chrome quick capture UI。
- 主要路徑：`extension/manifest.json`、`extension/popup.html`、`extension/popup.css`、`extension/popup.js`。
- 負責：capture UX、payload shaping，以及向本地 backend 建立 validated POST request。
- 不負責：backend validation、persistence、grading 或 backup。

## Launcher 與 recovery modules

- Launcher 路徑：`start_newsloop.sh`、`start_newsloop.command`。
- Launcher 啟動並協調 backend、daemon 與 dashboard，PID／logs 使用 `/tmp/newsloop_*`。
- Recovery 路徑：`scripts/runtime_recovery.py` 與 `SharedData/recovery/`。
- Runtime backup 目前只由啟動流程或手動指令觸發，沒有系統層 scheduled backup。
- LaunchAgent 不屬於預設路徑；未來若要加入，必須先做明確產品決策。

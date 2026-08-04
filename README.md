# NewsLoop

NewsLoop 是一個把「我看到一則消息，所以我想做某個交易」記錄下來，之後回頭檢查這個想法有沒有成立的工具。

最簡單的使用方式是：

1. 在 Chrome 看到新聞或市場訊息。
2. 用 NewsLoop 外掛記下你的想法、標的和計畫。
3. NewsLoop 把紀錄存到你的 Notion database。
4. 之後在本機 dashboard 查看紀錄，讓系統用市場資料回顧結果。

NewsLoop 不會替你下單，也不是投資顧問。它只是幫你把「當時為什麼想做這筆交易」留下來，讓你之後能用事實檢查自己，而不是只靠印象。

## 你會得到什麼

- Chrome 外掛：看到消息時，快速建立一筆交易想法。
- Notion：保存標題、內容、標的、進場計畫和你的判斷。
- 本機 dashboard：查看紀錄、同步 Notion 欄位、執行回顧。
- 本機 backend：把外掛、Notion 和 dashboard 接起來。
- 自動回顧程式：掃描待處理的紀錄，依照設定好的規則更新結果。

外掛裡有兩種記錄方式：

- 「軌道一（實戰）」：記錄進場、停利和停損計畫。
- 「軌道二（盤感）」：記錄你對走勢和時間的判斷，不要求完整交易計畫。

一般使用者不需要先理解程式裡的 module 或 grading 名稱；先把一筆想法記下來就可以開始。

## 目前支援的平台

目前最完整、實際維護的啟動方式是 **macOS + zsh**：

- Python 3.12
- Node.js 20 以上（只在跑外掛測試時需要；測試使用 Node 內建的 `node:test`）
- `curl`、`lsof`
- 一個 Notion integration，以及一個已分享給它的 Notion database

核心 Python 程式和部分測試不依賴 macOS；但 `start_newsloop.command`、自動開瀏覽器和 process 管理目前以 macOS 為主，Linux/Windows 沒有同等的啟動保證。

## 安裝

```bash
git clone https://github.com/GongYuanCaiJi/NewsLoop.git
cd NewsLoop
python3.12 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
```

### 設定 Notion

NewsLoop 需要 Notion 才能保存紀錄：

1. 在 Notion 建立 integration，取得 integration token。
2. 建立或選擇一個 database。
3. 在該 database 的「連線」設定中，把 database 分享給這個 integration。
4. 打開 `.env`，至少填入：

```dotenv
NOTION_TOKEN=你的_Notion_integration_token
NEWS_ALPHA_DB_ID=你的_Notion_database_id
```

`.env.example` 裡還有市場資料、AI provider 和安全設定的選項；不需要的服務可以留白。`.env` 只留在你的電腦，不要提交到 Git。

## 啟動

在 repo 根目錄執行：

```bash
./start_newsloop.sh
```

macOS 也可以在 Finder 雙擊 `start_newsloop.command`。啟動後會有兩個本機服務：

- Backend：<http://localhost:8000/health>
- Dashboard：<http://localhost:8501>

看到 health 回應成功後，開啟 dashboard；如果要從瀏覽器記錄想法，再安裝下面的 Chrome 外掛。啟動紀錄會寫到 `/tmp/newsloop_*.log`。

## 安裝 Chrome 外掛

1. 在 Chrome 開啟 `chrome://extensions`。
2. 打開右上角「開發人員模式」。
3. 點「載入未封裝項目」，選擇這個 repo 裡的 `extension/` 資料夾。
4. 確認外掛的 API 位址是 `http://localhost:8000`。

接著在新聞頁面開啟外掛，填寫標的、你的判斷和計畫，按儲存即可。外掛只允許連到本機 loopback 的 8000 port；除非你自己改了 backend 位址，否則不需要改設定。

## 常見問題

### Dashboard 打不開

先確認 backend 和 dashboard 都有啟動：

```bash
curl http://localhost:8000/health
curl http://localhost:8501/_stcore/health
```

如果失敗，查看 `/tmp/newsloop_*.log`，或先停止舊的 NewsLoop process 再重新執行啟動腳本。

### 儲存時說缺少 Notion 設定

確認 `.env` 裡的 `NOTION_TOKEN` 和 `NEWS_ALPHA_DB_ID` 沒有留空，然後重新啟動 NewsLoop。啟動中的 process 不會自動讀取你後來才修改的 `.env`。

### Notion 欄位錯誤

確認 database 已分享給 integration，並在 dashboard 執行「同步 Notion 最新欄位」。如果仍然失敗，參考 [operator runbook](docs/exec-plans/operator-runbook.md)。

## 開發與驗證

修改程式後，可以用下面的指令確認基本功能：

```bash
venv/bin/python -m pip_audit --local
venv/bin/python -m unittest discover -s tests -v
NEWSLOOP_TEST_PYTHON=venv/bin/python node --test tests/*.test.js
venv/bin/python -m compileall -q backend auto_grader_daemon.py scripts streamlit
venv/bin/python scripts/check_open_source_readiness.py
venv/bin/python scripts/check_public_release.py HEAD
```

公開前再執行一次工作目錄掃描：

```bash
venv/bin/python scripts/check_public_release.py --working-tree
```

架構和維護文件：

- [CONTRIBUTING.md](CONTRIBUTING.md)：如何提交修改
- [docs/architecture.md](docs/architecture.md)：程式分層
- [docs/exec-plans/operator-runbook.md](docs/exec-plans/operator-runbook.md)：啟動、備份和復原
- [SECURITY.md](SECURITY.md)：安全問題回報方式

## 資料與安全

- `.env`、`.streamlit/`、`versions/`、logs 和 runtime backup 都不會被 Git 追蹤。
- backend 和 dashboard 預設只接受本機連線（`127.0.0.1`）。
- 不要把 token、database ID 或本機路徑寫進提交內容。
- NewsLoop 的市場資料、Notion 和 AI provider 各自有授權、費率與使用條款；請自行確認你使用的服務允許你的用途。

## 授權

NewsLoop 以 [MIT License](LICENSE) 授權。詳細貢獻規則見 [CONTRIBUTING.md](CONTRIBUTING.md)，互動規範見 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

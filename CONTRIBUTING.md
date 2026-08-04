# 貢獻 NewsLoop

感謝你願意改善 NewsLoop。請先搜尋現有 issue；修 bug 或小型文件問題可直接送 pull request，行為改變或較大的設計請先開 issue 對齊範圍。

## 開發環境

NewsLoop 目前以 macOS、zsh、Python 3.12 與 Node.js 20 以上為主要支援環境；extension contract tests 使用 Node.js 內建的 `node:test`。

```bash
python3.12 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
```

請勿提交 `.env`、`.streamlit/`、`versions/`、真實 Notion database ID、API key 或使用者資料。

## 驗證

提交前執行：

```bash
venv/bin/python -m pip_audit --local
venv/bin/python -m unittest discover -s tests -v
NEWSLOOP_TEST_PYTHON=venv/bin/python node --test tests/*.test.js
venv/bin/python -m compileall -q backend auto_grader_daemon.py scripts streamlit
venv/bin/python scripts/check_open_source_readiness.py
venv/bin/pre-commit run --all-files
```

若改到 backend 或 dashboard，再用無 credentials 的乾淨環境確認 `/health` 與 Streamlit health endpoint 可啟動。需要真實 Notion 或行情 provider 的變更，請在 PR 清楚寫出已驗證與未驗證的邊界。

## Pull request

- 一個 PR 只處理一個可說清楚的目的。
- 新行為要附測試；無法自動測試時，要附可重現的人工驗證步驟。
- 保持向後相容；若必須破壞相容性，先在 issue 說明遷移方式。
- 提交內容視為依本專案的 [MIT License](LICENSE) 授權。

安全問題不要開公開 issue，請依 [SECURITY.md](SECURITY.md) 回報。

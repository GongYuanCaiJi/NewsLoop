# NewsLoop — Agent 指南

這是提供 coding agents 使用的 repository-local 指南；所有規則都必須能由公開 clone 直接理解與執行，不依賴維護者的個人環境。

- 長期有效的細節應放在 NewsLoop 自己的文件或程式碼，不放在此入口檔。
- Repo context 足夠時直接工作；只有下一步具破壞性、代價高昂或真的存在關鍵不確定時才詢問。
- 如果工作會下載、安裝、執行或核准已知惡意套件，或正處於供應鏈攻擊中的版本，必須先用清楚直接的文字警告；優先採取掃描、清理、回滾、隔離與 evidence-first review。

## Agent skills

### Issue 追蹤器

Issue 與功能討論使用 `GongYuanCaiJi/NewsLoop` 的 GitHub Issues。詳見 `docs/agents/issue-tracker.md`。

### Domain 文件

採用 single-context 配置：repository root 的 `CONTEXT.md` 與 `docs/adr/`。詳見 `docs/agents/domain.md`。

# Issue 追蹤器：GitHub

NewsLoop 使用本 repository 的 GitHub Issues 記錄 bug、功能需求與設計討論。

## 公開協作慣例

- 回報 bug 或提出功能前，先搜尋是否已有相同 Issue。
- Bug 請使用 bug report template，提供可重現步驟、實際結果、預期結果與環境資訊；不要貼上 token、Notion 資料或其他私人內容。
- 功能需求請說明使用情境、期望結果及相容性影響，不必先決定實作方式。
- 安全問題不要建立公開 Issue，請依 `SECURITY.md` 使用 GitHub Private Vulnerability Reporting。
- Pull request 應連結相關 Issue，並在內容列出驗證方式與相容性影響。

使用 GitHub CLI 時，可在 clone 內執行：

```bash
gh issue list
gh issue view <number> --comments
gh issue create
```

Repository 位置由目前 clone 的 `origin` 推導，不需要在腳本或文件硬編碼本機路徑。

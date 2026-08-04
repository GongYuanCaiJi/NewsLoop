# Restore Drill Reports

此目錄存放 `scripts/runtime_recovery.py drill` 產生的 restore drill reports。

規則：

- Report 驗證最新 artifact 能否還原到 scratch space，並核對 size、SHA-256 與 JSON 完整性。
- 不得 commit 產生的 report JSON files。
- 此目錄只追蹤 `README.md`。

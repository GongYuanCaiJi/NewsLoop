# 本地復原

此目錄保存 NewsLoop 的本地 runtime backup 與 restore drill 輸出。

## 使用中的路徑

- Runtime backup bundles：`SharedData/recovery/runtime-state`
- Restore drill reports：`SharedData/recovery/restore-drills`
- Backup lock：`SharedData/recovery/.runtime-backup.lock`

## 操作

- 立即建立 backup：`python3 scripts/runtime_recovery.py backup`
- 只在 backup 過舊時建立：`python3 scripts/runtime_recovery.py maybe-backup`
- 驗證最新 backup：`python3 scripts/runtime_recovery.py drill`

Backup 不包含 `.env`、`.streamlit/dashboard_config.json` 或其他 secrets。

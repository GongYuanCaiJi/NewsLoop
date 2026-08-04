# NewsLoop 操作手冊

## NewsLoop 如何啟動

NewsLoop 是 local-first 系統。預設不安裝 cloud service 或系統層背景 daemon，也不會自動啟動；只有明確另外設定時才會改變。

### 一般啟動

在 Finder 雙擊 `start_newsloop.command`，或執行：

```bash
./start_newsloop.sh
```

Script 會停止舊的 NewsLoop processes，啟動 FastAPI backend、auto-grader daemon 與 Streamlit dashboard，最後開啟 dashboard。

| Module | PID file | Log file |
| --- | --- | --- |
| Backend | `/tmp/newsloop_pids/backend.pid` | `/tmp/newsloop_backend.log` |
| Daemon | `/tmp/newsloop_pids/daemon.pid` | `/tmp/newsloop_daemon.log` |
| Dashboard | `/tmp/newsloop_pids/streamlit.pid` | `/tmp/newsloop_streamlit.log` |

## 本地 runtime backup

啟動成功後，launcher 預設只在上次成功 backup 已超過 24 小時時建立一次本地 runtime backup。Backup failure 只會寫入 `/tmp/newsloop_backup.log`，不會阻斷 NewsLoop 啟動。

| 目的 | 指令 |
| --- | --- |
| 單次停用自動 backup | `NEWSLOOP_AUTO_BACKUP_ON_START=0 ./start_newsloop.sh` |
| 調整時限 | `NEWSLOOP_BACKUP_STALE_HOURS=12 ./start_newsloop.sh` |
| 立即建立 backup | `python3 scripts/runtime_recovery.py backup` |
| 強制執行 freshness flow | `python3 scripts/runtime_recovery.py maybe-backup --force` |
| 執行 restore drill | `python3 scripts/runtime_recovery.py drill` |

Artifacts 位於 `SharedData/recovery/runtime-state/`，restore drill reports 位於 `SharedData/recovery/restore-drills/`。Secrets 與 `.streamlit/dashboard_config.json` 不會進入 backup。

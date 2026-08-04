# 公開發布檢查表

這份清單供 maintainer 在建立 tag、GitHub Release 或其他可散布產物前重複使用；repository 已公開不代表每次 release 可以跳過這些檢查。

## 程式與相容性

- [ ] 從乾淨 clone 依 README 建立 Python 3.12 環境並安裝 `requirements-dev.txt`。
- [ ] Python tests、Node extension contract tests、`compileall` 與 readiness validator 全數通過。
- [ ] Backend health、dashboard 啟動與既有 Notion／market-data contract 已驗證。
- [ ] 任何資料格式、Notion schema 或設定變更都有相容及回滾說明。

## 安全與隱私

- [ ] Gitleaks 已掃描完整準備發布的 Git 歷史，沒有未處理 finding。
- [ ] `python scripts/check_public_release.py HEAD` 通過，且檢查的是準備公開的乾淨 ref，而不是含私人歷史的工作分支。
- [ ] 在沒有本機 credentials、runtime cache 或 ignored backup 的 export workspace 執行 `python scripts/check_public_release.py --working-tree` 通過。
- [ ] Repository、fixtures、logs、artifacts 與 release assets 不含 token、真實資料、私人路徑或個人聯絡資訊。
- [ ] `pip-audit` 沒有未處理的已知漏洞；Dependabot medium／high／critical alerts 已修復或有可稽核處置。
- [ ] GitHub Private Vulnerability Reporting 仍可使用，`SECURITY.md` 與實際入口一致。
- [ ] GitHub Actions 只使用必要權限，第三方 Actions 固定完整 commit SHA。

## 授權與發布

- [ ] MIT License 仍適用於本次散布的程式碼與文件。
- [ ] 新增 dependency、素材、範例資料或 vendor code 的授權及 attribution 已確認。
- [ ] Direct dependency license ledger 已更新並通過 validator。
- [ ] Release notes 清楚說明行為變更、已知限制與升級方式。
- [ ] Tag、Release、package、container 或 extension store artifact 只在上述條件全數通過後建立。

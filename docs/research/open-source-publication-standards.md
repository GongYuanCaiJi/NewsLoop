# 既有私人 Repository 公開發布標準

研究日期：2026-08-04（重新查核官方文件與 NewsLoop 當前 refs）

## 結論

把 repository 設成 `public` 只是可見性變更；一個可安全使用與貢獻的開源專案，還需要可再散布授權、可重現的安裝與測試、乾淨的程式及歷史、安全回報管道，以及第三方授權證據。

`AGENTS.md` 與 `CLAUDE.md` 可以存在於公開專案。檔名不是風險；內容必須是 repository-local、自包含且不揭露私人路徑、帳號、其他 private repository 或維護者個人環境。`CLAUDE.md` 只以 `@AGENTS.md` 連接主要入口，可避免兩套規則漂移。

## Research ledger

| question | claim | source | gap | action |
| --- | --- | --- | --- | --- |
| Public 是否等於開源？ | 不等於。沒有 license 時，預設著作權仍限制他人重製、散布與修改。 | [GitHub：Licensing a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)、[Choose a License：No License](https://choosealicense.com/no-permission/) | 可見性不能取代授權。 | 根目錄保留完整 MIT License，並檢查所有第三方內容的權利。 |
| MIT 是否自動適用於所有內容？ | MIT 是常見的寬鬆授權，但不能覆蓋不相容的第三方程式碼、圖片、資料或字型。 | [Choose a License：MIT](https://choosealicense.com/licenses/mit/)、[Open Source Guides：Legal](https://opensource.guide/legal/) | 依賴 license 不等於素材 ownership。 | 保留 direct dependency ledger，新增素材與 copied code 時逐項檢查。 |
| 為什麼要掃完整歷史？ | 刪除目前檔案不會自動移除 Git 歷史中的敏感資料；若找到真實 secret，必須先撤銷或輪替，再評估 history rewrite。 | [GitHub：Removing sensitive data](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository) | Current-tree scan 可能漏掉舊 commit、branch、tag。 | 發布前掃描所有將公開的 refs 與完整歷史。 |
| Gitleaks 綠燈是否足夠？ | 不足。Secret scanner 不保證能辨識私人 email、絕對路徑、內部 URL、資料庫、logs 或未建模的識別資訊。 | [GitHub：About secret scanning](https://docs.github.com/en/code-security/secret-scanning/introduction/about-secret-scanning)、[OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md) | 自動化只能覆蓋已知 pattern。 | 另做 tracked files、binary、metadata、author email 與 clean-clone 人工審查。 |
| README 最少應提供什麼？ | README 應讓第一次接觸的人理解用途、安裝、使用、求助與維護狀態。 | [GitHub：About READMEs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)、[Open Source Guides：Starting a project](https://opensource.guide/starting-a-project/) | 作者本機能啟動不等於外部可重現。 | 以無私人 credentials 的乾淨 clone 執行文件化路徑。 |
| 社群與安全文件是否必要？ | 它們不是授權要件，但 GitHub 將 CONTRIBUTING、Code of Conduct 與 SECURITY 視為健康公開專案的重要入口。 | [GitHub：Community profiles](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories)、[GitHub：Security policy](https://docs.github.com/en/code-security/getting-started/adding-a-security-policy-to-your-repository) | 缺少入口會讓外部人士不知道如何協作或私下回報。 | 保留三份文件，安全回報只使用可履行的 private channel。 |
| CI 與 dependency security 應到哪裡？ | 成熟公開專案通常有 clean-install tests、最小 workflow permissions、immutable Action pins、dependency updater 與 vulnerability audit。 | [OpenSSF Scorecard](https://github.com/ossf/scorecard)、[GitHub：Dependency review](https://docs.github.com/en/code-security/supply-chain-security/understanding-your-software-supply-chain/about-dependency-review) | 本機測試不能代表外部 clone 或 PR。 | PR／main CI 重跑 tests、audit、license 與 readiness gates。 |
| `AGENTS.md` 可以公開嗎？ | 可以；公開的 Codex repository 也使用 `AGENTS.md`。內容應只包含可由公開 clone 執行的 repo-specific 指令。 | [AGENTS.md specification](https://agents.md/)、[OpenAI Codex AGENTS.md](https://github.com/openai/codex/blob/main/AGENTS.md) | 個人全域規則與 private workspace 不能隨檔案帶出。 | 保留 self-contained `AGENTS.md`，移除個人環境依賴。 |
| `CLAUDE.md` 可否匯入 `AGENTS.md`？ | Anthropic project instructions 支援以 `@path` 匯入其他檔案；薄入口可避免規則重複。 | [Anthropic：Claude Code memory](https://docs.anthropic.com/en/docs/claude-code/memory) | 被匯入內容仍須通過公開審查。 | 保留只含 `@AGENTS.md` 的 `CLAUDE.md`。 |

## 發布 gate

### Blocking

- [ ] 所有程式碼、文件、素材與資料都有公開散布權利。
- [ ] `LICENSE` 與第三方授權義務完整。
- [ ] Working tree、所有公開 refs 與 Git 歷史沒有 secret 或私人資料。
- [ ] 不希望公開的 author／committer identity 不存在於公開歷史。
- [ ] 乾淨 clone 不依賴作者的 `.env`、絕對路徑或 private service。
- [ ] CI 不會把 secrets 提供給不受信任的 fork PR。
- [ ] 公開安全回報管道與 `SECURITY.md` 一致。

### Minimum

- [ ] `LICENSE`、`README.md`、`.gitignore` 與無秘密的 `.env.example`。
- [ ] 明確的 runtime 版本、安裝、啟動、測試與外部帳號說明。
- [ ] Clean-clone smoke test 與核心行為 tests 通過。

### Recommended

- [ ] `CONTRIBUTING.md`、`SECURITY.md`、`CODE_OF_CONDUCT.md`。
- [ ] Issue／PR templates、required CI、Dependabot、secret scanning 與 push protection。
- [ ] Dependency vulnerability audit、license ledger 與固定 SHA 的 Actions。
- [ ] Branch protection、release checklist 與維護者回應安全問題的流程。

## 2026-08-04 研究刷新與 NewsLoop 證據

| question | claim | source | gap | action |
| --- | --- | --- | --- | --- |
| 公開 repository 會公開哪些東西？ | GitHub 的 public repository 對所有人可見；repository 不只包含目前檔案，也包含每個檔案的 revision history。因此 tracked files、可達 branches/tags、commit metadata、Actions logs 與 PR 參照都要當成公開面。 | [GitHub：About repositories](https://docs.github.com/en/repositories/creating-and-managing-repositories/about-repositories)、[GitHub：Setting repository visibility](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility) | GitHub 的可見性設定不會替 maintainer 判斷商業機密或個資。 | 先 inventory tree、所有要公開的 refs、history metadata，再做 owner/legal gate；不要把 private `main` 當成已清理。 |
| `.md`、`AGENTS.md`、`CLAUDE.md` 能不能上傳？ | 副檔名本身沒有公開豁免。這些檔案可以公開，但只能包含 owner 有權發布、可由公開 clone 執行的 repository-local 指示；不能含 secrets、個人路徑、private URL、內部 prompt、真實資料或未授權第三方內容。 | [GitHub：About READMEs / repository files](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)、[AGENTS.md specification](https://agents.md/)、[Anthropic：Claude Code memory](https://docs.anthropic.com/en/docs/claude-code/memory) | 沒有一個檔名規則能證明內容可公開；必須逐檔閱讀。 | 保留 self-contained `AGENTS.md`，`CLAUDE.md` 只用 `@AGENTS.md`，並逐檔掃描 private path、account、prompt 與未授權內容。 |
| `.gitignore` 是否等於安全？ | 不是。`.gitignore` 只告訴 Git 不要把檔案加入 commit；已追蹤檔不會因新增 ignore rule 消失，歷史中的內容也不會被清除。 | [GitHub：Ignoring files](https://docs.github.com/en/get-started/git-basics/ignoring-files)、[GitHub：Removing sensitive data](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository) | ignored files 仍可能留在本機，且人工誤用 `git add -f` 仍可上傳。 | 發布前同時掃 tracked tree、working tree、所有公開 refs 與 Git objects；真實 secret 若曾曝光，先 revoke/rotate，再處理 history。 |
| 個人 email 是否算發布風險？ | 是。GitHub 說明變更 commit email 只影響未來 commit，修改前的 commit 仍會保留舊 email；因此舊 author/committer metadata 也屬公開歷史的一部分。 | [GitHub：Setting your commit email address](https://docs.github.com/en/account-and-profile/how-tos/email-preferences/setting-your-commit-email-address) | 是否要保留歷史需由 owner 決定，重寫會改變 SHA 並影響分支與 PR。 | NewsLoop 的 `ed2765b` 個人 Gmail metadata 是 release blocker；不要用一般 merge PR 假裝已清掉，應採用乾淨 export history 或由 owner 明確決定 history rewrite。 |
| Secret scanner 綠燈是否足夠？ | 不足。GitHub secret scanning 針對已知 credential pattern；它不能替代私人 email、絕對路徑、真實 logs、業務資料與授權檢查。公開 repository 仍應啟用 secret scanning、push protection、Dependabot 與 code scanning。 | [GitHub：Secret scanning](https://docs.github.com/en/code-security/concepts/secret-security/secret-scanning)、[GitHub：Security and analysis settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-security-and-analysis-settings-for-your-repository)、[OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md) | 本 repo 目前仍 private，API 回報 `security_and_analysis=null`，所以不能宣稱這些 GitHub controls 已啟用。 | 保持 private；公開前逐項在 GitHub 設定中啟用並 live verify，再把結果記入 release evidence。 |
| 本機 ignored credential 是否可以丟上去？ | 不可以。當前 workspace 發現 `.env`、`versions/1.3/.env`、`versions/1.3/.streamlit/dashboard_config.json` 含非空 credentials；它們目前未追蹤、被 `.gitignore` 排除，但仍是私人資料。 | [GitHub：Ignoring files](https://docs.github.com/en/get-started/git-basics/ignoring-files)、[GitHub：Removing sensitive data](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)、[OWASP：Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html) | 本次只做 read-only inventory，尚未替 owner 刪除或輪替本機 secrets。 | 絕不 stage／push 這些路徑；公開前由 owner 依實際使用狀態安全輪替，並在乾淨 clone 中只保留 `.env.example`。 |
| MIT 是否涵蓋所有東西？ | MIT 只授權你有權授予的軟體著作權；它不自動涵蓋第三方 code、圖片、字型、音訊、資料集、商標或 provider data。沒有 license 時，GitHub 說明預設著作權仍限制他人重製、散布與修改。 | [GitHub：Licensing a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)、[OSI：MIT License](https://opensource.org/license/mit)、[GitHub：Open source license compliance](https://docs.github.com/en/code-security/concepts/supply-chain-security/open-source-license-compliance) | direct dependency ledger 已有 evidence，但新增素材仍需逐項 provenance／license review。 | 保留 root `LICENSE` 與 direct dependency ledger；任何 copied code、asset、fixture、font 或 dataset 先核對散布權利與 NOTICE 義務。 |

### 本次可重現的查核結果

- `origin/public-release`：70 個 tracked files，`scripts/check_public_release.py` 通過。
- `origin/main`、PR head 與 `public-release` 的目前 tracked tree，人工 pattern scan 沒有找到私人 email、絕對使用者路徑、private key、token prefix 或 Notion ID candidate。
- 遠端可達 history 的 Gitleaks full-history scan：0 findings；但 metadata scan 仍找到 `ed2765b` 的 2 筆私人 Gmail author/committer metadata。
- PR #10 的 GitHub `Gitleaks` 與 `Python 3.12` checks 都通過；這只能證明該 PR 的 CI gate，不代表 `main` 已可公開。
- GitHub API 目前確認 repository 是 `private`、license detector 是 `MIT`、`security_and_analysis` 為 `null`；因此 security controls 尚未被 live-confirmed。

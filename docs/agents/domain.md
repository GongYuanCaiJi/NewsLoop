# Domain 文件

在修改 NewsLoop 的核心流程前，先讀取 repository root 的 `CONTEXT.md`，以及 `docs/adr/` 中與工作範圍相關的決策紀錄（若存在）。缺少 ADR 目錄時直接繼續，不需要為了形式建立空文件。

## 使用一致的 vocabulary

Issue、設計說明、程式碼、測試與文件提到 domain concept 時，使用 `CONTEXT.md` 定義的詞彙，例如 Trading Thesis、Track1、Track2、Grading Outcome 與 Pending。

不要用 glossary 已排除的近義詞替換核心概念。如果實作真的引入新的 domain concept，應同時更新 `CONTEXT.md`；純技術細節不需要加入 glossary。

## 記錄架構決策

跨模組、資料格式、相容性或不可逆的設計決策，可在 `docs/adr/` 新增 ADR。若提案與既有 ADR 衝突，必須在 Issue 或 Pull request 明確指出衝突與重新決策的原因，不可靜默覆蓋。

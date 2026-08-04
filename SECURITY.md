# 安全政策

## 支援範圍

NewsLoop 目前只維護 `main` 的最新版本，不另外承諾舊 snapshot 或 `versions/` 目錄內容的安全修補。

## 私下回報弱點

請勿用公開 issue 揭露 token、使用者資料、可利用步驟或尚未修補的弱點。

公開前，maintainer 必須先在 repository 的 **Security** 頁面啟用並實際確認 **Private Vulnerability Reporting**。啟用後，請選擇 **Report a vulnerability** 建立私密報告。功能尚未啟用或無法確認時，請不要把 repository 改成 public，也不要在公開 issue、discussion 或 pull request 揭露細節。

回報時請包含：

- 受影響的 commit 或版本
- 可重現步驟與最小 proof of concept
- 影響範圍
- 已知緩解方式

Maintainer 會透過該 private report 確認收到、協調修補與揭露時間。安全修補發布前，不保證任何公開時程。

## 安全邊界

NewsLoop 預設只綁定 loopback。若自行改成對外網路介面，必須設定 `NEWS_ALPHA_API_KEY`，並自行負責 TLS、反向代理、存取控制與監控。本專案不應被當成可直接暴露到公網的託管服務。

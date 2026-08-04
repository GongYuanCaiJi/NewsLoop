# NewsLoop

NewsLoop 記錄事件驅動的 Trading Thesis，並用市場資料形成可回顧的 grading feedback loop。

## 術語

**Trading Thesis**:
一筆已記錄的市場想法，包含方向、發生時間，以及可選的交易計畫。
_避免使用_：News、Record、Idea

**Track1**:
以實際 fill 為起點，依 take-profit／stop-loss 與時間限制判定 Trading Thesis outcome 的 grading track。
_避免使用_：Auto Result、Primary Track

**Track2**:
從指定 entry time 起觀察固定 bars 數，依 final close 相對 entry price 的 move threshold 判定 outcome 的 grading track。
_避免使用_：Secondary Track、Track1 fallback

**Grading Outcome**:
Track 對 Trading Thesis 的判定結果，由 outcome label、elapsed bars 與 reason code 共同描述；它不是數值分數。
_避免使用_：Score、Rating

**Pending**:
Track 尚未取得足夠 market bars，因此不能形成 final Grading Outcome 的狀態。
_避免使用_：Unknown、Failed

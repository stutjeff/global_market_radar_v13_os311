# 全球市場雷達 V13 + OS 3.1.1 手機版單檔部署版

這版是給 GitHub 手機網頁上傳用的 V13。

它把主要程式集中在 `main.py`，避免手機版 GitHub 難以上傳資料夾。

## 上傳檔案

請上傳：

```text
main.py
README.md
requirements.txt
```

再手動建立 yml：

```text
.github/workflows/global-market-radar-v13.yml
```

## 會自動建立的狀態檔

程式執行成功後會自動建立：

```text
storage/os31_state.json
storage/tw_margin_history.csv
storage/tpex_margin_history.csv
storage/source_status.json
storage/last_radar_snapshot.json
```

## Secrets

GitHub repo → Settings → Secrets and variables → Actions：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

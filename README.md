# 全球市場雷達 V13.1 + OS 3.1.1 手機版單檔部署版

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


## V13.1 FRED 修正版

這版針對 Fed 流動性雷達加強：

- 先用 GitHub Secret `FRED_API_KEY` 走官方 FRED API。
- 沒有 API key 時，退回 FRED 公開 CSV。
- 再失敗時，嘗試官方 CSV 的 raw proxy 備援。
- yml 加 `timeout-minutes: 15`，避免資料源卡住整個 workflow。
- `FRED_API_KEY` 是選用，不設定也可以跑；設定後 Fed 流動性雷達穩定性會明顯提高。

# 全球市場雷達 V13.4 + OS 3.1.1 手機版單檔部署版

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


## V13.2 Private Credit Stress
新增私募信貸壓力 proxy 雷達。


## V13.3 修正

- 私募信貸 proxy ticker（BKLN/SRLN/BIZD/JAAA/JBBB/BDC 個股）改為輔助資料。
- 這些 proxy 抓不到時，只讓「私募信貸壓力雷達」該項不計分，不再讓 Data Health 主控禁止模式切換。
- 總分仍在主雷達最上方顯示為「市場風險總分」。
- 台股融資若抓不到，仍只作趨勢參考，不應讓整體主控失真。


## V13.4 亞洲槓桿壓力提醒 beta

新增「亞洲槓桿壓力提醒 beta」，只做提醒，不納入總分、不阻止模式切換。

觀察項目：

- 台股融資本地累積與約 12M/200 交易日變化
- 台股接近高檔時的融資升溫提醒
- 台灣違約交割 / margin financing 新聞 proxy
- 韓國 margin debt / leveraged ETF / retail leverage 新聞 proxy

這個模組是泡沫後段提醒器，不是買賣訊號。

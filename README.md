# G2G 遊戲帳號爬蟲與分級

此專案用於抓取 G2G 指定遊戲帳號商品，依價格與內容物進行評分，並分為：

- 初始號
- 中階帳號
- 高階帳號

最後輸出 Excel 檔案，主要欄位包含：`帳號名稱、賣家、價格、分數、類別`。

## 1. 安裝

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## 2. 執行

```bash
python main.py
```

常用參數：

```bash
python main.py --max-pages 3 --max-items 50 --output result.xlsx
python main.py --headful
```

- `--max-pages`：限制抓取頁數，方便快速測試
- `--max-items`：限制抓取商品筆數
- `--headful`：顯示瀏覽器畫面（預設為無頭模式）
- `--output`：輸出 Excel 檔名

## 3. 評分與財務規則

目前整合在 `main.py` 內，可調整：

- `ScoreConfig.price_weight`：價格分權重（預設 0.4）
- `ScoreConfig.content_weight`：內容分權重（預設 0.6）
- `ScoreConfig.keyword_weights`：關鍵字加分表
- `ScoreConfig.score_ranges`：分數對應類別（初始/中階/高階）
- `--usd-to-twd`：美金換算台幣匯率（預設 32）
- `--fee-rate`：手續費比率（預設 0.05）

## 4. 輸出欄位

Excel 會包含：

- 帳號名稱
- 賣家
- 價格(USD)
- 價格(TWD)
- 手續費(TWD)
- 總價(TWD)
- 分數
- 類別
- 商品連結
- 幣別
- 價格分
- 內容分
- 內容摘要
- 抓取時間

## 5. 注意事項

- G2G 頁面可能動態變更，若 selector 失效，請調整 `main.py` 內的 selector 區塊。
- 全分頁抓取時間較長，建議先用 `--max-pages` 驗證。
- 若遇到反爬限制，可提高等待時間或降低抓取頻率（`ScraperConfig` 的 delay 設定）。

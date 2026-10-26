# Apple iPhone 香港官網庫存監控

企業級、Zero-Trust 的香港官網庫存監控系統：GitHub Actions 定時查詢 + GitHub Pages 控制台 + Telegram 智慧聚合通知。

平時沒貨也能用 **測試模式** 把 `mock_status` 在 `unavailable` / `available` 之間切換，完整演練「無貨 → 有貨」通知鏈，不會洗版。

---

## 系統做什麼

1. 依 `config.json` 監控指定 iPhone 型號（香港 SKU，後綴 `ZA/A`）。
2. 同時支援 **門市取貨（Pick-up）** 與 **送貨上門（Delivery）**。
3. 只在狀態從 `unavailable` 變成 `available` 時通知；同一輪多筆有貨會**打包成 1 條訊息**，並依優先級排序。
4. 訊息標註通路，並附上 [直達結帳連結](https://www.apple.com/hk-zh/shop/buy-iphone)。
5. 控制台可開關監控、改測試模式、估 GitHub Actions 用量，並用 GitHub REST API 回寫 `config.json`。

### 架構（Zero-Trust）

| 層 | 位置 | 放什麼 |
| --- | --- | --- |
| 公開設定 | `config.json` | 開關、型號、門市、優先級。**不含任何 token** |
| 執行狀態 | `state.json` | 每個「型號 × 通路」上次庫存，供防洗版 |
| 機密 | GitHub Secrets | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` |
| 控制台 PAT | 瀏覽器 `sessionStorage` | 只在當次分頁有效，關分頁即消失 |

官方路徑 `https://www.apple.com/hk-zh/shop/fulfillment-messages` 目前常回 **HTTP 541** 攔截頁。`checker.py` 仍會先嘗試該端點，失敗後自動改走：

- 取貨：`/hk-zh/shop/retail/pickup-message`
- 送貨：`/hk-zh/shop/delivery-message`

查詢失敗時**不會**把舊狀態改成無貨，避免之後誤報「又有貨了」。

---

## 5 分鐘安裝

### 1. Fork / 使用本倉庫

把本專案放到你的 GitHub 帳號（公開或私人皆可）。私人倉庫的 GitHub Pages 需要付費方案。

### 2. 建立通訊 Secrets

Repo → **Settings → Secrets and variables → Actions**，新增：

| Name | 說明 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | 向 [@BotFather](https://t.me/BotFather) 建立機器人後取得 |
| `TELEGRAM_CHAT_ID` | 對機器人發訊息後，用 `https://api.telegram.org/bot<TOKEN>/getUpdates` 查看 `chat.id` |

控制台「更新 Telegram 憑證」兩個欄位係**可選覆寫**：GitHub 唔會把 Secret 值讀返畀網頁，所以唔會自動填。你已經喺 Settings 加咗就留空；得要換 token 先填。通知只經 Telegram。

### 3. 建立 Fine-grained PAT（給控制台用）

GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens。

最低權限：

- Repository access：只勾這個 repo
- Permissions：
  - **Contents: Read and write**（更新 `config.json`）
  - **Actions: Read and write**（觸發測試通知）
  - **Workflows: Read and write**（由控制台改監控頻率 cron）
  - **Secrets: Read and write**（可選，只在要覆寫通訊憑證時）

PAT **不要** commit 進倉庫。

### 4. 開啟 GitHub Pages 控制台

Repo → Settings → Pages → Build and deployment：

- Source: **Deploy from a branch**
- Branch: `main` / folder: `/ (root)`

數分鐘後開啟：

`https://<你的帳號>.github.io/<repo>/`

本地預覽：

```bash
python3 -m http.server 8080
# 瀏覽器打開 http://127.0.0.1:8080
```

### 5. 在控制台完成設定

1. 填 Owner / Repo / PAT。
2. 選擇型號、顏色、容量，按「加入監控」。
3. 多選門市，調整優先級。
4. **先保持測試模式 ON、總開關可先 OFF**。
5. 按 **儲存設定**。
6. 要用真的監控時：關閉測試模式、打開總開關、再到 Actions 確認 `iPhone HK Monitor` workflow 為 Enable。

---

## 控制台功能對照

| 元件 | 寫入欄位 | 作用 |
| --- | --- | --- |
| 總開關 | `enabled` | `false` 時 checker 印提示並 `exit(0)` |
| 測試模式 | `test_mode` / `mock_status` | 不打 Apple API，直接模擬有貨／無貨 |
| 取貨模式 | `fulfillment_modes` | `pickup`、`delivery` 可並存 |
| 型號下拉 | `models[]` | 香港現行 iPhone 18 Pro / Max、17、Air、17e、Duo |
| 門市多選 | `stores` + `priority.stores` | 六間香港 Apple Store |
| 優先級 | `priority` | 門市權重 ×10000 + 容量 ×100 + 通路 |
| 監控頻率 | `check_interval_minutes` + `monitor.yml` cron | 儲存後改 Actions 排程；用量面板只係預估 |
| 測試通知 | `workflow_dispatch` | 觸發 Actions 發送示範訊息 |
| 儲存設定 | GitHub Contents API | 更新 `config.json`。Telegram 欄位**留空＝唔改 Secrets**（值讀唔返） |

---

## 測試模式實戰（強烈建議先做完再盯真貨）

目標：證明「無貨 → 有貨」會通知一次，再查一次不會洗版。

### A. 本地（最快）

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

1. 確認 `config.json`：

```json
{
  "enabled": true,
  "test_mode": true,
  "mock_status": "unavailable"
}
```

2. 跑第一次（建立「無貨」狀態，不應通知）：

```bash
python checker.py --no-delay
```

預期：`沒有「無貨 → 有貨」的新項目`。

3. 把 `mock_status` 改成 `"available"`，再跑：

```bash
python checker.py --no-delay
```

預期：印出 **1 條**綜合訊息，門市與送貨都在裡面，並依優先級排序。

若已設定環境變數，就會真的發到 Telegram：

```bash
export TELEGRAM_BOT_TOKEN="…"
export TELEGRAM_CHAT_ID="…"
python checker.py --no-delay
```

4. **不要改設定**再跑一次：不應再通知（防洗版）。
5. 改回 `"unavailable"` 跑一次，再改 `"available"`：應再次通知。
6. 只測通道、不改狀態機：

```bash
python checker.py --test-notify
```

7. 邏輯自我測試（不連網）：

```bash
python checker.py --self-test
```

8. 驗證總開關：

```bash
# 把 enabled 設為 false
python checker.py
# 應印出「監控總開關為 OFF」並以狀態碼 0 結束
```

### B. 雲端（控制台 + Actions）

1. 控制台：測試模式 ON、`mock_status = 無貨`、總開關 ON → 儲存。
2. Actions → `iPhone HK Monitor` → **Run workflow**。
3. 看 `state.json`：各組合應為 `unavailable`，不該收到通知。
4. 控制台把模擬狀態改成 **有貨** → 儲存。
5. 再 Run workflow：應收到 **1 條**聚合通知，含直達結帳連結。
6. 再 Run 一次：不該再收到。
7. 按控制台 **測試通知**：觸發 `test_notify`，不影響真實狀態機。
8. 確認無誤後，關閉測試模式，才開始打官網。

---

## 用量與 2,000 分鐘額度（必讀）

workflow 預設 `*/2 * * * *`（每 2 分鐘）。

GitHub Actions **以整分鐘進位**。即便 `enabled=false`，cron 仍會 checkout，通常仍計 **1 分鐘／次**。

粗估（30 天）：

| 頻率 | 時間窗 | 每月分鐘數 | 對比 2,000 |
| --- | --- | --- | --- |
| 每 2 分鐘 | 24 小時 | ≈ 21,600 | 嚴重超標 |
| 每 2 分鐘 | 10:00–22:00 | ≈ 10,800 | 超標 |
| 每 10 分鐘 | 10:00–22:00 | ≈ 2,160 | 邊緣 |
| 每 15 分鐘 | 10:00–22:00 | ≈ 1,440 | 通常可接受 |

**建議**

- 平時到 Actions 頁 **Disable** `iPhone HK Monitor`，開售當日再 Enable。
- 總開關只適合「開售日臨時暫停」，不能省 cron 本身的分鐘。
- 要把頻率改成 10 分鐘：同時改 `.github/workflows/monitor.yml` 的 cron（例如 `*/10 * * * *`）與控制台「監控頻率」。
- 控制台用量面板會依頻率與營業時間即時估算，並連結 [GitHub Billing](https://github.com/settings/billing)。

---

## 搶機避坑指南

1. **提前登入 Apple ID**  
   用要結帳的帳號登入 [apple.com/hk-zh](https://www.apple.com/hk-zh/)，先通過雙重認證。通知一來，不要再花時間登入。

2. **先走完結帳頁到付款前一步**  
   開售前用同型號走一次流程到「確認付款」前（不要真的下單），熟悉地址、取貨店、支付方式。

3. **點通知裡的直達結帳，不要從首頁再選一次**  
   連結是 `https://www.apple.com/hk-zh/shop/product/<SKU>`，會進對應顏色／容量頁。

4. **5G / 穩定網路，必要時切換**  
   開售瞬間 Wi-Fi 可能擠爆。準備好手機 5G 熱點，結帳失敗就切網路再送一次，不要狂連點。

5. **排隊時切勿重新整理**  
   進入 waiting room / 結帳佇列後，重新整理通常會掉到隊尾或被踢出。等到逾時再重進。

6. **門市取貨帶齊證件**  
   取貨要政府發出的附相片證件，且必須本人。通知寫「今日可取」仍要等官方取貨電郵／Wallet pass。

7. **限購與風控**  
   官網對新品常限購。多帳號、自動化下單可能被風控。本系統只做**庫存提醒**，下單仍由你本人完成。

8. **官方營業時間**  
   香港 Apple Store 多為 10:00–21:00 / 22:00。時間窗外取貨欄位常是無貨，控制台預設過濾此外時段。

9. **541 / 暫時查不到**  
   同一出口 IP 短時間打太多次會被攔。程式已加 3–7 秒隨機延遲與指數退避。若持續失敗，拉長 cron 間隔。

10. **通知不是下單成功**  
    有貨窗口可能只有數十秒。看到訊息立刻點連結，不要回覆機器人。

---

## 檔案一覽

```
.
├── index.html                 # Tailwind 控制台（GitHub Pages）
├── checker.py                 # 監控、狀態機、聚合通知
├── config.json                # 公開設定（enabled / test_mode / 優先級）
├── catalog.json               # 香港門市與現行 iPhone SKU
├── state.json                 # 防洗版狀態（Actions 會回寫）
├── requirements.txt
├── .github/workflows/monitor.yml
└── README.md
```

### checker.py 常用參數

```bash
python checker.py                 # 依 config 執行
python checker.py --test-notify   # 只發測試訊息
python checker.py --ignore-enabled
python checker.py --ignore-hours
python checker.py --no-delay
python checker.py --self-test
python checker.py -v
```

---

## 香港 Apple Store（2026）

| 編號 | 門市 |
| --- | --- |
| R428 | 中環 ifc mall |
| R409 | 銅鑼灣 Causeway Bay |
| R499 | 尖沙咀 廣東道 |
| R485 | 九龍塘 又一城 |
| R673 | 觀塘 apm |
| R610 | 沙田 新城市廣場 |

SKU 若隨官網改版，更新 `catalog.json` 即可，控制台會自動讀取。

---

## 疑難排解

| 現象 | 處理 |
| --- | --- |
| 控制台儲存失敗 `Bad credentials` | PAT 過期或權限不足 |
| 測試通知 404 | workflow 檔名必須是 `.github/workflows/monitor.yml`，且 PAT 有 Actions 寫入 |
| 有貨但沒通知 | 看 `state.json`：若上次已是 `available`，屬防洗版。先用測試模式把狀態打成無貨再測 |
| 永遠 unknown | Apple 541／網路失敗。拉長間隔，或用 `--verbose` 看 log |
| Actions 分鐘燒太快 | Disable workflow；不要 24 小時每 2 分鐘空轉 |
| Telegram 沒收到 | Secret 名稱是否 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`，以及有冇同 bot 講過話 |

---

## 授權與使用界線

本專案僅供你個人掌握公開庫存資訊。請遵守 Apple 網站條款與 GitHub 使用條款，不要用於大量併發打爆官網或自動下單。

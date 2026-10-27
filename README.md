# Apple iPhone 香港官網庫存監控

企業級、Zero-Trust 的香港官網庫存監控系統：GitHub Actions 定時查詢 + GitHub Pages 控制台 + Telegram 智慧聚合通知。

控制台分成兩塊：**模擬模式**（每格自己揀有貨／冇貨，儲存時只有冇貨→有貨先發測試 Telegram）同 **真實模式**（打開之後先問 Apple 官網，只盯 iPhone Duo）。

---

## 系統做什麼

1. 依 `config.json` 只監測 **iPhone Duo**（香港 SKU，後綴 `ZA/A`）。
2. 同時支援 **門市取貨（Pick-up）** 與 **送貨上門（Delivery）**。
3. 只在狀態從冇貨變成有貨時通知；同一輪多筆有貨會**打包成 1 條訊息**，並依優先級排序。
4. 訊息標註通路，並附上 [直達結帳連結](https://www.apple.com/hk-zh/shop/buy-iphone)。
5. 控制台可開關真實模式、改模擬貨源、估 GitHub Actions 用量，並用 GitHub REST API 回寫 `config.json`。

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
  - **Actions: Read and write**（觸發連線測試與儲存模擬狀態）
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
4. 模擬模式：每格揀有貨／冇貨，撳 **儲存模擬狀態**（只有冇貨→有貨先收到測試 Telegram）。
5. 真實模式：打開開關，按 **儲存設定**，再到 Actions 確認 `iPhone HK Monitor` workflow 為 Enable。

---

## 控制台功能對照

| 元件 | 寫入欄位 | 作用 |
| --- | --- | --- |
| 真實模式 | `enabled` | `false` 時不查 Apple。打開後只盯 Duo |
| 幾耐之內先當有貨 | `soon_window_days` | 真實模式左邊下拉，預設 7 日。送貨／取貨都係：呢個日數內可送或可取先當有貨，其他當冇貨。要撳「儲存設定」先生效 |
| 模擬貨源 | `simulation.statuses` | 每格獨立有貨／冇貨。儲存時只有冇貨→有貨發測試訊息 |
| 取貨模式 | `fulfillment_modes` | `pickup`、`delivery` 可並存 |
| 型號下拉 | `models[]` | 只保留 iPhone Duo |
| 門市多選 | `stores` + `priority.stores` | 六間香港 Apple Store |
| 優先級 | `priority` | 門市權重 ×10000 + 容量 ×100 + 通路 |
| 監控頻率 | `check_interval_minutes` + `monitor.yml` cron | 儲存後改排程；真實模式貨源板會自動刷新 |
| 最近一次官網貨源 | `state.json` `items` | 上次問 Apple 時，每部 Duo × 門市／送貨嘅有貨情況 |
| 測試 Telegram 連線 | `workflow_dispatch` `test_notify` | 只發連線短訊，**唔會**扮 iPhone 有貨 |
| 儲存模擬狀態 | `workflow_dispatch` `apply_simulation` | 套用每格模擬貨源，有補貨先發測試 Telegram |
| 儲存設定 | GitHub Contents API | 更新真實模式開關、頻率、監測清單 |
| 用量提醒 | `state.json` `usage` | 每次排程約 +1 分鐘；到 1,800 同 2,000 發 Telegram |

---

## 用量常見問題

1. **盯多幾部機、幾間門市，15 分鐘一轉，分鐘數會唔會多啲？**  
   基本上唔會。GitHub 計嘅係「鬧鐘響咗幾多次」，每次大約 1 分鐘，唔係「查咗幾多款」。多幾部機只係同一轉入面多問幾次官網，多數仍然係 1 分鐘。

2. **開啟之後要唔要開住瀏覽器？**  
   唔需要。鬧鐘喺 GitHub 伺服器行，有貨會直接 Telegram 你。控制台只係改設定用。

3. **超過 2,000 分鐘會唔會自動停？**  
   唔會優雅停。關「真實模式」都停唔到鬧鐘。要真正停燒分鐘：Actions → `iPhone HK Monitor` → Disable workflow。私人倉庫超額之後 GitHub 可能突然跑唔到，或者開始收費。本系統會喺估計用到 **1,800** 同 **2,000** 分鐘各發一次 Telegram 提醒。公開倉庫通常唔計呢個 2,000 額度。真正已用量只喺 [GitHub Billing](https://github.com/settings/billing)。

---

## 模擬模式實戰（建議先做完再打開真實模式）

模擬模式同真實模式分開。模擬唔會問 Apple，亦唔會改真實貨源紀錄。

### A. 控制台（建議）

1. 填 PAT。可用「測試 Telegram 連線」確認 bot 收得到短訊。
2. 模擬模式表格：每格用下拉選 **冇貨** 或 **有貨**。
3. 撳 **儲存模擬狀態**。
   - 全部仍然冇貨／已經有貨：唔會收到 Telegram。
   - 任何一格由冇貨變有貨：收到 **🧪 模擬模式測試結果**，列明邊部機補貨。
4. 再儲存一次（唔改）：唔應該再收到。

### B. 本地

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python checker.py --self-test
python checker.py --test-notify
python checker.py --apply-simulation
python checker.py   # enabled=false 時應印出「真實模式為 OFF」
```

真實模式：控制台打開開關 → **儲存設定** → 系統即刻查一次 Apple，之後按你設嘅頻率更新「最近一次官網貨源」。任何 Duo 由冇貨變有貨就發 🍎 通知。

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
├── usage.py                   # Actions 分鐘累計；1800／2000 Telegram 提醒
├── config.json                # 公開設定（真實模式開關 / 模擬貨源 / 優先級）
├── catalog.json               # 香港門市與 iPhone Duo SKU
├── state.json                 # 防洗版狀態（Actions 會回寫）
├── requirements.txt
├── .github/workflows/monitor.yml
└── README.md
```

### checker.py 常用參數

```bash
python checker.py                 # 依 config 執行
python checker.py --test-notify         # 只發 Telegram 連線測試短訊
python checker.py --apply-simulation    # 套用模擬貨源：冇貨→有貨先發測試結果
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
| 連線測試／儲存模擬 404 | workflow 檔名必須是 `.github/workflows/monitor.yml`，且 PAT 有 Actions 寫入 |
| 有貨但沒通知 | 看 `state.json`：若上次已是 `available`，屬防洗版。真實同模擬係兩套紀錄 |
| 永遠 unknown | Apple 541／網路失敗。拉長間隔，或用 `--verbose` 看 log |
| Actions 分鐘燒太快 | Disable workflow；不要 24 小時每 2 分鐘空轉 |
| Telegram 沒收到 | Secret 名稱是否 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`，以及有冇同 bot 講過話 |

---

## 授權與使用界線

本專案僅供你個人掌握公開庫存資訊。請遵守 Apple 網站條款與 GitHub 使用條款，不要用於大量併發打爆官網或自動下單。

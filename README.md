# Apple iPhone 香港官網庫存監控

企業級、Zero-Trust 的香港官網庫存監控系統：GitHub Actions 定時查詢 + GitHub Pages 控制台 + Telegram 智慧聚合通知。

控制台分成兩塊：**模擬模式**（預設收起；展開後每格自己揀有貨／冇貨，儲存時只有冇貨→有貨先發測試 Telegram）同 **真實模式**（打開之後先問 Apple 官網，監測清單上嘅型號）。

---

## 系統做什麼

1. 依 `config.json` 監測清單上嘅型號。已內建 **iPhone Duo**（香港 SKU，後綴 `ZA/A`）；日後新品可喺控制台貼官網 SKU／門市 code，唔使改程式。
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
  - Account permissions **Plan: Read**（可選，但 GitHub Billing usage API 官方話唔支援 Fine-grained PAT，多數仍然讀唔到帳戶總用量）

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
2. 選擇型號、顏色、容量，按「加入監控」。新品未入內建清單時，喺「加自訂型號」貼官網 SKU（例如 `MK2J4ZA/A`）。
3. 多選門市，調整優先級。新店未入內建清單時，喺「加自訂門市」貼店號（例如 `R428`）。下面「預覽監測 code」可以即時打開產品頁、取貨 JSON、送貨 JSON。
4. 模擬模式：每格揀有貨／冇貨，撳 **儲存模擬狀態**（只有冇貨→有貨先收到測試 Telegram）。
5. 真實模式：打開開關，按 **儲存設定**，再到 Actions 確認 `iPhone HK Monitor` workflow 為 Enable。

---

## 控制台功能對照

| 元件 | 寫入欄位 | 作用 |
| --- | --- | --- |
| 真實模式 | `enabled` | `false` 時不查 Apple。打開後查監測清單上嘅型號 |
| 幾耐之內先當有貨 | `soon_window_days` | 真實模式左邊下拉，預設 7 日。送貨／取貨都係：呢個日數內可送或可取先當有貨，其他當冇貨。要撳「儲存設定」先生效 |
| 模擬貨源 | `simulation.statuses` | 每格獨立有貨／冇貨。儲存時只有冇貨→有貨發測試訊息 |
| 取貨模式 | `fulfillment_modes` | `pickup`、`delivery` 可並存 |
| 型號下拉 | `models[]` | 內建 iPhone Duo；亦可加任意官網 SKU |
| 自訂型號／門市 | `models[]` + `custom_stores` + `stores` | 控制台貼 SKU 或 `R` 店號；下面有官網預覽連結 |
| 門市多選 | `stores` + `priority.stores` | 六間香港 Apple Store，可再加店號 |
| 優先級 | `priority` | 門市權重 ×10000 + 容量 ×100 + 通路 |
| 監控頻率 | `check_interval_minutes` | 儲存後由 job 內循環按呢個間隔查；真實模式貨源板會自動刷新 |
| 每日監控時段 | `business_hours` + watchdog cron 小時欄 | 你自己定香港時間幾點開始、幾點完。Watchdog 只喺呢段時間拉起 loop |
| 查貨紀錄 | `state.json` `loop` | 每次查貨寫入時間；控制台只顯示最近 5 轉，連續次數會一直累加 |
| 最近一次官網貨源 | `state.json` `items` | 上次問 Apple 時，每部監測機 × 門市／送貨嘅有貨情況 |
| 測試 Telegram 連線 | `workflow_dispatch` `test_notify` | 只發連線短訊，**唔會**扮 iPhone 有貨 |
| 儲存模擬狀態 | `workflow_dispatch` `apply_simulation` | 套用每格模擬貨源，有補貨先發測試 Telegram |
| 儲存設定 | GitHub Contents API | 更新真實模式開關、頻率、監測清單 |
| 本月 GitHub 用量 | GitHub Actions API | 控制台左邊顯示呢個監控倉本月真實跑咗幾多次。帳戶 Billing 總用量 Fine-grained PAT 通常讀唔到 |
| 用量提醒 | `state.json` `usage` | 每次排程約 +1 分鐘；到 1,800 同 2,000 發 Telegram |

---

## 用量常見問題

1. **盯多幾部機、幾間門市，分鐘數會唔會多啲？**  
   基本上唔會因為多部機。公開倉標準 runner 通常免費。私人倉而家係一條長駐 loop，runner 開住幾耐就計幾多分鐘，唔係「查咗幾多款」。

2. **開啟之後要唔要開住瀏覽器？**  
   唔需要。鬧鐘喺 GitHub 伺服器行，有貨會直接 Telegram 你。控制台只係改設定用。

3. **超過 2,000 分鐘會唔會自動停？**  
   唔會優雅停。關「真實模式」都停唔到鬧鐘。要真正停燒分鐘：Actions → `iPhone HK Monitor` → Disable workflow。私人倉庫超額之後 GitHub 可能突然跑唔到，或者開始收費。本系統會喺估計用到 **1,800** 同 **2,000** 分鐘各發一次 Telegram 提醒。

4. **公開倉係咪即使 1 分鐘查一次都唔計 2,000 分鐘？**  
   **標準 GitHub-hosted runner（ubuntu-latest）喺公開倉係免費嘅**，唔會扣私人倉嗰 2,000 分鐘。太密仍然會打爆 Apple（541）。如果之後改成私人倉，一條長駐 loop 可以一醒就用幾百分鐘，千祈唔好喺私人倉開住 1 分鐘 loop。

5. **點解 set 咗 1 分鐘，控制台仲停喺十幾分鐘前？**  
   舊做法靠 GitHub `schedule` 鬧鐘。個鬧鐘官方最短約 5 分鐘，繁忙時會遲、甚至漏。而家改成 **job 入面按你設嘅間隔循環查**；鬧鐘只負責 loop 死咗就再拉起。真實模式 ON 並儲存之後，1 分鐘就會約 1 分鐘查一次。

6. **點樣先知道真係每一分鐘執行一次？**  
   真實模式 ON 並儲存之後：
   - 控制台「查貨紀錄」會列出最近 5 轉查官網嘅香港時間，同「距上一轉約 X 分鐘」。連續次數會一直累加。填咗 PAT 會每 15 秒直接讀 GitHub，唔使等 Pages 刷新。
   - 兩筆相差應約等於你設嘅間隔（1 分鐘就大約 1；查官網同 commit 可能變成 1–2 分鐘）。疏過設定 +1 分鐘會標 ⚠️。
   - GitHub → Actions → `iPhone HK Monitor` → 最新 **In progress** run，log 會每轉印 `heartbeat … next check in 1 minute(s)`。
   - `state.json` 嘅 `loop.ticks` / `loop.tick_count` 都係同一份紀錄。

7. **Loop 死咗會唔會 Telegram 我？**  
   會。Job 失敗／被取消會即刻發「⚠️ 監測 loop 中斷」。如果 runner 無聲消失、心跳過期，5 分鐘內 watchdog 再拉起時都會再通知一次。正常跑滿約 5.5 小時會**即刻再開下一條 job**（唔使等鬧鐘），或者你關掉真實模式，**唔會**當死機嚟煩你。鬧鐘只係後備。

8. **公開倉建議幾耐查一次？**  
   開售用 1–3 分鐘；日常用 5 分鐘穩陣啲。想少打官網用 10 或 15 分鐘。

9. **想比朋友一齊用？**  

   - **只係想朋友都收到有貨 Telegram（最簡單）**：開一個 Telegram 群組，加朋友，再加你個 bot 入去。用群組嘅 Chat ID（多數係負數）填 `TELEGRAM_CHAT_ID`。兩個人收同一套通知，朋友唔使碰 GitHub。  
   - **朋友想自己監第二款／另一套門市**：叫佢 fork 呢個 repo，自己開 GitHub Pages、自己整 Fine-grained PAT、自己喺 Secrets 填 Telegram。唔好把你嘅 PAT／bot token 傳畀人。  
   - **朋友想一齊改你呢個控制台**：Repo → Settings → Collaborators 加佢。佢用自己嘅 PAT（要有呢個倉嘅寫入權）。大家共用一份 `config.json`，後儲存嘅會蓋過先儲存嘅。


---

## 模擬模式實戰（建議先做完再打開真實模式）

模擬模式同真實模式分開。模擬唔會問 Apple，亦唔會改真實貨源紀錄。

### A. 控制台（建議）

1. 填 PAT。可用「測試 Telegram 連線」確認 bot 收得到短訊。
2. 展開模擬模式。表格：每格用下拉選 **冇貨** 或 **有貨**。
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

真實模式：控制台打開開關 → **儲存設定** → 系統即刻查一次 Apple，之後按你設嘅頻率更新「最近一次官網貨源」。監測清單上任何型號由冇貨變有貨就發 🍎 通知。

---

## 日後新品／新門市點加

唔使改 `catalog.json` 或重新部署程式。控制台「監測邊款 iPhone」同「門市多選」下面有自訂欄：

1. **型號監測 code**：去 [香港官網買 iPhone](https://www.apple.com/hk-zh/shop/buy-iphone) 揀顏色容量，產品頁網址尾段就係 SKU，例如 `https://www.apple.com/hk-zh/shop/product/MK2J4ZA/A` 嘅 `MK2J4ZA/A`。貼入「SKU／監測 code」，可選填顯示名稱，撳「加入呢個 SKU」，再 **儲存設定**。
2. **門市監測 code**：撳控制台「預覽監測 code」嘅取貨 JSON，入面每間店有 `storeNumber`（香港而家係 `R428`、`R409` 呢類）。貼入「門市 code」，可選填名稱，撳「加入呢間門市」，再 **儲存設定**。
3. 下面四條連結會跟住你喺 SKU 欄打嘅字更新，方便核對產品頁、取貨 JSON、送貨 JSON。

`checker.py` 唔再略過非 Duo 型號；自訂 SKU 同店號會寫入 `config.json` 嘅 `models`／`stores`／`custom_stores`。

---

## 用量與 2,000 分鐘額度（必讀）

監測 loop 按控制台「幾耐查一次」喺 runner 入面 sleep。單條 job 約 5.5 小時換班後會即刻自己開下一條；GitHub `schedule` 只係每 5 分鐘後備 watchdog（唔好再把 cron 改成 1 分鐘）。

GitHub Actions **以整分鐘進位**。**公開倉**用標準 runner 通常唔計入 2,000。**私人倉**即使 `enabled=false`，只要 cron 響就會 checkout，通常仍計 **1 分鐘／次**。控制台「每日監控時段」儲存後會改 cron 小時欄，出咗時段鬧鐘唔響。

粗估（30 天）：

| 頻率 | 時間窗 | 每月分鐘數 | 對比 2,000 |
| --- | --- | --- | --- |
| 每 2 分鐘 | 24 小時 | ≈ 21,600 | 嚴重超標 |
| 每 2 分鐘 | 10:00–22:00 | ≈ 10,800 | 超標 |
| 每 10 分鐘 | 10:00–22:00 | ≈ 2,160 | 邊緣 |
| 每 15 分鐘 | 10:00–22:00 | ≈ 1,440 | 通常可接受 |

**建議**

- 平時到 Actions 頁 **Disable** `iPhone HK Monitor`，開售當日再 Enable。
- 總開關只適合「開售日臨時暫停」。要省私人倉分鐘，改「每日監控時段」或到 Actions 頁 Disable workflow。
- 頻率同每日時段：喺控制台改完撳「儲存設定」，會一齊改 `.github/workflows/monitor.yml` 的 cron。
- 控制台用量面板會讀呢個監控倉本月 Actions 真實跑次（唔再係「跑滿 30 日」預估）。

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

8. **每日監控時段**  
   控制台可以自己定香港時間幾點開始、幾點完。預設 10:00–22:00。儲存後鬧鐘只喺呢段時間響。

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
| Actions 分鐘燒太快 | Disable workflow；私人倉唔好開住 1 分鐘長駐 loop |
| 查貨紀錄一直空白 | 真實模式未 ON／未儲存，或 PAT 未填。Actions 確認有 In progress 嘅 `iPhone HK Monitor` |
| 收到「監測 loop 中斷」 | GitHub 會自動再拉起。去 Actions 睇失敗原因；Secret 唔齊就唔會收到呢條 |
| Telegram 沒收到 | Secret 名稱是否 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`，以及有冇同 bot 講過話 |

---

## 授權與使用界線

本專案僅供你個人掌握公開庫存資訊。請遵守 Apple 網站條款與 GitHub 使用條款，不要用於大量併發打爆官網或自動下單。

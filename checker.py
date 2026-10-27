#!/usr/bin/env python3
"""
Apple 香港官網 iPhone 庫存監控器。

設計重點
--------
- Zero-Trust：Telegram 憑證只從環境變數（GitHub Secrets）讀取，
  絕不寫入 config.json。
- 真實模式：config.enabled == false 時安全退出（exit 0），不打 Apple API。
  開啟後只監測 iPhone Duo；冇貨 → 有貨先發 Telegram。
- 模擬模式：--apply-simulation 讀每格獨立貨源，只寫入 simulation 狀態，
  唔問 Apple；任意一格冇貨 → 有貨先發測試結果 Telegram。
- 雙模式：店內取貨（pickup）與送貨上門（delivery）。
- 防洗版：僅在 unavailable → available 時納入通知；同週期多筆打包成 1 條。
- 優先級：依 config.priority 的門市 / 容量 / 取貨模式權重排序。
- 防干擾：隨機延遲、指數退避重試、營業時間窗。

Apple 香港目前會對 /shop/fulfillment-messages 回 541 攔截頁。
本程式仍會先嘗試該官方路徑（規格要求），失敗後自動改走
/shop/retail/pickup-message 與 /shop/delivery-message，避免把攔截誤判成無貨。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:  # pragma: no cover - 執行環境缺依賴時給明確提示
    print("缺少 requests，請先執行: pip install -r requirements.txt", file=sys.stderr)
    raise

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
CATALOG_PATH = ROOT / "catalog.json"

APPLE_ORIGIN = "https://www.apple.com"
FULFILLMENT_URL = f"{APPLE_ORIGIN}/hk-zh/shop/fulfillment-messages"
PICKUP_URL = f"{APPLE_ORIGIN}/hk-zh/shop/retail/pickup-message"
DELIVERY_URL = f"{APPLE_ORIGIN}/hk-zh/shop/delivery-message"
CHECKOUT_URL = f"{APPLE_ORIGIN}/hk-zh/shop/product/"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)
REQUEST_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "zh-HK,zh-TW;q=0.9,en;q=0.8",
    "User-Agent": USER_AGENT,
    "Referer": "https://www.apple.com/hk-zh/shop/buy-iphone",
}

HK_TZ = ZoneInfo("Asia/Hong_Kong")
FREE_MINUTES = 2000
DUO_FAMILY = "iphone-duo"
STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNKNOWN = "unknown"

LOGGER = logging.getLogger("iphone_monitor")


# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------
@dataclass
class WatchItem:
    part_number: str
    family: str
    name: str
    color: str
    capacity: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WatchItem":
        return cls(
            part_number=str(raw.get("part_number") or raw.get("part") or "").strip(),
            family=str(raw.get("family") or ""),
            name=str(raw.get("name") or raw.get("part_number") or ""),
            color=str(raw.get("color") or ""),
            capacity=str(raw.get("capacity") or "").lower(),
        )


@dataclass
class AvailabilityHit:
    """單一「型號 × 通路」查詢結果。"""

    key: str
    mode: str  # pickup | delivery
    part_number: str
    name: str
    capacity: str
    store_id: str
    store_name: str
    status: str
    quote: str
    checkout_url: str
    score: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def now_hk() -> datetime:
    return datetime.now(tz=HK_TZ)


def iso_now() -> str:
    return now_hk().isoformat(timespec="seconds")


def load_json(path: Path, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        if fallback is not None:
            return json.loads(json.dumps(fallback))
        raise FileNotFoundError(f"找不到檔案：{path}")
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 必須是 JSON object")
    return data


def save_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(path)


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def checkout_url(part_number: str) -> str:
    """一鍵直達官網對應型號購買頁（會 301 到完整 PDP）。"""
    return CHECKOUT_URL + urllib.parse.quote(part_number, safe="/")


def item_key(mode: str, store_id: str, part_number: str) -> str:
    return f"{mode}:{store_id}:{part_number}"


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def due_for_interval(cfg: dict[str, Any], state: dict[str, Any], moment: datetime | None = None) -> bool:
    """距離上次成功查詢未滿 check_interval_minutes 則略過（cron 未改到時的保險）。"""
    try:
        interval = max(1, int(cfg.get("check_interval_minutes") or 2))
    except (TypeError, ValueError):
        interval = 2
    last = state.get("last_run") or {}
    if last.get("status") != "ok":
        return True
    raw = last.get("at")
    if not raw:
        return True
    try:
        prev = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=HK_TZ)
    moment = moment or now_hk()
    elapsed = (moment - prev).total_seconds()
    return elapsed >= (interval - 0.2) * 60


def within_business_hours(cfg: dict[str, Any], moment: datetime | None = None) -> bool:
    hours = cfg.get("business_hours") or {}
    if not hours.get("enabled", False):
        return True
    tz_name = hours.get("timezone") or "Asia/Hong_Kong"
    moment = moment or datetime.now(tz=ZoneInfo(tz_name))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(tz_name))
    else:
        moment = moment.astimezone(ZoneInfo(tz_name))
    days = hours.get("days")
    if isinstance(days, list) and days and moment.weekday() not in set(int(d) for d in days):
        # Python weekday: Mon=0 … Sun=6。config 使用同一慣例。
        return False
    start_h, start_m = parse_hhmm(str(hours.get("start") or "00:00"))
    end_h, end_m = parse_hhmm(str(hours.get("end") or "23:59"))
    start_min = start_h * 60 + start_m
    end_min = end_h * 60 + end_m
    now_min = moment.hour * 60 + moment.minute
    if start_min <= end_min:
        return start_min <= now_min < end_min
    # 跨日（例如 22:00–02:00）
    return now_min >= start_min or now_min < end_min


def priority_score(cfg: dict[str, Any], hit: AvailabilityHit) -> int:
    prio = cfg.get("priority") or {}
    store_w = int((prio.get("stores") or {}).get(hit.store_id, 0))
    cap_w = int((prio.get("capacities") or {}).get(hit.capacity, 0))
    mode_w = int((prio.get("modes") or {}).get(hit.mode, 0))
    # 門市權重為主，其次容量，再次取貨模式。
    return store_w * 10_000 + cap_w * 100 + mode_w


def catalog_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["part"]: p for p in catalog.get("products") or [] if p.get("part")}


def store_label(catalog: dict[str, Any], store_id: str) -> str:
    for store in catalog.get("stores") or []:
        if store.get("id") == store_id:
            return str(store.get("name_zh") or store.get("name") or store_id)
    if store_id == "DELIVERY":
        return "送貨上門（香港）"
    return store_id


def resolve_watch_list(cfg: dict[str, Any], catalog: dict[str, Any]) -> list[WatchItem]:
    products = catalog_index(catalog)
    items: list[WatchItem] = []
    seen: set[str] = set()
    for raw in cfg.get("models") or []:
        item = WatchItem.from_dict(raw)
        if not item.part_number or item.part_number in seen:
            continue
        meta = products.get(item.part_number, {})
        if not item.name:
            item.name = str(meta.get("name") or item.part_number)
        if not item.capacity:
            item.capacity = str(meta.get("capacity") or "")
        if not item.family:
            item.family = str(meta.get("family") or "")
        if not item.color:
            item.color = str(meta.get("color") or "")
        family = item.family or str(meta.get("family") or "")
        if family != DUO_FAMILY:
            LOGGER.info("略過非 iPhone Duo 型號：%s", item.part_number)
            continue
        item.family = DUO_FAMILY
        seen.add(item.part_number)
        items.append(item)
    return items


def selected_modes(cfg: dict[str, Any]) -> list[str]:
    modes = [str(m).lower() for m in (cfg.get("fulfillment_modes") or [])]
    valid = [m for m in modes if m in {"pickup", "delivery"}]
    if not valid:
        valid = ["pickup"]
    # delivery.enabled 可單獨關掉送貨
    delivery_cfg = cfg.get("delivery") or {}
    if "delivery" in valid and delivery_cfg.get("enabled") is False:
        valid = [m for m in valid if m != "delivery"]
    return valid


# ---------------------------------------------------------------------------
# Apple API（重試 + 後備端點）
# ---------------------------------------------------------------------------
class AppleClient:
    def __init__(self, cfg: dict[str, Any], session: requests.Session | None = None) -> None:
        anti = cfg.get("anti_abuse") or {}
        self.max_retries = int(anti.get("max_retries") or 4)
        self.backoff_base = float(anti.get("backoff_base_seconds") or 2)
        self.timeout = float(anti.get("request_timeout_seconds") or 20)
        self.session = session or requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        # 香港目前對 fulfillment-messages 固定回 541。同一輪記住後就改走後備端點，
        # 避免指數退避把 GitHub Actions 分鐘與官網頻率額度一起燒光。
        self._dead_hosts: set[str] = set()

    def _sleep_backoff(self, attempt: int) -> None:
        # 指數退避 + 小幅 jitter，降低被同一出口 IP 連打的機率。
        delay = (self.backoff_base ** attempt) + random.uniform(0.15, 0.85)
        LOGGER.warning("退避 %.1f 秒後重試（第 %s 次）", delay, attempt)
        time.sleep(delay)

    def get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if url in self._dead_hosts:
            LOGGER.info("本輪已確認 %s 不可用，直接改走後備端點", url.split("/shop/")[-1])
            return None
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code in {429, 500, 502, 503, 504, 541}:
                    LOGGER.warning(
                        "Apple API %s → HTTP %s（可能為頻率攔截）",
                        url.split("/shop/")[-1],
                        resp.status_code,
                    )
                    if resp.status_code == 541:
                        # 541 是官網對該路徑／出口 IP 的硬攔截，重試同一 URL 幾乎無效。
                        self._dead_hosts.add(url)
                        return None
                    if attempt < self.max_retries:
                        self._sleep_backoff(attempt)
                        continue
                    return None
                if resp.status_code >= 400:
                    LOGGER.warning("Apple API HTTP %s：%s", resp.status_code, url)
                    return None
                ctype = resp.headers.get("content-type", "")
                if "json" not in ctype.lower():
                    # fulfillment-messages 被 541/HTML 攔截時會走到這裡。
                    LOGGER.info(
                        "端點未回 JSON（content-type=%s），稍後改走後備 API",
                        ctype or "unknown",
                    )
                    self._dead_hosts.add(url)
                    return None
                payload = resp.json()
                if not isinstance(payload, dict):
                    return None
                return payload
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                LOGGER.warning("網路錯誤：%s", exc)
                if attempt < self.max_retries:
                    self._sleep_backoff(attempt)
            except ValueError as exc:
                last_error = exc
                LOGGER.warning("JSON 解析失敗：%s", exc)
                return None
        if last_error:
            LOGGER.error("重試耗盡：%s", last_error)
        return None

    def try_fulfillment(
        self, parts: list[str], store_id: str | None, location: str
    ) -> dict[str, Any] | None:
        """規格要求的官方路徑；香港目前常回 541，失敗屬預期。"""
        params: dict[str, Any] = {
            "pl": "true",
            "fae": "true",
            "mts.0": "regular",
            "searchNearby": "true",
            "little": "false",
            "location": location,
        }
        for idx, part in enumerate(parts):
            params[f"parts.{idx}"] = part
        if store_id:
            params["store"] = store_id
        return self.get_json(FULFILLMENT_URL, params)

    def pickup(self, parts: list[str], location: str = "Hong Kong") -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "pl": "true",
            "mts.0": "regular",
            "location": location,
        }
        for idx, part in enumerate(parts):
            params[f"parts.{idx}"] = part
        return self.get_json(PICKUP_URL, params)

    def delivery(self, parts: list[str], location: str = "Hong Kong") -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "little": "false",
            "mts.0": "regular",
            "location": location,
        }
        for idx, part in enumerate(parts):
            params[f"parts.{idx}"] = part
        return self.get_json(DELIVERY_URL, params)


def _pickup_status(part_info: dict[str, Any]) -> tuple[str, str]:
    display = str(part_info.get("pickupDisplay") or "").lower()
    regular = ((part_info.get("messageTypes") or {}).get("regular") or {})
    enabled = bool(regular.get("storeSelectionEnabled"))
    quote = str(
        regular.get("storePickupQuote")
        or part_info.get("pickupSearchQuote")
        or part_info.get("pickupType")
        or ""
    )
    if display == STATUS_AVAILABLE or enabled:
        return STATUS_AVAILABLE, quote
    if display == STATUS_UNAVAILABLE:
        return STATUS_UNAVAILABLE, quote or "暫無取貨"
    return STATUS_UNKNOWN, quote or "狀態不明"


def _delivery_status(part_block: dict[str, Any]) -> tuple[str, str]:
    regular = part_block.get("regular") if isinstance(part_block.get("regular"), dict) else part_block
    buy = regular.get("buyability") if isinstance(regular.get("buyability"), dict) else {}
    is_buyable = bool(regular.get("isBuyable") or buy.get("isBuyable"))
    options = regular.get("deliveryOptionMessages") or regular.get("deliveryOptions") or []
    quote = ""
    if options and isinstance(options[0], dict):
        quote = str(options[0].get("displayName") or options[0].get("date") or "")
    quote = quote or str(regular.get("stickyMessageSTH") or regular.get("subHeader") or "")
    # 去掉簡單 HTML，避免通知裡出現 <span>
    quote = (
        quote.replace("<span class=\"bold\">", "")
        .replace("</span>", "")
        .replace("&nbsp;", " ")
        .strip()
    )
    if is_buyable:
        return STATUS_AVAILABLE, quote or "可送貨"
    reason = str(buy.get("reason") or "")
    if reason and reason.upper() not in {"OK", ""}:
        return STATUS_UNAVAILABLE, reason
    return STATUS_UNAVAILABLE, quote or "暫不提供送貨"


def parse_pickup_payload(
    payload: dict[str, Any],
    watch: list[WatchItem],
    selected_stores: set[str],
    catalog: dict[str, Any],
) -> list[AvailabilityHit]:
    hits: list[AvailabilityHit] = []
    watch_by_part = {w.part_number: w for w in watch}
    stores = ((payload.get("body") or {}).get("stores")) or []
    # fulfillment-messages 舊結構
    if not stores:
        stores = (
            ((payload.get("body") or {}).get("content") or {})
            .get("pickupMessage", {})
            .get("stores")
            or []
        )
    for store in stores:
        store_id = str(store.get("storeNumber") or "")
        if selected_stores and store_id not in selected_stores:
            continue
        store_name = store_label(catalog, store_id) or str(store.get("storeName") or store_id)
        parts = store.get("partsAvailability") or {}
        for part, info in parts.items():
            if part not in watch_by_part:
                continue
            item = watch_by_part[part]
            status, quote = _pickup_status(info if isinstance(info, dict) else {})
            title = ""
            if isinstance(info, dict):
                regular = (info.get("messageTypes") or {}).get("regular") or {}
                title = str(regular.get("storePickupProductTitle") or "")
            hits.append(
                AvailabilityHit(
                    key=item_key("pickup", store_id, part),
                    mode="pickup",
                    part_number=part,
                    name=title or item.name,
                    capacity=item.capacity,
                    store_id=store_id,
                    store_name=store_name,
                    status=status,
                    quote=quote,
                    checkout_url=checkout_url(part),
                )
            )
    return hits


def parse_delivery_payload(
    payload: dict[str, Any],
    watch: list[WatchItem],
) -> list[AvailabilityHit]:
    hits: list[AvailabilityHit] = []
    watch_by_part = {w.part_number: w for w in watch}
    body = payload.get("body") or {}
    content = body.get("content") or body
    delivery = content.get("deliveryMessage") or content.get("delivery") or {}
    # fulfillment-messages 偶爾把送貨資訊放在 content.deliveryMessage
    for part, item in watch_by_part.items():
        block = delivery.get(part)
        if not isinstance(block, dict):
            continue
        status, quote = _delivery_status(block)
        hits.append(
            AvailabilityHit(
                key=item_key("delivery", "DELIVERY", part),
                mode="delivery",
                part_number=part,
                name=item.name,
                capacity=item.capacity,
                store_id="DELIVERY",
                store_name="送貨上門（香港）",
                status=status,
                quote=quote,
                checkout_url=checkout_url(part),
            )
        )
    return hits


# ---------------------------------------------------------------------------
# 狀態機：只在 unavailable → available 時通知
# ---------------------------------------------------------------------------
def apply_state_machine(
    state: dict[str, Any],
    hits: list[AvailabilityHit],
) -> tuple[list[AvailabilityHit], dict[str, Any]]:
    items: dict[str, Any] = dict(state.get("items") or {})
    newly_available: list[AvailabilityHit] = []
    checked_at = iso_now()

    for hit in hits:
        previous = items.get(hit.key) or {}
        prev_status = str(previous.get("status") or STATUS_UNKNOWN)
        if hit.status == STATUS_UNKNOWN:
            # 查詢失敗不改寫舊狀態，避免之後誤觸發「又有貨了」。
            items[hit.key] = {
                **previous,
                "last_checked_at": checked_at,
                "last_error": hit.quote or "unknown",
            }
            continue

        record = {
            "status": hit.status,
            "mode": hit.mode,
            "part_number": hit.part_number,
            "name": hit.name,
            "store_id": hit.store_id,
            "store_name": hit.store_name,
            "quote": hit.quote,
            "checkout_url": hit.checkout_url,
            "last_checked_at": checked_at,
        }
        if hit.status == STATUS_AVAILABLE:
            record["last_available_at"] = checked_at
            if prev_status != STATUS_AVAILABLE:
                newly_available.append(hit)
        else:
            record["last_available_at"] = previous.get("last_available_at")
        items[hit.key] = record

    new_state = {
        **state,
        "schema_version": 1,
        "updated_at": checked_at,
        "items": items,
    }
    return newly_available, new_state


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
def format_connection_test_message() -> str:
    """只測 Telegram 通道，絕不模仿補貨通知。"""
    return (
        "🔌 Telegram 連線測試\n"
        "\n"
        "呢條訊息純粹用嚟確認 Bot Token 同 Chat ID 設定正確，"
        "Telegram 通道可以成功收到訊息。\n"
        "\n"
        "唔代表任何 iPhone 有貨，亦唔係補貨通知，唔會改監控狀態。\n"
        f"時間：{iso_now()}（香港）\n"
    )


def _hit_lines(hits: list[AvailabilityHit]) -> list[str]:
    lines: list[str] = []
    for idx, hit in enumerate(hits, start=1):
        mode_label = "門市取貨" if hit.mode == "pickup" else "送貨上門"
        place = hit.store_name
        if hit.mode == "pickup" and hit.store_id:
            place = f"{hit.store_name}（{hit.store_id}）"
        lines.extend(
            [
                f"{idx}. [{mode_label}] {hit.name}",
                f"   📍 {place}",
                f"   💬 {hit.quote or '可立即購買'}",
                f"   🔗 直達結帳：{hit.checkout_url}",
                "",
            ]
        )
    return lines


def format_message(hits: list[AvailabilityHit]) -> str:
    """真實模式：官網由冇貨轉有貨。"""
    lines = [
        "🍎 Apple 香港有貨通知",
        "",
        f"共 {len(hits)} 項由「冇貨」轉為「有貨」（已依優先級排序）：",
        "",
    ]
    lines.extend(_hit_lines(hits))
    lines.append(f"時間：{iso_now()}（香港）")
    return "\n".join(lines).strip() + "\n"


def format_simulation_message(hits: list[AvailabilityHit]) -> str:
    """模擬模式測試結果：講明係測試，並列出邊部機補貨。"""
    lines = [
        "🧪 模擬模式測試結果",
        "",
        "以下項目由「冇貨」變成「有貨」。呢條係測試訊息，唔代表官網真係有貨。",
        "",
    ]
    lines.extend(_hit_lines(hits))
    lines.append(f"時間：{iso_now()}（香港）")
    return "\n".join(lines).strip() + "\n"


def _post_json(url: str, payload: dict[str, Any], timeout: int = 20) -> bool:
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code >= 400:
            LOGGER.error("通知失敗 HTTP %s：%s", resp.status_code, resp.text[:240])
            return False
        return True
    except requests.RequestException as exc:
        LOGGER.error("通知連線失敗：%s", exc)
        return False


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOGGER.info("未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，略過 Telegram")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = _post_json(
        url,
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        },
    )
    if ok:
        LOGGER.info("已發送 Telegram 通知")
    return ok


def dispatch_notifications(cfg: dict[str, Any], text: str) -> int:
    notify = cfg.get("notifications") or {}
    sent = 0
    if notify.get("telegram_enabled", True):
        sent += int(send_telegram(text))
    if sent == 0:
        LOGGER.warning("Telegram 通知失敗。請檢查 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID。")
    return sent


# ---------------------------------------------------------------------------
# 查詢編排
# ---------------------------------------------------------------------------
def mock_hits(
    cfg: dict[str, Any],
    watch: list[WatchItem],
    catalog: dict[str, Any],
    *,
    statuses: dict[str, str] | None = None,
    default_status: str | None = None,
) -> list[AvailabilityHit]:
    """依每格獨立狀態產生模擬結果。default_status 用於自我測試一次過全開／全關。"""
    per_item = statuses if statuses is not None else ((cfg.get("simulation") or {}).get("statuses") or {})
    fallback = str(default_status or STATUS_UNAVAILABLE).lower()
    if fallback not in {STATUS_AVAILABLE, STATUS_UNAVAILABLE}:
        fallback = STATUS_UNAVAILABLE
    hits: list[AvailabilityHit] = []
    modes = selected_modes(cfg)
    stores = [str(s) for s in (cfg.get("stores") or [])]
    for item in watch:
        if "pickup" in modes:
            for store_id in stores:
                key = item_key("pickup", store_id, item.part_number)
                status = str(per_item.get(key) or fallback).lower()
                if status not in {STATUS_AVAILABLE, STATUS_UNAVAILABLE}:
                    status = STATUS_UNAVAILABLE
                hits.append(
                    AvailabilityHit(
                        key=key,
                        mode="pickup",
                        part_number=item.part_number,
                        name=item.name,
                        capacity=item.capacity,
                        store_id=store_id,
                        store_name=store_label(catalog, store_id),
                        status=status,
                        quote="模擬：有貨" if status == STATUS_AVAILABLE else "模擬：冇貨",
                        checkout_url=checkout_url(item.part_number),
                    )
                )
        if "delivery" in modes:
            key = item_key("delivery", "DELIVERY", item.part_number)
            status = str(per_item.get(key) or fallback).lower()
            if status not in {STATUS_AVAILABLE, STATUS_UNAVAILABLE}:
                status = STATUS_UNAVAILABLE
            hits.append(
                AvailabilityHit(
                    key=key,
                    mode="delivery",
                    part_number=item.part_number,
                    name=item.name,
                    capacity=item.capacity,
                    store_id="DELIVERY",
                    store_name="送貨上門（香港）",
                    status=status,
                    quote="模擬：有貨" if status == STATUS_AVAILABLE else "模擬：冇貨",
                    checkout_url=checkout_url(item.part_number),
                )
            )
    return hits


def live_hits(
    cfg: dict[str, Any],
    watch: list[WatchItem],
    catalog: dict[str, Any],
    client: AppleClient,
) -> list[AvailabilityHit]:
    parts = [w.part_number for w in watch]
    stores = {str(s) for s in (cfg.get("stores") or [])}
    location = str((cfg.get("delivery") or {}).get("location") or "Hong Kong")
    modes = selected_modes(cfg)
    hits: list[AvailabilityHit] = []

    if "pickup" in modes:
        payload = client.try_fulfillment(parts, next(iter(stores), None), location)
        parsed = parse_pickup_payload(payload, watch, stores, catalog) if payload else []
        if not parsed:
            LOGGER.info("fulfillment-messages 無可用取貨資料，改查 pickup-message")
            payload = client.pickup(parts, location="Hong Kong")
            parsed = parse_pickup_payload(payload, watch, stores, catalog) if payload else []
        if not parsed:
            LOGGER.error("取貨查詢失敗，本輪門市狀態維持上次結果（不改寫）")
            # 補 unknown，讓狀態機略過
            for item in watch:
                for store_id in stores:
                    hits.append(
                        AvailabilityHit(
                            key=item_key("pickup", store_id, item.part_number),
                            mode="pickup",
                            part_number=item.part_number,
                            name=item.name,
                            capacity=item.capacity,
                            store_id=store_id,
                            store_name=store_label(catalog, store_id),
                            status=STATUS_UNKNOWN,
                            quote="pickup query failed",
                            checkout_url=checkout_url(item.part_number),
                        )
                    )
        else:
            hits.extend(parsed)

    if "delivery" in modes:
        payload = client.try_fulfillment(parts, None, location)
        parsed = parse_delivery_payload(payload, watch) if payload else []
        if not parsed:
            LOGGER.info("fulfillment-messages 無可用送貨資料，改查 delivery-message")
            payload = client.delivery(parts, location=location)
            parsed = parse_delivery_payload(payload, watch) if payload else []
        if not parsed:
            LOGGER.error("送貨查詢失敗，本輪送貨狀態維持上次結果（不改寫）")
            for item in watch:
                hits.append(
                    AvailabilityHit(
                        key=item_key("delivery", "DELIVERY", item.part_number),
                        mode="delivery",
                        part_number=item.part_number,
                        name=item.name,
                        capacity=item.capacity,
                        store_id="DELIVERY",
                        store_name="送貨上門（香港）",
                        status=STATUS_UNKNOWN,
                        quote="delivery query failed",
                        checkout_url=checkout_url(item.part_number),
                    )
                )
        else:
            hits.extend(parsed)

    return hits


def maybe_random_delay(cfg: dict[str, Any], *, skip: bool) -> None:
    if skip:
        return
    anti = cfg.get("anti_abuse") or {}
    bounds = anti.get("random_delay_seconds") or [3, 7]
    try:
        low, high = float(bounds[0]), float(bounds[1])
    except (TypeError, ValueError, IndexError):
        low, high = 3.0, 7.0
    if high < low:
        low, high = high, low
    delay = random.uniform(low, high)
    LOGGER.info("隨機延遲 %.1f 秒，降低被官網頻率限制的機率", delay)
    time.sleep(delay)


def apply_simulation(
    cfg: dict[str, Any],
    catalog: dict[str, Any],
    state: dict[str, Any],
) -> tuple[list[AvailabilityHit], dict[str, Any], str | None]:
    """套用每格模擬貨源。只改 state.simulation，唔動真實 Apple 紀錄。"""
    watch = resolve_watch_list(cfg, catalog)
    if not watch:
        return [], state, "config.json 沒有任何監控型號（models 為空）。"
    hits = mock_hits(cfg, watch, catalog)
    for hit in hits:
        hit.score = priority_score(cfg, hit)
    sim_wrap = {"items": dict(((state.get("simulation") or {}).get("items") or {}))}
    newly, new_sim = apply_state_machine(sim_wrap, hits)
    newly.sort(key=lambda h: (-h.score, h.mode, h.store_id, h.part_number))
    next_state = dict(state)
    sim = dict(next_state.get("simulation") or {})
    sim["items"] = new_sim.get("items") or {}
    sim["updated_at"] = iso_now()
    next_state["simulation"] = sim
    return newly, next_state, None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    cfg = load_json(CONFIG_PATH)
    catalog = load_json(CATALOG_PATH, fallback={"stores": [], "products": []})
    state = load_json(
        STATE_PATH,
        fallback={"updated_at": None, "schema_version": 1, "items": {}, "last_run": {}},
    )

    test_notify = bool(args.test_notify) or os.environ.get("TEST_NOTIFY", "").lower() in {
        "1",
        "true",
        "yes",
    }
    apply_sim = bool(args.apply_simulation) or os.environ.get(
        "APPLY_SIMULATION", ""
    ).lower() in {"1", "true", "yes"}
    ignore_enabled = bool(args.ignore_enabled)

    if test_notify:
        LOGGER.info("Telegram 連線測試：只發短訊確認通道，不查庫存、不改狀態機")
        text = format_connection_test_message()
        print(text)
        sent = dispatch_notifications(cfg, text)
        state["telegram_test"] = {
            "status": "ok" if sent else "failed",
            "message": f"已嘗試發送 Telegram 連線測試（成功通道 {sent}）",
            "at": iso_now(),
        }
        save_json(STATE_PATH, state)
        return 0

    if apply_sim:
        LOGGER.info("模擬模式：套用每格貨源，只在冇貨 → 有貨時發測試 Telegram")
        newly, new_state, err = apply_simulation(cfg, catalog, state)
        if err:
            LOGGER.error(err)
            print(err)
            sim = dict(state.get("simulation") or {})
            sim["last_run"] = {
                "status": "error",
                "message": err,
                "notified": 0,
                "checked": 0,
                "at": iso_now(),
            }
            state["simulation"] = sim
            save_json(STATE_PATH, state)
            return 0
        notified = 0
        if newly:
            text = format_simulation_message(newly)
            print(text)
            notified = 1 if dispatch_notifications(cfg, text) else 0
        else:
            print("模擬模式：沒有「冇貨 → 有貨」的新項目，不發送通知。")
        sim = dict(new_state.get("simulation") or {})
        sim["last_run"] = {
            "status": "ok",
            "message": f"模擬模式：新轉有貨 {len(newly)}（成功通道 {notified}）",
            "notified": len(newly),
            "checked": len(newly),
            "at": iso_now(),
        }
        new_state["simulation"] = sim
        save_json(STATE_PATH, new_state)
        return 0

    if not cfg.get("enabled", False) and not ignore_enabled:
        msg = "真實模式為 OFF（enabled=false），安全退出，不查詢 Apple API。"
        LOGGER.info(msg)
        print(msg)
        # 已是 disabled 就不要重寫 state，避免 Actions 每 2 分鐘空 commit。
        prev = state.get("last_run") or {}
        if prev.get("status") != "disabled":
            state["last_run"] = {
                "status": "disabled",
                "message": msg,
                "notified": 0,
                "checked": 0,
                "at": iso_now(),
            }
            save_json(STATE_PATH, state)
        return 0

    watch = resolve_watch_list(cfg, catalog)
    if not watch:
        msg = "config.json 沒有任何監控型號（models 為空）。"
        LOGGER.error(msg)
        state["last_run"] = {
            "status": "error",
            "message": msg,
            "notified": 0,
            "checked": 0,
            "at": iso_now(),
        }
        save_json(STATE_PATH, state)
        return 0

    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not is_manual and not args.ignore_interval and not due_for_interval(cfg, state):
        interval = cfg.get("check_interval_minutes") or 2
        msg = f"距離上次查詢未滿 {interval} 分鐘，略過本輪（不改寫狀態）。"
        LOGGER.info(msg)
        print(msg)
        return 0

    if not args.ignore_hours and not within_business_hours(cfg):
        msg = "目前不在營業時間窗內，略過本輪查詢（不改寫庫存狀態）。"
        LOGGER.info(msg)
        print(msg)
        prev = state.get("last_run") or {}
        if prev.get("status") != "outside_hours":
            state["last_run"] = {
                "status": "outside_hours",
                "message": msg,
                "notified": 0,
                "checked": 0,
                "at": iso_now(),
            }
            save_json(STATE_PATH, state)
        return 0

    maybe_random_delay(cfg, skip=bool(args.no_delay))

    LOGGER.info("真實模式：查詢 Apple 香港官網（只監測 iPhone Duo）")
    client = AppleClient(cfg)
    hits = live_hits(cfg, watch, catalog, client)

    for hit in hits:
        hit.score = priority_score(cfg, hit)

    newly, new_state = apply_state_machine(state, hits)
    newly.sort(key=lambda h: (-h.score, h.mode, h.store_id, h.part_number))

    available_now = sum(1 for h in hits if h.status == STATUS_AVAILABLE)
    LOGGER.info(
        "本輪查詢 %s 組，有貨 %s，新轉有貨 %s",
        len(hits),
        available_now,
        len(newly),
    )

    notified = 0
    if newly:
        text = format_message(newly)
        print(text)
        notified = 1 if dispatch_notifications(cfg, text) else 0
    else:
        print("沒有「冇貨 → 有貨」的新項目，不發送通知（防洗版）。")

    new_state["last_run"] = {
        "status": "ok",
        "message": f"真實模式：查詢 {len(hits)} 組，新轉有貨 {len(newly)}",
        "notified": len(newly),
        "checked": len(hits),
        "at": iso_now(),
    }
    save_json(STATE_PATH, new_state)
    return 0


def run_self_test() -> int:
    """不依賴網路的邏輯測試：開關、狀態機、聚合排序。"""
    failures = 0

    def check(cond: bool, label: str) -> None:
        nonlocal failures
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failures += 1

    cfg = {
        "enabled": False,
        "fulfillment_modes": ["pickup", "delivery"],
        "stores": ["R428", "R409"],
        "models": [
            {
                "part_number": "MK2E4ZA/A",
                "name": "iPhone Duo 256GB 夜空色",
                "capacity": "256gb",
            },
            {
                "part_number": "MK2G4ZA/A",
                "name": "iPhone Duo 512GB 夜空色",
                "capacity": "512gb",
            },
        ],
        "priority": {
            "stores": {"R428": 100, "R409": 10},
            "capacities": {"256gb": 100, "512gb": 10},
            "modes": {"pickup": 100, "delivery": 1},
        },
        "business_hours": {
            "enabled": True,
            "timezone": "Asia/Hong_Kong",
            "start": "10:00",
            "end": "22:00",
            "days": [0, 1, 2, 3, 4, 5, 6],
        },
        "delivery": {"enabled": True, "location": "Hong Kong"},
    }
    catalog = load_json(CATALOG_PATH)

    # 營業時間
    inside = datetime(2026, 10, 26, 12, 0, tzinfo=HK_TZ)
    outside = datetime(2026, 10, 26, 23, 30, tzinfo=HK_TZ)
    check(within_business_hours(cfg, inside), "營業時間內應通過")
    check(not within_business_hours(cfg, outside), "營業時間外應過濾")

    cfg["check_interval_minutes"] = 15
    fresh = {"last_run": {"status": "ok", "at": inside.isoformat()}}
    later = inside.replace(minute=inside.minute + 3)
    much_later = inside.replace(hour=inside.hour + 1)
    check(not due_for_interval(cfg, fresh, later), "未滿間隔不應再查")
    check(due_for_interval(cfg, fresh, much_later), "滿間隔應再查")
    check(due_for_interval(cfg, {"last_run": {"status": "disabled", "at": inside.isoformat()}}, later), "非成功查詢不套用間隔")

    watch = resolve_watch_list(cfg, catalog)
    hits = mock_hits(cfg, watch, catalog, default_status=STATUS_AVAILABLE)
    check(len(hits) == 6, f"雙模式模擬應產生 6 組（實際 {len(hits)}）")

    # 第一次：空狀態 + available → 全部進入通知
    newly, state = apply_state_machine({"items": {}}, hits)
    check(len(newly) == 6, f"首次有貨應通知 6 筆（實際 {len(newly)}）")

    # 第二次：狀態已是 available → 防洗版
    newly2, _ = apply_state_machine(state, hits)
    check(len(newly2) == 0, "持續有貨不應再通知")

    # 無貨再有貨
    empty_hits = mock_hits(cfg, watch, catalog, default_status=STATUS_UNAVAILABLE)
    _, state3 = apply_state_machine(state, empty_hits)
    again = mock_hits(cfg, watch, catalog, default_status=STATUS_AVAILABLE)
    newly3, _ = apply_state_machine(state3, again)
    check(len(newly3) == 6, "無貨→有貨應再次通知")

    for hit in newly3:
        hit.score = priority_score(cfg, hit)
    newly3.sort(key=lambda h: (-h.score, h.mode, h.store_id))
    top = newly3[0]
    check(
        top.store_id == "R428" and top.capacity == "256gb" and top.mode == "pickup",
        f"最高優先應為 ifc 256GB 門市（實際 {top.store_id} {top.capacity} {top.mode}）",
    )

    text = format_message(newly3)
    check("直達結帳" in text and "門市取貨" in text and "送貨上門" in text, "綜合訊息含通路與結帳連結")
    check(text.count("🍎") == 1, "同週期只產出 1 條訊息")

    ping = format_connection_test_message()
    check("Telegram 連線測試" in ping, "連線測試標題正確")
    check("唔代表任何 iPhone 有貨" in ping, "連線測試聲明唔係補貨")
    check("直達結帳" not in ping and "有貨通知" not in ping, "連線測試不含補貨版面")

    duo_cfg = json.loads(json.dumps(cfg))
    duo_cfg["models"] = [cfg["models"][0]]
    duo_watch = resolve_watch_list(duo_cfg, catalog)
    all_keys = [h.key for h in mock_hits(duo_cfg, duo_watch, catalog, default_status=STATUS_UNAVAILABLE)]
    duo_cfg["simulation"] = {"statuses": {k: STATUS_UNAVAILABLE for k in all_keys}}
    none, sim_state, sim_err = apply_simulation(duo_cfg, catalog, {"items": {}, "simulation": {"items": {}}})
    check(sim_err is None and len(none) == 0, "模擬全冇貨不應通知")
    ifc_key = item_key("pickup", "R428", "MK2E4ZA/A")
    duo_cfg["simulation"]["statuses"][ifc_key] = STATUS_AVAILABLE
    one, sim_state, _ = apply_simulation(duo_cfg, catalog, sim_state)
    check(len(one) == 1 and one[0].store_id == "R428", f"只有 ifc 冇貨→有貨應通知 1 筆（實際 {len(one)}）")
    zero, sim_state, _ = apply_simulation(duo_cfg, catalog, sim_state)
    check(len(zero) == 0, "模擬持續有貨不應再通知")
    cwb_key = item_key("pickup", "R409", "MK2E4ZA/A")
    duo_cfg["simulation"]["statuses"][cwb_key] = STATUS_AVAILABLE
    two, _, _ = apply_simulation(duo_cfg, catalog, sim_state)
    check(len(two) == 1 and two[0].store_id == "R409", "另一格冇貨→有貨才再通知")
    sim_text = format_simulation_message(one)
    check("模擬模式測試結果" in sim_text and "唔代表官網真係有貨" in sim_text, "測試結果訊息標明係模擬")
    check("🍎" not in sim_text, "測試結果唔好同真實補貨標題混淆")

    from usage import run_self_test as usage_self_test

    print("用量提醒：")
    failures += usage_self_test()

    print("自我測試完成，失敗數：", failures)
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apple 香港 iPhone 庫存監控")
    parser.add_argument("--test-notify", action="store_true", help="只發送 Telegram 連線測試短訊")
    parser.add_argument(
        "--apply-simulation",
        action="store_true",
        help="套用模擬貨源：只有冇貨→有貨先發測試 Telegram",
    )
    parser.add_argument("--ignore-enabled", action="store_true", help="忽略 enabled 總開關")
    parser.add_argument("--ignore-hours", action="store_true", help="忽略營業時間窗")
    parser.add_argument("--ignore-interval", action="store_true", help="忽略監控頻率間隔")
    parser.add_argument("--no-delay", action="store_true", help="跳過隨機延遲")
    parser.add_argument("--self-test", action="store_true", help="執行不連網的邏輯測試")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    configure_logging(args.verbose)
    if args.self_test:
        return run_self_test()
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001 — 頂層捕捉，避免 Actions 留下半寫入
        LOGGER.exception("未預期錯誤：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

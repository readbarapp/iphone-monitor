#!/usr/bin/env python3
"""紀錄每次 Actions 跑大約用 1 分鐘，並在 1800／2000 發 Telegram 提醒。

只用標準庫，方便 workflow 喺未 pip install 時都跑到。
GitHub 私人倉庫免費額度每月 2,000 分鐘（UTC 曆月）。公開倉庫通常唔計呢個額度。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
FREE_MINUTES = 2000
WARN_POINTS = (1800, 2000)


def billing_month(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"schema_version": 1, "items": {}, "last_run": {}}
    with STATE_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {"schema_version": 1, "items": {}}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(STATE_PATH)


def apply_usage_tick(
    state: dict[str, Any],
    *,
    minutes: int = 1,
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[int]]:
    """累加本月估計分鐘，回傳（新 state, 今次新跨過嘅門檻）。"""
    month = billing_month(now)
    usage = dict(state.get("usage") or {})
    if usage.get("month") != month:
        usage = {"month": month, "estimated_minutes": 0, "warned": []}
    used = int(usage.get("estimated_minutes") or 0) + max(0, int(minutes))
    usage["estimated_minutes"] = used
    already = {int(x) for x in (usage.get("warned") or [])}
    crossed: list[int] = []
    for mark in WARN_POINTS:
        if used >= mark and mark not in already:
            crossed.append(mark)
            already.add(mark)
    usage["warned"] = sorted(already)
    next_state = dict(state)
    next_state["usage"] = usage
    return next_state, crossed


def format_usage_warning(mark: int, used: int, month: str) -> str:
    if mark >= FREE_MINUTES:
        return (
            "⚠️ GitHub Actions 用量提醒\n"
            "\n"
            f"呢個監控系統本月（{month}，UTC）估計已用約 {used} 分鐘，"
            f"已到達免費額度 {FREE_MINUTES} 分鐘。\n"
            "\n"
            "GitHub 唔會幫你優雅停低呢個監控。私人倉庫超額之後，排程可能突然跑唔到；"
            "如果未設消費上限，仲可能開始收費。\n"
            "\n"
            "要真正停燒分鐘：去 GitHub → Actions → iPhone HK Monitor → ⋮ → Disable workflow。\n"
            "只關「真實模式」唔夠，因為鬧鐘（cron）仍會每轉用大約 1 分鐘。\n"
            "真正已用量以 GitHub Billing 為準。\n"
        )
    remain = max(0, FREE_MINUTES - used)
    return (
        "⚠️ GitHub Actions 用量提醒\n"
        "\n"
        f"呢個監控系統本月（{month}，UTC）估計已用約 {used} 分鐘，"
        f"接近免費額度 {FREE_MINUTES} 分鐘（大約仲剩 {remain}）。\n"
        "\n"
        "盯多幾部機／幾間門市唔會令「跑幾多次」變多，但鬧鐘一日響多少次就用多少分鐘。\n"
        "想慳：去 Actions Disable workflow，或者把頻率改疏啲。\n"
        "真正已用量以 GitHub Billing 為準。\n"
    )


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，略過用量提醒", file=sys.stderr)
        return False
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        ensure_ascii=False,
    ).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError as exc:
        print(f"用量提醒發送失敗：{exc}", file=sys.stderr)
        return False


def run_self_test() -> int:
    failures = 0

    def check(cond: bool, label: str) -> None:
        nonlocal failures
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failures += 1

    jan = datetime(2026, 1, 15, tzinfo=timezone.utc)
    feb = datetime(2026, 2, 1, tzinfo=timezone.utc)
    state, crossed = apply_usage_tick({"usage": {}}, minutes=1, now=jan)
    check(state["usage"]["estimated_minutes"] == 1, "第一次累加 1 分鐘")
    check(crossed == [], "1 分鐘未過門檻")

    state, crossed = apply_usage_tick(state, minutes=1799, now=jan)
    check(state["usage"]["estimated_minutes"] == 1800, "累加到 1800")
    check(crossed == [1800], "剛到 1800 應提醒一次")

    state, crossed = apply_usage_tick(state, minutes=1, now=jan)
    check(crossed == [], "1800 之後唔好再提醒同一門檻")

    state, crossed = apply_usage_tick(state, minutes=199, now=jan)
    check(state["usage"]["estimated_minutes"] == 2000, "累加到 2000")
    check(crossed == [2000], "剛到 2000 應提醒一次")

    state, crossed = apply_usage_tick(state, minutes=10, now=feb)
    check(state["usage"]["month"] == "2026-02", "新曆月應歸零重計")
    check(state["usage"]["estimated_minutes"] == 10, "新月由 0 再加")
    check(state["usage"]["warned"] == [], "新月清提醒紀錄")

    text = format_usage_warning(1800, 1800, "2026-10")
    check("1800" in text and "用量提醒" in text, "1800 提醒文案")
    text2 = format_usage_warning(2000, 2005, "2026-10")
    check("2000" in text2 and "Disable workflow" in text2, "2000 提醒叫人停鬧鐘")

    print("用量自我測試完成，失敗數：", failures)
    return 1 if failures else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return run_self_test()
    state, crossed = apply_usage_tick(load_state(), minutes=1)
    usage = state.get("usage") or {}
    used = int(usage.get("estimated_minutes") or 0)
    month = str(usage.get("month") or billing_month())
    print(f"usage month={month} estimated_minutes={used} crossed={crossed}")
    for mark in crossed:
        ok = send_telegram(format_usage_warning(mark, used, month))
        print(f"warning {mark}: {'sent' if ok else 'not_sent'}")
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

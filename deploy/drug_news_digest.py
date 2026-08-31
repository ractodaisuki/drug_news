#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drug_news の news.json から未送信の記事を選んで Telegram へ送る。

配置: hermes-vps:/opt/data/scripts/drug_news_digest.py（hermes ユーザーの cron で毎朝実行）
元ネタ: /opt/data/scripts/rss_news_importance_digest.py

一般ニュースの digest と違い、重要度の閾値ではなく「まだ送っていないか」で選ぶ。
薬のフィードは1日の流量が少ないので閾値で切ると何日も無音になり、逆に閾値を下げると
同じ記事が毎朝並ぶ。送信済みリンクを state に持って差分だけ流すのが実態に合う。

使い方:
    drug_news_digest.py                # 未送信の記事を送る（cron 用）
    drug_news_digest.py --no-send      # 送らずに内容だけ表示
    drug_news_digest.py --min 4        # ★4以上に絞る
    drug_news_digest.py --resend       # 送信済みを無視して送り直す
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

# 検証時はローカルのファイルパスを渡せる（DRUG_NEWS_URL=./data/news.json など）。
NEWS_URL = os.environ.get(
    "DRUG_NEWS_URL", "https://raw.githubusercontent.com/ractodaisuki/drug_news/main/data/news.json"
)
STATUS_URL = os.environ.get(
    "DRUG_NEWS_STATUS_URL", "https://raw.githubusercontent.com/ractodaisuki/drug_news/main/data/status.json"
)
# 専用ボットなので token を他のボットと共有しない。FLEET.md の household/*.env に揃える。
ENV_PATH = Path(os.environ.get("DRUG_NEWS_ENV", "/opt/data/household/drugnews.env"))
STATE_PATH = Path(os.environ.get("DRUG_NEWS_DIGEST_STATE", "/opt/data/scripts/.drug_news_digest_state.json"))

MIN_IMPORTANCE = 3
MAX_ITEMS = 12
STATE_RETENTION = 800


def fetch_json(url: str) -> dict:
    if not url.startswith(("http://", "https://")):
        return json.loads(Path(url).read_text(encoding="utf-8"))

    req = urllib.request.Request(url, headers={"User-Agent": "Hermes-DrugNews-Digest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        return json.loads(resp.read().decode("utf-8"))


def load_env_value(key: str, env_path: Path = ENV_PATH) -> str:
    if key in os.environ:
        return os.environ[key]
    try:
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""


def load_sent_links() -> list[str]:
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    links = payload.get("sent") if isinstance(payload, dict) else None
    return [str(link) for link in links] if isinstance(links, list) else []


def save_sent_links(links: list[str]) -> None:
    # 末尾が最新。古いものから捨てる。
    trimmed = list(dict.fromkeys(links))[-STATE_RETENTION:]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"count": len(trimmed), "sent": trimmed}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def one_line(text: str, limit: int = 120) -> str:
    text = " ".join(unescape(str(text or "")).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_item(idx: int, item: dict) -> str:
    importance = int(item.get("importance") or 0)
    tags = item.get("tags") or []
    keywords = item.get("keywords") or []

    lines = [
        f"{idx}. {'★' * importance} {one_line(item.get('title'), 140)}",
        f"   {one_line(item.get('source'), 40)} / {item.get('published_label') or '日時不明'}"
        f" / {' / '.join(str(t) for t in tags[:3]) if tags else 'タグなし'}",
    ]

    summary = one_line(item.get("summary"), 260)
    if summary and summary != one_line(item.get("title"), 140):
        lines.append(f"   {summary}")
    if keywords:
        lines.append(f"   🔑 {' · '.join(str(k) for k in keywords[:5])}")
    if item.get("link"):
        lines.append(f"   {item['link']}")
    return "\n".join(lines)


def select_items(news: dict, min_importance: int, sent_links: set[str]) -> list[dict]:
    items = [
        item
        for item in news.get("items", [])
        if int(item.get("importance") or 0) >= min_importance and item.get("link") not in sent_links
    ]
    # 重要度が高いものを先に。同点なら新しい順。
    items.sort(key=lambda it: (int(it.get("importance") or 0), it.get("published") or ""), reverse=True)
    return items


def format_header(news: dict, status: dict, *, selected: int, total_new: int, min_importance: int) -> str:
    lines = [
        f"薬剤師ニュース（★{min_importance}以上の新着）",
        f"最終更新: {news.get('updated_label') or '不明'}",
    ]
    if status.get("state") == "error":
        lines.append(f"⚠️ 収集状態: {status.get('message') or 'エラー'}")
    lines.append(f"新着: {total_new}件 / 収集済み {len(news.get('items', []))}件")
    if selected < total_new:
        lines.append(f"うち {selected} 件を表示")
    return "\n".join(lines)


def send_telegram_message(text: str, *, disable_notification: bool = False) -> bool:
    token = load_env_value("DRUG_NEWS_BOT_TOKEN")
    chat_id = load_env_value("DRUG_NEWS_CHAT_ID")
    if not token or not chat_id:
        print(f"DRUG_NEWS_BOT_TOKEN / DRUG_NEWS_CHAT_ID が {ENV_PATH} に無い")
        return False

    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "false",
            # 記事ごとに1通送るので、通知音は先頭だけ。
            "disable_notification": "true" if disable_notification else "false",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as error:
        # 新規ボットでは chat not found（本人がまだボットに話しかけていない）が起きやすいので、
        # 理由を潰さずそのまま出す。
        print(f"Telegram API error {error.code}: {error.read().decode('utf-8', 'ignore')[:200]}")
        return False
    except Exception as error:  # noqa: BLE001
        print(f"Telegram send failed: {error}")
        return False


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send unsent drug news as Telegram messages.")
    parser.add_argument("--no-send", action="store_true", help="送らずに内容だけ表示する")
    parser.add_argument("--min", type=int, default=MIN_IMPORTANCE, help=f"重要度の下限 (既定 {MIN_IMPORTANCE})")
    parser.add_argument("--max", type=int, default=MAX_ITEMS, help=f"1回に送る最大件数 (既定 {MAX_ITEMS})")
    parser.add_argument("--resend", action="store_true", help="送信済みを無視して送り直す")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    news = fetch_json(NEWS_URL)
    try:
        status = fetch_json(STATUS_URL)
    except Exception:
        status = {}

    sent_links = set() if args.resend else set(load_sent_links())
    candidates = select_items(news, args.min, sent_links)
    selected = candidates[: args.max]

    header = format_header(
        news, status, selected=len(selected), total_new=len(candidates), min_importance=args.min
    )

    if not selected:
        if args.no_send:
            print(header + "\n\n新着はありません。")
        # 新着ゼロで毎朝「ありません」を送っても読まないので、黙って終わる。
        print("no new items")
        return 0

    messages = [header] + [format_item(idx, item) for idx, item in enumerate(selected, start=1)]
    if len(candidates) > len(selected):
        messages.append(f"ほか {len(candidates) - len(selected)} 件は次回に回します。")

    if args.no_send:
        print("\n\n---MESSAGE---\n\n".join(messages))
        return 0

    sent_any = False
    for index, message in enumerate(messages):
        if send_telegram_message(message, disable_notification=index > 0):
            sent_any = True
        else:
            print(f"send failed at message {index}")
            return 1
        time.sleep(0.5)

    if sent_any and not args.resend:
        save_sent_links(load_sent_links() + [item["link"] for item in selected])

    print(f"sent {len(selected)} items")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

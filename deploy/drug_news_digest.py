#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drug_news の news.json から未送信の記事を選んで Telegram へ送る。

配置: hermes-vps:/opt/data/scripts/drug_news_digest.py（hermes ユーザーの cron で毎朝実行）
元ネタ: /opt/data/scripts/rss_news_importance_digest.py

平日20〜33件しか流れてこないので、★で絞らずその日の新着を全部1通の一覧にする。
キーワード採点では「ビタジェクトが一時供給停止」と「太陽光・蓄電池で供給を守る」を
区別できず、当てにならない★で切ると良い記事を落とす方が損。素読みできる件数なら選別しない。

何を出すかは重要度ではなく「まだ送っていないか」で決める。送信済みリンクを state に持つので、
連休や実行漏れがあっても取りこぼさず、同じ記事が翌朝また並ぶこともない。

使い方:
    drug_news_digest.py                # 未送信の記事を送る（cron 用）
    drug_news_digest.py --no-send      # 送らずに内容だけ表示
    drug_news_digest.py --min 4        # 絞りたいときだけ（既定は絞らない）
    drug_news_digest.py --resend       # 送信済みを無視して送り直す
"""

from __future__ import annotations

import argparse
import datetime
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

MIN_IMPORTANCE = 1   # 絞らない。★は当てにならないので順位付けに使わない
MAX_ITEMS = 60
STATE_RETENTION = 800
MESSAGE_CHAR_BUDGET = 3800   # Telegram の上限 4096 に余裕を持たせる

# 「業界動向」しか当たらなかった記事は薬局実務に関係しない（筆頭株主・四半期業績・
# 販売提携・疾患啓発マンガなど）。データ側には残し、一覧に出すときだけ落とす。
# 「その他」は落とさない。OTC類似薬の見直し議論や薬局実習の記事が混ざっていて、
# タグだけでは当たりと外れを分けられないため。
SKIP_TAG_SETS = ({"業界動向"},)


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


def format_item(item: dict) -> str:
    """一覧の1件。タイトルと出典とURLだけ。

    ★は付けない。キーワード採点では「ビタジェクト供給停止」と「太陽光・蓄電池で供給を守る」を
    区別できず、当てにならない順位を添えると読み手が信用してしまうため。
    """
    return f"・{one_line(item.get('title'), 60)}\n  {one_line(item.get('source'), 20)} {item.get('link') or ''}"


def select_items(news: dict, min_importance: int, sent_links: set[str]) -> list[dict]:
    items = [
        item
        for item in news.get("items", [])
        if int(item.get("importance") or 0) >= min_importance
        and item.get("link") not in sent_links
        and set(item.get("tags") or []) not in SKIP_TAG_SETS
    ]
    # 新しい順。絞らずに全部出すので、並べ替えの基準は日付だけでいい。
    items.sort(key=lambda it: it.get("published") or "", reverse=True)
    return items


def format_header(news: dict, status: dict, *, total_new: int) -> str:
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date().isoformat()
    lines = [f"薬剤師ニュース {today}（{total_new}件）"]
    if status.get("state") == "error":
        lines.append(f"⚠️ 収集が失敗しています: {status.get('message') or 'エラー'}")
    lines.append(f"最終更新: {news.get('updated_label') or '不明'}")
    return "\n".join(lines)


def build_messages(header: str, items: list[dict]) -> list[str]:
    """Telegram の1通あたり4096文字に収まるように分割する。

    平日で20〜33件なので普通は1通で収まるが、連休明けや障害復帰後にまとめて出ると溢れる。
    """
    messages: list[str] = []
    current = [header, ""]
    length = len(header) + 1

    for item in items:
        block = format_item(item)
        if length + len(block) + 1 > MESSAGE_CHAR_BUDGET and len(current) > 2:
            messages.append("\n".join(current))
            current = [f"（続き {len(messages) + 1}）", ""]
            length = len(current[0]) + 1

        current.append(block)
        length += len(block) + 1

    messages.append("\n".join(current))
    return messages


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
            # 溢れて複数通になったときは、通知音は先頭だけ。
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
    parser = argparse.ArgumentParser(description="Send unsent drug news as one Telegram list.")
    parser.add_argument("--no-send", action="store_true", help="送らずに内容だけ表示する")
    parser.add_argument("--min", type=int, default=MIN_IMPORTANCE, help=f"重要度の下限 (既定 {MIN_IMPORTANCE}=絞らない)")
    parser.add_argument("--max", type=int, default=MAX_ITEMS, help=f"1回に出す最大件数 (既定 {MAX_ITEMS})")
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

    if not selected:
        # 新着ゼロで毎朝「ありません」を送っても読まないので、黙って終わる。
        print("no new items")
        return 0

    messages = build_messages(format_header(news, status, total_new=len(selected)), selected)
    if len(candidates) > len(selected):
        messages[-1] += f"\n\nほか {len(candidates) - len(selected)} 件は次回に回します。"

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

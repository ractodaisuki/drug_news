#!/usr/bin/env python3
"""薬剤師向けニュースを RSS から収集し、Gemini で実務重要度を付けて data/news.json に書く。

ractodaisuki/RSS_news の fetch_rss.py から取得まわり（フィード取得・日付パース・重複排除・
Gemini 解析のキャッシュ）を移植し、汎用ニュース向けの重みづけを薬剤師向けに入れ替えたもの。

元スクリプトとの主な違い:
- importance は Gemini の判定をそのまま使う。元は calc_importance() のヒューリスティックが
  最終値で、Gemini の importance は analytics.json に入るだけで表示に使われていなかった。
  薬の記事が一般ニュースと同じ土俵で相対評価されて★3に張り付く原因がこれ。
- カテゴリと重要度の基準を薬局実務（回収・供給・添付文書改訂・薬価/報酬）に振り直した。
- feeds.json に include_keywords を足した。厚労省の新着 RSS は省全体が流れてくるため、
  医薬品関連だけに絞らないと Gemini の解析枠を予算や雇用の記事に食われる。
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import certifi
import feedparser
from dateutil import parser as date_parser

ROOT_DIR = Path(__file__).resolve().parents[1]
FEEDS_PATH = ROOT_DIR / "feeds.json"
TAG_RULES_PATH = ROOT_DIR / "config" / "tag_rules.json"
NEWS_OUTPUT_PATH = ROOT_DIR / "data" / "news.json"
ANALYSIS_CACHE_PATH = ROOT_DIR / "data" / "analysis_cache.json"

MAX_ITEMS_PER_FEED = 60
MAX_ITEMS_TOTAL = 200
MAX_SUMMARY_LENGTH = 220
MAX_TAGS_PER_ITEM = 4
MAX_GEMINI_CONTENT_LENGTH = 4000
MAX_GEMINI_ANALYSIS_PER_RUN = int(os.environ.get("MAX_GEMINI_ANALYSIS_PER_RUN", "80"))
MAX_GEMINI_KEYWORDS = 5
CACHE_RETENTION = 1500

REQUEST_TIMEOUT = 20
USER_AGENT = "PharmaNews/1.0 (+https://github.com/ractodaisuki/pharma-news)"
DISPLAY_TIMEZONE = ZoneInfo("Asia/Tokyo")
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

DEFAULT_TAG = "その他"
WEB_MONITOR_SOURCE = "Web監視"
GEMINI_MODEL_CANDIDATES = ("gemini-2.5-flash", "gemini-2.0-flash")

# 薬局実務の切り口。tag_rules.json のタグ名と揃えてある。
CATEGORIES = (
    "回収",
    "供給・出荷",
    "安全性情報",
    "承認・新薬",
    "薬価・制度",
    "法令・行政",
    "医療安全",
    "薬局経営",
    "感染症・ワクチン",
    "一般用医薬品",
    "学術・エビデンス",
    "業界動向",
    "薬剤師コラム",
    "その他",
)
CATEGORY_SET = set(CATEGORIES)
CATEGORY_PROMPT = "、".join(CATEGORIES)
CATEGORY_ALIASES = {
    "医薬品回収": "回収",
    "自主回収": "回収",
    "供給": "供給・出荷",
    "出荷": "供給・出荷",
    "供給不安": "供給・出荷",
    "限定出荷": "供給・出荷",
    "安全性": "安全性情報",
    "副作用": "安全性情報",
    "添付文書": "安全性情報",
    "承認": "承認・新薬",
    "新薬": "承認・新薬",
    "薬価": "薬価・制度",
    "制度": "薬価・制度",
    "診療報酬": "薬価・制度",
    "調剤報酬": "薬価・制度",
    "法令": "法令・行政",
    "行政": "法令・行政",
    "規制": "法令・行政",
    "ヒヤリ・ハット": "医療安全",
    "医療事故": "医療安全",
    "経営": "薬局経営",
    "薬局": "薬局経営",
    "感染症": "感染症・ワクチン",
    "ワクチン": "感染症・ワクチン",
    "OTC": "一般用医薬品",
    "市販薬": "一般用医薬品",
    "学術": "学術・エビデンス",
    "エビデンス": "学術・エビデンス",
    "研究": "学術・エビデンス",
    "臨床試験": "学術・エビデンス",
    "業界": "業界動向",
    "製薬": "業界動向",
    "コラム": "薬剤師コラム",
}
FALLBACK_CATEGORY = "その他"
FALLBACK_IMPORTANCE = 3

RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["category", "importance", "keywords"],
    "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        "keywords": {
            "type": "array",
            "minItems": 3,
            "maxItems": MAX_GEMINI_KEYWORDS,
            "items": {"type": "string"},
        },
    },
}

MIN_KEYWORD_LENGTH = 3
IGNORED_GENERIC_KEYWORDS = {"x"}
ALLOWED_SHORT_ASCII_KEYWORDS = {"gmp", "otc", "di"}

# Gemini が使えないときのフォールバック用。薬局で「今すぐ動く」必要がある語ほど重い。
CRITICAL_TITLE_KEYWORDS = (
    "自主回収",
    "回収命令",
    "緊急安全性情報",
    "イエローレター",
    "安全性速報",
    "ブルーレター",
    "供給停止",
    "販売中止",
    "使用中止",
)
STRONG_TITLE_KEYWORDS = (
    "限定出荷",
    "出荷調整",
    "添付文書",
    "改訂",
    "禁忌",
    "警告",
    "承認",
    "薬価",
    "改定",
    "算定",
    "事務連絡",
    "通知",
)
SHORT_SUMMARY_THRESHOLD = 24


class PlainTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def get_text(self) -> str:
        return "".join(self.parts)


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    published: str
    published_label: str
    summary: str
    tags: list[str]
    importance: int
    keywords: list[str]
    sort_key: tuple[int, float]
    published_dt: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "link": self.link,
            "source": self.source,
            "published": self.published,
            "published_label": self.published_label,
            "summary": self.summary,
            "tags": self.tags,
            "importance": self.importance,
            "keywords": self.keywords,
        }


@dataclass
class FetchStats:
    total_feeds: int = 0
    successful_feeds: int = 0
    failed_feeds: int = 0


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def load_feed_configs(path: Path) -> list[dict[str, Any]]:
    configs = load_json(path)
    if not isinstance(configs, list):
        raise ValueError("feeds.json must contain a list of feed definitions.")

    valid_configs: list[dict[str, Any]] = []
    for config in configs:
        if not isinstance(config, dict):
            logging.warning("Skipping invalid feed config: %r", config)
            continue

        name = str(config.get("name", "")).strip()
        url = str(config.get("url", "")).strip()
        if not name or not url:
            logging.warning("Skipping feed config with missing name/url: %r", config)
            continue

        raw_includes = config.get("include_keywords")
        includes = (
            [normalize_text(keyword).casefold() for keyword in raw_includes if normalize_text(keyword)]
            if isinstance(raw_includes, list)
            else []
        )

        valid_configs.append({"name": name, "url": url, "include_keywords": includes})

    return valid_configs


def load_tag_rules(path: Path) -> dict[str, list[str]]:
    rules = load_json(path)
    if not isinstance(rules, dict):
        raise ValueError("tag_rules.json must contain an object.")

    normalized_rules: dict[str, list[str]] = {}
    for tag, keywords in rules.items():
        if not isinstance(tag, str) or not isinstance(keywords, list):
            logging.warning("Skipping invalid tag rule: %r -> %r", tag, keywords)
            continue

        clean_keywords = [
            normalized
            for normalized in (normalize_text(keyword).casefold() for keyword in keywords)
            if not should_ignore_keyword(normalized)
        ]
        if clean_keywords:
            normalized_rules[tag.strip()] = clean_keywords

    return normalized_rules


def should_ignore_keyword(keyword: str) -> bool:
    if not keyword or keyword in IGNORED_GENERIC_KEYWORDS:
        return True

    if is_short_ascii_keyword(keyword):
        if len(keyword) >= MIN_KEYWORD_LENGTH:
            return False
        return keyword not in ALLOWED_SHORT_ASCII_KEYWORDS

    return len(keyword) < 2


def is_short_ascii_keyword(keyword: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9.+-]+", keyword))


def text_contains_keyword(text: str, keyword: str) -> bool:
    if not keyword:
        return False

    if is_short_ascii_keyword(keyword):
        pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
        return re.search(pattern, text) is not None

    return keyword in text


def fetch_feed_content(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT) as response:
        return response.read()


def parse_feed_datetime(entry: Any) -> datetime | None:
    candidates = [
        getattr(entry, "published", None),
        getattr(entry, "updated", None),
        entry.get("dc_date"),
    ]

    for candidate in candidates:
        parsed = parse_datetime_value(candidate)
        if parsed is not None:
            return parsed

    for struct_key in ("published_parsed", "updated_parsed"):
        struct_value = getattr(entry, struct_key, None)
        if struct_value is None:
            continue

        try:
            return datetime(*struct_value[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue

    return None


def parse_datetime_value(value: Any) -> datetime | None:
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        parsed = None

    if parsed is None:
        try:
            parsed = date_parser.parse(text)
        except (TypeError, ValueError, OverflowError):
            return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def normalize_text(value: Any, max_length: int | None = None) -> str:
    text = unescape(str(value or ""))
    extractor = PlainTextExtractor()
    extractor.feed(text)
    normalized = re.sub(r"\s+", " ", extractor.get_text()).strip()

    if max_length is not None and len(normalized) > max_length:
        return normalized[: max_length - 1].rstrip() + "…"

    return normalized


def normalize_link(link: str) -> str:
    return str(link or "").strip()


def format_datetime(value: datetime | None) -> tuple[str, str, tuple[int, float]]:
    if value is None:
        return "", "", (1, 0.0)

    utc_value = value.astimezone(timezone.utc)
    local_value = utc_value.astimezone(DISPLAY_TIMEZONE)
    return (
        utc_value.isoformat().replace("+00:00", "Z"),
        local_value.strftime("%Y/%m/%d %H:%M"),
        (0, -utc_value.timestamp()),
    )


def extract_summary(entry: Any) -> str:
    content = entry.get("content")
    summary_candidates = [
        entry.get("summary"),
        entry.get("description"),
        content[0].get("value") if content else "",
    ]

    for candidate in summary_candidates:
        summary = normalize_text(candidate, max_length=MAX_SUMMARY_LENGTH)
        if summary:
            return summary

    return ""


def extract_entry_metadata_text(entry: Any) -> str:
    parts: list[str] = []

    raw_tags = entry.get("tags")
    if isinstance(raw_tags, list):
        for raw_tag in raw_tags:
            if not isinstance(raw_tag, dict):
                continue
            for key in ("term", "label"):
                value = normalize_text(raw_tag.get(key))
                if value:
                    parts.append(value)

    for key in ("category", "keywords"):
        raw_value = entry.get(key)
        if isinstance(raw_value, list):
            parts.extend(normalized for normalized in (normalize_text(v) for v in raw_value) if normalized)
            continue

        normalized = normalize_text(raw_value)
        if normalized:
            parts.append(normalized)

    return " ".join(dict.fromkeys(parts))


def detect_tags(title: str, summary: str, metadata_text: str, rules: dict[str, list[str]]) -> list[str]:
    normalized_title = normalize_text(title).casefold()
    normalized_summary = normalize_text(summary).casefold()
    normalized_metadata = normalize_text(metadata_text).casefold()
    scored_tags: list[tuple[str, int, int, int]] = []

    for order, (tag, keywords) in enumerate(rules.items()):
        title_hits = 0
        summary_hits = 0

        for keyword in keywords:
            if text_contains_keyword(normalized_title, keyword):
                title_hits += 1
                continue

            if text_contains_keyword(normalized_summary, keyword) or text_contains_keyword(
                normalized_metadata, keyword
            ):
                summary_hits += 1

        if title_hits or summary_hits:
            scored_tags.append((tag, title_hits, summary_hits, order))

    if not scored_tags:
        return [DEFAULT_TAG]

    # タイトルに出た語を優先し、同点なら tag_rules.json の並び順（実務の緊急度順）で決める。
    scored_tags.sort(key=lambda item: (-(item[1] > 0), -item[1], -item[2], item[3], item[0]))
    return [tag for tag, _, _, _ in scored_tags[:MAX_TAGS_PER_ITEM]]


def matches_include_keywords(text: str, include_keywords: list[str]) -> bool:
    if not include_keywords:
        return True

    normalized = text.casefold()
    return any(text_contains_keyword(normalized, keyword) for keyword in include_keywords)


def calc_fallback_importance(title: str, summary: str, tags: list[str]) -> int:
    """Gemini が使えないときだけ使う近似スコア。

    ★5 は回収・緊急安全性情報などタイトルだけで確実に判る語に限る。要約の長さは薬局実務への
    近さと関係がないので加点材料にしない（長いだけの記事が★5に混ざると digest が信用を失う）。
    """
    title_text = title.casefold()

    if any(keyword.casefold() in title_text for keyword in CRITICAL_TITLE_KEYWORDS):
        return 5

    score = 3
    if any(keyword.casefold() in title_text for keyword in STRONG_TITLE_KEYWORDS):
        score += 1

    if len(summary) < SHORT_SUMMARY_THRESHOLD:
        score -= 1

    if tags == [DEFAULT_TAG]:
        score -= 1

    return max(1, min(4, score))


def build_news_item(entry: Any, feed_config: dict[str, Any], tag_rules: dict[str, list[str]]) -> NewsItem | None:
    title = normalize_text(entry.get("title"))
    link = normalize_link(entry.get("link", ""))

    if not title or not link:
        return None

    summary = extract_summary(entry)
    metadata_text = extract_entry_metadata_text(entry)

    if not matches_include_keywords(f"{title} {summary} {metadata_text}", feed_config["include_keywords"]):
        return None

    tags = detect_tags(title, summary, metadata_text, tag_rules)
    published_dt = parse_feed_datetime(entry)
    published, published_label, sort_key = format_datetime(published_dt)

    return NewsItem(
        title=title,
        link=link,
        source=feed_config["name"],
        published=published,
        published_label=published_label,
        summary=summary,
        tags=tags,
        importance=calc_fallback_importance(title, summary, tags),
        keywords=[],
        sort_key=sort_key,
        published_dt=published_dt,
    )


def build_prompt(item: NewsItem) -> str:
    content = normalize_text(item.summary, max_length=MAX_GEMINI_CONTENT_LENGTH)
    current_tags = "、".join(tag for tag in item.tags if tag) or DEFAULT_TAG
    return f"""あなたは保険薬局に勤める薬剤師のためのニュース選別エンジンです。
「明日の窓口業務・調剤・服薬指導にどれだけ効くか」だけを基準に評価してください。
研究者や製薬企業の視点ではなく、現場の薬剤師の視点で判断すること。

出力は必ずJSONのみ。Markdownや説明文は不要。

評価項目:
- category: 次の候補から最も具体的なものを1つ選ぶ: {CATEGORY_PROMPT}
- importance: 1〜5
- keywords: 記事を検索し直せる固有性の高い語を3〜5個（一般名・製品名・制度名を優先）

重要度の基準（薬局実務への直結度で決める）:
5: 今日から行動が変わる。自主回収、緊急安全性情報（イエローレター）、安全性速報（ブルーレター）、
   広く使われる薬の供給停止・販売中止、禁忌や警告の新設、法令の施行日決定
4: 近いうちに影響が出る。限定出荷・出荷調整の開始や解除、添付文書の重要な改訂、
   薬価改定・調剤報酬の変更、汎用薬の新規承認や適応拡大、疑義照会や調剤過誤の注意喚起
3: 知っておくと役立つ。一般的な承認・適応拡大、学術情報、業界動向、薬剤師向けの実務コラム
2: 背景知識どまり。総論的な解説、イベント告知、他職種向けの話題
1: 実務に無関係。広告、PR、企業の決算数値のみ、重複記事

判定の注意:
- 医師向け・研究向けの話題でも、薬局で扱う薬の話なら現場目線で読み替えて評価する
- 逆に、医療ニュースでも薬剤師が動く余地がないもの（病院経営、医師の働き方など）は2以下にする
- タイトルだけで派手に見えても、対象が極めて限られた薬なら3以下に落とす

記事:
タイトル: {item.title}
配信元: {item.source}
URL: {item.link}
現在タグ: {current_tags}
本文: {content}
"""


def build_fallback_analysis() -> dict[str, Any]:
    return {"category": FALLBACK_CATEGORY, "importance": FALLBACK_IMPORTANCE, "keywords": []}


def is_fallback_analysis(analysis: dict[str, Any] | None) -> bool:
    if not analysis:
        return True

    keywords = analysis.get("keywords")
    has_keywords = isinstance(keywords, list) and any(normalize_text(keyword) for keyword in keywords)
    return normalize_category(analysis.get("category")) == FALLBACK_CATEGORY and not has_keywords


def normalize_category(value: Any) -> str:
    category = normalize_text(value)
    if not category:
        return FALLBACK_CATEGORY

    category = CATEGORY_ALIASES.get(category, category)
    return category if category in CATEGORY_SET else FALLBACK_CATEGORY


def normalize_analysis(raw_analysis: Any) -> dict[str, Any]:
    if not isinstance(raw_analysis, dict):
        return build_fallback_analysis()

    try:
        importance = int(raw_analysis.get("importance", FALLBACK_IMPORTANCE))
    except (TypeError, ValueError):
        importance = FALLBACK_IMPORTANCE

    raw_keywords = raw_analysis.get("keywords")
    keywords: list[str] = []
    if isinstance(raw_keywords, list):
        keywords = [
            normalized for normalized in (normalize_text(k, max_length=40) for k in raw_keywords) if normalized
        ]

    return {
        "category": normalize_category(raw_analysis.get("category")),
        "importance": max(1, min(5, importance)),
        "keywords": list(dict.fromkeys(keywords))[:MAX_GEMINI_KEYWORDS],
    }


def extract_json_object_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)

    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    start = stripped.find("{")
    end = stripped.rfind("}")
    return stripped[start : end + 1] if start >= 0 and end > start else stripped


def parse_response(text: str) -> dict[str, Any]:
    return normalize_analysis(json.loads(extract_json_object_text(text)))


def load_analysis_cache(path: Path) -> dict[str, dict[str, Any]]:
    payload = load_existing_payload(path)
    raw_analyses = (payload or {}).get("analyses")
    if not isinstance(raw_analyses, dict):
        return {}

    analyses: dict[str, dict[str, Any]] = {}
    for link, raw_analysis in raw_analyses.items():
        normalized_link = normalize_link(str(link))
        if not normalized_link:
            continue

        analysis = normalize_analysis(raw_analysis)
        if is_fallback_analysis(analysis):
            continue

        analyses[normalized_link] = analysis

    return analyses


class GeminiAnalyzer:
    def __init__(self, client: Any, models: tuple[str, ...]) -> None:
        self.client = client
        self.models = models

    def analyze(self, item: NewsItem) -> dict[str, Any]:
        prompt = build_prompt(item)
        last_error: Exception | None = None

        for model in self.models:
            try:
                response = self.client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_json_schema": RESPONSE_JSON_SCHEMA,
                    },
                )
                analysis = parse_response(str(getattr(response, "text", "") or ""))
                logging.info("Analyzed with %s (★%d): %s", model, analysis["importance"], item.title)
                return analysis
            except json.JSONDecodeError as error:
                last_error = error
                logging.warning("Model %s returned invalid JSON for %s: %s", model, item.link, error)
            except Exception as error:  # noqa: BLE001
                last_error = error
                logging.warning("Model %s failed for %s: %s", model, item.link, error)

        if last_error is not None:
            logging.warning("Analysis failed for %s. Using fallback.", item.link)
        return build_fallback_analysis()


def create_analyzer() -> GeminiAnalyzer | None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        logging.info("GEMINI_API_KEY is not set. Keeping heuristic importance.")
        return None

    try:
        from google import genai
    except Exception as error:  # noqa: BLE001
        logging.warning("google-genai is not available: %s", error)
        return None

    try:
        client = genai.Client(api_key=api_key)
    except Exception as error:  # noqa: BLE001
        logging.warning("Failed to initialize Gemini client: %s", error)
        return None

    return GeminiAnalyzer(client=client, models=GEMINI_MODEL_CANDIDATES)


def build_analyses(items: list[NewsItem], cache: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    analyzer = create_analyzer()
    if analyzer is None:
        return cache

    pending = [item for item in items if item.link and is_fallback_analysis(cache.get(item.link))]
    if not pending:
        logging.info("No new articles require analysis.")
        return cache

    # 新しい記事から先に解析枠を使う。取りこぼしても次の実行で拾える。
    pending.sort(key=lambda item: item.sort_key)
    if len(pending) > MAX_GEMINI_ANALYSIS_PER_RUN:
        logging.info("Analysis targets: %d. Processing first %d this run.", len(pending), MAX_GEMINI_ANALYSIS_PER_RUN)
        pending = pending[:MAX_GEMINI_ANALYSIS_PER_RUN]

    logging.info("Analysis targets: %d", len(pending))
    for item in pending:
        try:
            analysis = analyzer.analyze(item)
            if is_fallback_analysis(analysis):
                continue
            cache[item.link] = analysis
        except Exception as error:  # noqa: BLE001
            logging.warning("Unexpected analysis error for %s: %s", item.link, error)

    return cache


def apply_analyses(items: list[NewsItem], analyses: dict[str, dict[str, Any]]) -> list[NewsItem]:
    """Gemini の category をタグ先頭に、importance と keywords をそのまま反映する。"""
    enriched: list[NewsItem] = []
    for item in items:
        analysis = analyses.get(item.link)
        if not analysis:
            enriched.append(item)
            continue

        category = normalize_category(analysis.get("category"))
        tags = [tag for tag in item.tags if tag != DEFAULT_TAG]
        if category != DEFAULT_TAG and category in tags:
            tags.remove(category)
        if category != DEFAULT_TAG:
            tags.insert(0, category)

        enriched.append(
            replace(
                item,
                tags=(tags or [DEFAULT_TAG])[:MAX_TAGS_PER_ITEM],
                importance=int(analysis.get("importance", item.importance)),
                keywords=list(analysis.get("keywords") or []),
            )
        )

    return enriched


def news_item_from_dict(item_data: dict[str, Any]) -> NewsItem | None:
    if not isinstance(item_data, dict):
        return None

    title = normalize_text(item_data.get("title"))
    link = normalize_link(str(item_data.get("link", "")))
    source = normalize_text(item_data.get("source"))
    if not title or not link or not source:
        return None

    raw_tags = item_data.get("tags")
    tags = [normalize_text(tag) for tag in raw_tags if normalize_text(tag)] if isinstance(raw_tags, list) else []

    raw_keywords = item_data.get("keywords")
    keywords = (
        [normalize_text(k) for k in raw_keywords if normalize_text(k)] if isinstance(raw_keywords, list) else []
    )

    try:
        importance = int(item_data.get("importance", 3))
    except (TypeError, ValueError):
        importance = 3

    published_dt = parse_datetime_value(item_data.get("published"))
    published, published_label, sort_key = format_datetime(published_dt)
    if not published:
        published = str(item_data.get("published", "")).strip()
    if not published_label:
        published_label = normalize_text(item_data.get("published_label"))

    return NewsItem(
        title=title,
        link=link,
        source=source,
        published=published,
        published_label=published_label,
        summary=normalize_text(item_data.get("summary"), max_length=MAX_SUMMARY_LENGTH),
        tags=tags or [DEFAULT_TAG],
        importance=max(1, min(5, importance)),
        keywords=keywords,
        sort_key=sort_key,
        published_dt=published_dt,
    )


def fetch_feed_items(feed_config: dict[str, Any], tag_rules: dict[str, list[str]]) -> tuple[list[NewsItem], bool]:
    source_name = feed_config["name"]
    logging.info("Fetching feed: %s (%s)", source_name, feed_config["url"])

    try:
        parsed_feed = feedparser.parse(fetch_feed_content(feed_config["url"]))
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        logging.error("Failed to fetch feed %s: %s", source_name, error)
        return [], False
    except Exception as error:  # noqa: BLE001
        logging.exception("Unexpected fetch error for %s: %s", source_name, error)
        return [], False

    if parsed_feed.bozo:
        logging.warning("Feed parse warning for %s: %s", source_name, parsed_feed.bozo_exception)

    items = [
        item
        for item in (
            build_news_item(entry, feed_config, tag_rules)
            for entry in parsed_feed.entries[:MAX_ITEMS_PER_FEED]
        )
        if item is not None
    ]

    logging.info("Collected %d items from %s", len(items), source_name)
    return items, True


def deduplicate_and_sort(items: list[NewsItem]) -> list[NewsItem]:
    unique_items: dict[str, NewsItem] = {}

    for item in items:
        normalized_link = item.link.casefold()
        existing = unique_items.get(normalized_link)
        if existing is None or item.sort_key < existing.sort_key:
            unique_items[normalized_link] = item

    return sorted(unique_items.values(), key=lambda item: item.sort_key)[:MAX_ITEMS_TOTAL]


def get_now_labels() -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    return (
        now_utc.isoformat().replace("+00:00", "Z"),
        now_utc.astimezone(DISPLAY_TIMEZONE).strftime("%Y/%m/%d %H:%M"),
    )


def load_existing_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None

    try:
        payload = load_json(path)
    except Exception as error:  # noqa: BLE001
        logging.warning("Failed to load existing JSON %s: %s", path, error)
        return None

    return payload if isinstance(payload, dict) else None


def load_news_items_from_payload(payload: dict[str, Any] | None) -> list[NewsItem]:
    raw_items = (payload or {}).get("items")
    if not isinstance(raw_items, list):
        return []

    return [item for item in (news_item_from_dict(raw) for raw in raw_items) if item is not None]


def extract_web_monitor_items(payload: dict[str, Any] | None) -> list[NewsItem]:
    return [item for item in load_news_items_from_payload(payload) if item.source == WEB_MONITOR_SOURCE]


def build_news_json(items: list[NewsItem], existing_payload: dict[str, Any] | None) -> dict[str, Any]:
    updated_at, updated_label = get_now_labels()
    payload = {
        "updated_at": updated_at,
        "updated_label": updated_label,
        "count": len(items),
        "items": [item.to_dict() for item in items],
    }

    # 中身が変わっていないなら更新時刻も据え置く（無意味な差分コミットを避ける）。
    if existing_payload and existing_payload.get("items") == payload["items"]:
        payload["updated_at"] = existing_payload.get("updated_at", updated_at)
        payload["updated_label"] = existing_payload.get("updated_label", updated_label)

    return payload


def build_analysis_cache_json(analyses: dict[str, dict[str, Any]], keep_links: set[str]) -> dict[str, Any]:
    """表示中の記事の解析は必ず残し、それ以外は上限まで残して古いものから捨てる。"""
    retained = {link: analysis for link, analysis in analyses.items() if link in keep_links}
    for link, analysis in analyses.items():
        if len(retained) >= CACHE_RETENTION:
            break
        retained.setdefault(link, analysis)

    updated_at, _ = get_now_labels()
    return {"updated_at": updated_at, "count": len(retained), "analyses": retained}


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def print_fetch_summary(stats: FetchStats, article_count: int) -> None:
    print(f"Fetched feeds: {stats.total_feeds}")
    print(f"Successful feeds: {stats.successful_feeds}")
    print(f"Failed feeds: {stats.failed_feeds}")
    print(f"Articles collected: {article_count}")


def main() -> int:
    setup_logging()

    try:
        feed_configs = load_feed_configs(FEEDS_PATH)
        tag_rules = load_tag_rules(TAG_RULES_PATH)
    except Exception as error:  # noqa: BLE001
        logging.exception("Failed to load configuration: %s", error)
        return 1

    all_items: list[NewsItem] = []
    stats = FetchStats(total_feeds=len(feed_configs))
    for config in feed_configs:
        items, fetched = fetch_feed_items(config, tag_rules)
        if fetched:
            stats.successful_feeds += 1
        else:
            stats.failed_feeds += 1
        all_items.extend(items)

    existing_news = load_existing_payload(NEWS_OUTPUT_PATH)
    # Web監視で拾った記事は RSS には無いので、既存の news.json から引き継ぐ。
    sorted_items = deduplicate_and_sort(all_items + extract_web_monitor_items(existing_news))
    if not sorted_items and NEWS_OUTPUT_PATH.exists() and feed_configs:
        logging.warning("No items collected. Keeping existing output files.")
        print_fetch_summary(stats, 0)
        return 1

    analyses = build_analyses(sorted_items, load_analysis_cache(ANALYSIS_CACHE_PATH))
    display_items = apply_analyses(sorted_items, analyses)

    save_json(NEWS_OUTPUT_PATH, build_news_json(display_items, existing_news))
    logging.info("Wrote %d items to %s", len(display_items), NEWS_OUTPUT_PATH)

    save_json(ANALYSIS_CACHE_PATH, build_analysis_cache_json(analyses, {item.link for item in display_items}))

    print_fetch_summary(stats, len(display_items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

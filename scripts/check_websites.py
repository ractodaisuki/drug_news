#!/usr/bin/env python3
"""RSS を持たない薬剤師向けサイトを CSS セレクタで監視し、更新を news.json へ足す。

ractodaisuki/RSS_news の check_websites.py をほぼそのまま移植したもの。
違いは、検知した記事をその場で Gemini に渡して重要度を付けるところ。元は importance=3 固定で、
次回の fetch 実行（最大3時間後）まで実際の評価が入らなかった。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from fetch_news import (
    ANALYSIS_CACHE_PATH,
    NEWS_OUTPUT_PATH,
    ROOT_DIR,
    WEB_MONITOR_SOURCE,
    NewsItem,
    apply_analyses,
    build_analyses,
    build_analysis_cache_json,
    build_news_json,
    calc_fallback_importance,
    deduplicate_and_sort,
    format_datetime,
    load_analysis_cache,
    load_existing_payload,
    load_news_items_from_payload,
    save_json,
)

WATCH_SITES_PATH = ROOT_DIR / "config" / "watch_sites.json"
WATCH_STATE_PATH = ROOT_DIR / "data" / "watch_state.json"
REQUEST_TIMEOUT = 20
USER_AGENT = "PharmaNews/1.0 (+https://github.com/ractodaisuki/pharma-news)"
DEFAULT_WATCH_TAG = "Web更新"


@dataclass
class WatchSite:
    name: str
    url: str
    selector: str
    tag: str
    selector_attribute: str = ""
    item_selector: str = ""
    title_selector: str = ""
    title_attribute: str = ""
    link_selector: str = ""
    link_attribute: str = ""
    summary_selector: str = ""
    summary_attribute: str = ""
    tags_selector: str = ""
    tags_attribute: str = ""
    emit_on_initialize: bool = False

    @property
    def state_key(self) -> str:
        parts = [
            self.url,
            self.selector,
            self.item_selector,
            self.title_selector,
            self.link_selector,
            self.summary_selector,
            self.tags_selector,
        ]
        attribute_parts = [
            self.selector_attribute,
            self.title_attribute,
            self.link_attribute,
            self.summary_attribute,
            self.tags_attribute,
        ]
        if any(attribute_parts):
            parts.extend(attribute_parts)
        return "::".join(parts)


@dataclass
class WatchSnapshot:
    text: str
    title: str
    link: str
    summary: str
    extra_tags: list[str]


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def load_watch_sites(path: Path) -> list[WatchSite]:
    configs = load_json_file(path)
    if not isinstance(configs, list):
        raise ValueError("watch_sites.json must contain a list of site definitions.")

    sites: list[WatchSite] = []
    for config in configs:
        if not isinstance(config, dict):
            logging.warning("Skipping invalid watch site config: %r", config)
            continue

        name = str(config.get("name", "")).strip()
        url = str(config.get("url", "")).strip()
        if not name or not url:
            logging.warning("Skipping watch site with missing name/url: %r", config)
            continue

        sites.append(
            WatchSite(
                name=name,
                url=url,
                selector=str(config.get("selector", "")).strip(),
                tag=str(config.get("tag", DEFAULT_WATCH_TAG)).strip() or DEFAULT_WATCH_TAG,
                selector_attribute=str(config.get("selector_attribute", "")).strip(),
                item_selector=str(config.get("item_selector", "")).strip(),
                title_selector=str(config.get("title_selector", "")).strip(),
                title_attribute=str(config.get("title_attribute", "")).strip(),
                link_selector=str(config.get("link_selector", "")).strip(),
                link_attribute=str(config.get("link_attribute", "")).strip(),
                summary_selector=str(config.get("summary_selector", "")).strip(),
                summary_attribute=str(config.get("summary_attribute", "")).strip(),
                tags_selector=str(config.get("tags_selector", "")).strip(),
                tags_attribute=str(config.get("tags_attribute", "")).strip(),
                emit_on_initialize=bool(config.get("emit_on_initialize", False)),
            )
        )

    return sites


def load_watch_state(path: Path) -> dict[str, Any]:
    default_state: dict[str, Any] = {"updated_at": "", "sites": {}}
    if not path.exists():
        return default_state

    try:
        payload = load_json_file(path)
    except Exception as error:  # noqa: BLE001
        logging.warning("Failed to load watch state %s: %s", path, error)
        return default_state

    if not isinstance(payload, dict):
        logging.warning("watch_state.json must contain an object. Resetting state.")
        return default_state

    sites = payload.get("sites")
    return {
        "updated_at": str(payload.get("updated_at", "")).strip(),
        "sites": sites if isinstance(sites, dict) else {},
    }


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def clean_node_text(node: Any) -> str:
    for tag in node.select("script, style, noscript"):
        tag.decompose()
    return normalize_text(node.get_text(" ", strip=True))


def extract_node_value(node: Any, attribute: str = "") -> str:
    if attribute:
        return normalize_text(str(node.get(attribute, ""))) if hasattr(node, "get") else ""
    return clean_node_text(node)


def get_base_node(soup: BeautifulSoup, site: WatchSite) -> Any:
    if site.item_selector:
        node = soup.select_one(site.item_selector)
        if node is None:
            raise ValueError(f"Item selector not found: {site.item_selector}")
        return node

    return soup.body or soup


def extract_selector_text(node: Any, selector: str, attribute: str = "") -> str:
    if not selector:
        return ""

    target = node.select_one(selector)
    return extract_node_value(target, attribute) if target is not None else ""


def extract_selector_link(node: Any, selector: str, base_url: str, attribute: str = "") -> str:
    if not selector:
        return ""

    target = node.select_one(selector)
    if target is None:
        return ""

    href = target.get(attribute or "href") if hasattr(target, "get") else ""
    href_text = normalize_text(href or "")
    return urljoin(base_url, href_text) if href_text else ""


def extract_selector_tags(node: Any, selector: str, attribute: str = "") -> list[str]:
    if not selector:
        return []

    tags = [extract_node_value(target, attribute) for target in node.select(selector)]
    return list(dict.fromkeys(tag for tag in tags if tag))


def fetch_site_snapshot(site: WatchSite) -> WatchSnapshot:
    response = requests.get(site.url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    base_node = get_base_node(soup, site)

    if site.selector:
        nodes = base_node.select(site.selector) if site.item_selector else soup.select(site.selector)
        if not nodes:
            raise ValueError(f"Selector not found: {site.selector}")
        text = " ".join(extract_node_value(node, site.selector_attribute) for node in nodes)
    else:
        text = clean_node_text(base_node)

    normalized = normalize_text(text)
    if not normalized:
        raise ValueError("No text content extracted")

    metadata_node = base_node if site.item_selector else soup
    return WatchSnapshot(
        text=normalized,
        title=extract_selector_text(metadata_node, site.title_selector, site.title_attribute),
        link=extract_selector_link(metadata_node, site.link_selector, site.url, site.link_attribute),
        summary=extract_selector_text(metadata_node, site.summary_selector, site.summary_attribute),
        extra_tags=extract_selector_tags(metadata_node, site.tags_selector, site.tags_attribute),
    )


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_watch_item(site: WatchSite, detected_at: datetime, snapshot: WatchSnapshot) -> NewsItem:
    published, published_label, sort_key = format_datetime(detected_at)

    tags = [site.tag] if site.tag else []
    tags.extend(snapshot.extra_tags)
    tags = list(dict.fromkeys(tag for tag in tags if tag)) or [DEFAULT_WATCH_TAG]

    title = snapshot.title or f"{site.name} が更新されました"
    if snapshot.summary:
        summary = snapshot.summary
    elif snapshot.title:
        summary = f"{site.name}の新着記事を検知しました。"
    else:
        summary = "RSSがないサイトの更新を検知しました。"

    return NewsItem(
        title=title,
        link=snapshot.link or site.url,
        source=WEB_MONITOR_SOURCE,
        published=published,
        published_label=published_label,
        summary=summary,
        tags=tags,
        importance=calc_fallback_importance(title, summary, tags),
        keywords=[],
        sort_key=sort_key,
        published_dt=detected_at,
    )


def update_watch_state(
    sites: list[WatchSite],
    previous_state: dict[str, Any],
) -> tuple[list[NewsItem], dict[str, Any]]:
    previous_sites = previous_state.get("sites", {})
    next_sites: dict[str, Any] = {}
    detected_items: list[NewsItem] = []

    for site in sites:
        checked_at = datetime.now(timezone.utc)
        checked_at_iso = checked_at.isoformat().replace("+00:00", "Z")
        previous_site_state = previous_sites.get(site.state_key)
        if not isinstance(previous_site_state, dict):
            previous_site_state = {}

        try:
            snapshot = fetch_site_snapshot(site)
            content_hash = hash_text(snapshot.text)
        except (requests.RequestException, ValueError) as error:
            logging.warning("Failed to check %s (%s): %s", site.name, site.url, error)
            if previous_site_state:
                next_sites[site.state_key] = previous_site_state
            continue
        except Exception as error:  # noqa: BLE001
            logging.warning("Unexpected error while checking %s (%s): %s", site.name, site.url, error)
            if previous_site_state:
                next_sites[site.state_key] = previous_site_state
            continue

        previous_hash = str(previous_site_state.get("hash", "")).strip()
        next_site_state = {
            "name": site.name,
            "url": site.url,
            "tag": site.tag,
            "hash": content_hash,
            "last_checked_at": checked_at_iso,
            "last_changed_at": previous_site_state.get("last_changed_at", ""),
        }

        if not previous_hash:
            logging.info("Initialized watch state for %s", site.name)
            if site.emit_on_initialize:
                next_site_state["last_changed_at"] = checked_at_iso
                detected_items.append(build_watch_item(site, checked_at, snapshot))
                logging.info("Detected initial item for %s", site.name)
        elif previous_hash != content_hash:
            next_site_state["last_changed_at"] = checked_at_iso
            detected_items.append(build_watch_item(site, checked_at, snapshot))
            logging.info("Detected update for %s", site.name)

        next_sites[site.state_key] = next_site_state

    return detected_items, {
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sites": next_sites,
    }


def main() -> int:
    setup_logging()

    try:
        watch_sites = load_watch_sites(WATCH_SITES_PATH)
    except Exception as error:  # noqa: BLE001
        logging.exception("Failed to load watch site configuration: %s", error)
        return 1

    previous_news_payload = load_existing_payload(NEWS_OUTPUT_PATH)
    existing_items = load_news_items_from_payload(previous_news_payload)
    detected_items, next_watch_state = update_watch_state(watch_sites, load_watch_state(WATCH_STATE_PATH))

    merged_items = deduplicate_and_sort(existing_items + detected_items)

    # 検知したものだけ解析する。既存記事は fetch 側で解析済み。
    analyses = load_analysis_cache(ANALYSIS_CACHE_PATH)
    if detected_items:
        analyses = build_analyses(detected_items, analyses)
    display_items = apply_analyses(merged_items, analyses)

    save_json(NEWS_OUTPUT_PATH, build_news_json(display_items, previous_news_payload))
    logging.info("Wrote %d items to %s", len(display_items), NEWS_OUTPUT_PATH)

    save_json(ANALYSIS_CACHE_PATH, build_analysis_cache_json(analyses, {item.link for item in display_items}))
    save_json(WATCH_STATE_PATH, next_watch_state)
    logging.info("Detected website updates: %d", len(detected_items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

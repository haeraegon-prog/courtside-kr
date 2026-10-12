#!/usr/bin/env python3
"""NBA/G League news ingestion. Google News RSS + ESPN RSS -> Korean briefs -> static JSON.

Designed to run from GitHub Actions at the Korean 09:00 cutoff. If upstream
sources are down, previous data remains; failures are surfaced in warnings.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as dt_time, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data/news.json"
KST = ZoneInfo("Asia/Seoul")
HEADERS = {"User-Agent": "CourtsideKR/1.0 (personal news reader; public RSS feeds)"}
CATEGORIES = ["계약·이적", "부상", "경기·기록", "분석·전망", "리그 운영", "기타"]

# NBA 30 clubs, English and Korean aliases. Always match phrase boundaries for
# short city acronyms to avoid accidentally attaching teams to unrelated texts.
TEAM_ALIASES = {
    "ATL": ["atlanta hawks", "hawks", "애틀랜타", "호크스"],
    "BOS": ["boston celtics", "celtics", "보스턴", "셀틱스"],
    "BKN": ["brooklyn nets", "nets", "브루클린", "네츠"],
    "CHA": ["charlotte hornets", "hornets", "샬럿", "호네츠"],
    "CHI": ["chicago bulls", "bulls", "시카고", "불스"],
    "CLE": ["cleveland cavaliers", "cavaliers", "cavs", "클리블랜드", "캐벌리어스"],
    "DAL": ["dallas mavericks", "mavericks", "mavs", "댈러스", "매버릭스"],
    "DEN": ["denver nuggets", "nuggets", "덴버", "너기츠"],
    "DET": ["detroit pistons", "pistons", "디트로이트", "피스턴스"],
    "GSW": ["golden state warriors", "warriors", "골든스테이트", "워리어스"],
    "HOU": ["houston rockets", "rockets", "휴스턴", "로키츠"],
    "IND": ["indiana pacers", "pacers", "인디애나", "페이서스"],
    "LAC": ["los angeles clippers", "la clippers", "clippers", "클리퍼스"],
    "LAL": ["los angeles lakers", "la lakers", "lakers", "레이커스"],
    "MEM": ["memphis grizzlies", "grizzlies", "멤피스", "그리즐리스"],
    "MIA": ["miami heat", "heat", "마이애미"],
    "MIL": ["milwaukee bucks", "bucks", "밀워키", "벅스"],
    "MIN": ["minnesota timberwolves", "timberwolves", "wolves", "미네소타", "팀버울브스"],
    "NOP": ["new orleans pelicans", "pelicans", "뉴올리언스", "펠리컨스"],
    "NYK": ["new york knicks", "knicks", "뉴욕", "닉스"],
    "OKC": ["oklahoma city thunder", "thunder", "오클라호마시티", "선더"],
    "ORL": ["orlando magic", "magic", "올랜도", "매직"],
    "PHI": ["philadelphia 76ers", "sixers", "76ers", "필라델피아", "식서스"],
    "PHX": ["phoenix suns", "suns", "피닉스", "선스"],
    "POR": ["portland trail blazers", "trail blazers", "blazers", "포틀랜드", "블레이저스"],
    "SAC": ["sacramento kings", "kings", "새크라멘토", "킹스"],
    "SAS": ["san antonio spurs", "spurs", "샌안토니오", "스퍼스"],
    "TOR": ["toronto raptors", "raptors", "토론토", "랩터스"],
    "UTA": ["utah jazz", "jazz", "유타", "재즈"],
    "WAS": ["washington wizards", "wizards", "워싱턴", "위저즈"],
}

@dataclass
class Feed:
    url: str
    section: str
    source: str
    priority: int


def feeds() -> list[Feed]:
    queries = [
        ("NBA basketball player injury trade contract preseason", "nba", "Google News", 2, "en-US", "US", "US:en"),
        ("site:nba.com/news NBA", "nba", "NBA.com 검색", 1, "en-US", "US", "US:en"),
        ("NBA G League two-way contract roster", "gleague", "G리그 검색", 3, "en-US", "US", "US:en"),
        ("site:gleague.nba.com/news", "gleague", "NBA G리그 검색", 2, "en-US", "US", "US:en"),
        ('"이현중" (NBA OR G리그 OR 포틀랜드)', "lee", "Google 뉴스(한국)", 0, "ko", "KR", "KR:ko"),
        ('"Hyunjung Lee" OR "Hyun-jung Lee" basketball G League', "lee", "Google News", 0, "en-US", "US", "US:en"),
    ]
    result = [Feed("https://www.espn.com/espn/rss/nba/news", "nba", "ESPN", 1)]
    for query, section, source, priority, hl, gl, ceid in queries:
        url = f"https://news.google.com/rss/search?q={quote(query)}&hl={hl}&gl={gl}&ceid={ceid}"
        result.append(Feed(url, section, source, priority))
    return result


def cutoff_for(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(KST)
    cutoff = local.replace(hour=9, minute=0, second=0, microsecond=0)
    if local < cutoff:
        cutoff -= timedelta(days=1)
    return cutoff - timedelta(days=1), cutoff


def clean_html(value: str) -> str:
    text = BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()[:1400]


def find_teams(text: str) -> list[str]:
    original = text
    text = text.lower()
    ambiguous = {"heat", "jazz", "magic", "kings", "nets", "suns", "thunder"}
    matched = []
    for abbr, names in TEAM_ALIASES.items():
        def alias_found(name):
            if name in ambiguous:
                return bool(re.search(r"(?<![A-Za-z])" + re.escape(name.title()) + r"(?![A-Za-z])", original))
            return bool(re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", text))
        if any(alias_found(name) for name in names):
            matched.append(abbr)
    return matched[:5]


def categorize(text: str) -> str:
    t = text.lower()
    if any(x in t for x in ("injur", "ankle", "knee", "surgery", "부상", "염좌", "결장")):
        return "부상"
    if any(x in t for x in ("sign", "trade", "deal", "contract", "waive", "extend", "계약", "이적", "트레이드", "방출")):
        return "계약·이적"
    if any(x in t for x in ("win", "score", "points", "game", "defeat", "기록", "득점", "승리")):
        return "경기·기록"
    if any(x in t for x in ("schedule", "draft", "rules", "policy", "일정", "규정", "드래프트")):
        return "리그 운영"
    if any(x in t for x in ("preview", "ranking", "analysis", "season", "전망", "분석")):
        return "분석·전망"
    return "기타"


def normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", re.sub(r" - [^-]+$", "", value.lower()))[:100]


def article_id(url: str, title: str) -> str:
    return hashlib.sha256((normalized_title(title) + "|" + urlparse(url).path).encode()).hexdigest()[:20]


def is_lee(text: str) -> bool:
    return bool(re.search(r"이현중|hyun\s*[- ]?jung\s+lee|hyunjung\s+lee|lee\s+hyun\s*[- ]?jung", text, re.I))


def to_utc(entry: dict) -> datetime | None:
    parsed = entry.get("published") or entry.get("updated")
    if parsed:
        try:
            dt = parsedate_to_datetime(parsed)
            # An RSS timestamp without a zone is ambiguous. Dropping it is safer
            # than silently treating a publisher-local time as UTC.
            return dt.astimezone(timezone.utc) if dt.tzinfo else None
        except (ValueError, TypeError):
            try:
                dt = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
                return dt.astimezone(timezone.utc) if dt.tzinfo else None
            except ValueError:
                return None
    return None


def published_kst_date(value: str) -> date:
    """Return the date shown to readers, respecting an explicit article offset."""
    parsed = datetime.fromisoformat(value)
    if len(value) == 10:  # Seed articles may retain a publisher date without a time.
        return parsed.date()
    if parsed.tzinfo is None:
        raise ValueError("Article timestamp has no timezone")
    return parsed.astimezone(KST).date()


def parse_rss(content: bytes) -> list[dict]:
    """Small, dependency-free RSS / Atom parser for public syndication feeds."""
    root = ET.fromstring(content)
    entries = []
    items = root.findall(".//item")
    if not items:
        items = root.findall("{http://www.w3.org/2005/Atom}entry")
    for item in items:
        def extract(*tags):
            for tag in tags:
                result = item.find(tag)
                if result is not None:
                    return "".join(result.itertext()).strip()
            return ""
        link = extract("link")
        if not link:
            link_node = item.find("{http://www.w3.org/2005/Atom}link")
            link = link_node.get("href", "") if link_node is not None else ""
        entries.append({
            "title": extract("title", "{http://www.w3.org/2005/Atom}title"),
            "link": link,
            "summary": extract("description", "{http://www.w3.org/2005/Atom}summary", "{http://purl.org/rss/1.0/modules/content/}encoded"),
            "published": extract("pubDate", "{http://www.w3.org/2005/Atom}published", "{http://purl.org/dc/elements/1.1/}date"),
            "updated": extract("{http://www.w3.org/2005/Atom}updated"),
            "source": extract("source"),
        })
    return entries


def is_domain_or_subdomain(hostname: str, domain: str) -> bool:
    host = (hostname or "").lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def fetch_feed(feed: Feed, session: requests.Session) -> list[dict]:
    response = session.get(feed.url, timeout=18, headers=HEADERS)
    response.raise_for_status()
    entries = parse_rss(response.content)
    articles = []
    for entry in entries[:50]:
        title = clean_html(entry.get("title", ""))
        link = entry.get("link", "")
        published = to_utc(entry)
        if not (title and link and published):
            continue
        source = feed.source
        if entry.get("source"):
            source = str(entry["source"])[:65]
        excerpt = clean_html(entry.get("summary", ""))
        actual_section = "lee" if is_lee(title + " " + excerpt) else feed.section
        # A broad Google query sometimes yields an unrelated person named Lee;
        # require positive identification for the dedicated player feed.
        if feed.section == "lee" and actual_section != "lee":
            continue
        verified_domain = urlparse(link).hostname or ""
        verification = "official" if (
            is_domain_or_subdomain(verified_domain, "nba.com")
            or is_domain_or_subdomain(verified_domain, "gleague.nba.com")
        ) else "reported"
        if re.search(r"rumor|reportedly|sources say|루머|소문|전망", title, re.I):
            verification = "unconfirmed"
        articles.append({
            "id": article_id(link, title), "section": actual_section,
            "source": source, "title_original": title, "url": link,
            "published_at": published.isoformat(timespec="seconds"), "published_precision": "minute",
            "teams": find_teams(title + " " + excerpt),
            "category": categorize(title + " " + excerpt),
            "verification": verification, "excerpt": excerpt,
            "priority": feed.priority,
        })
    return articles


def full_article_excerpt(url: str, session: requests.Session) -> tuple[str, str]:
    """Try publisher article text only where the feed uses a direct link.

    Google News links are aggregator URLs: we never claim to have read the
    underlying article when we could only see a headline/snippet.
    """
    hostname = urlparse(url).hostname or ""
    if not any(is_domain_or_subdomain(hostname, domain) for domain in ("nba.com", "espn.com")):
        return "", "rss_excerpt"
    try:
        response = session.get(url, timeout=11, headers=HEADERS)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for node in soup.select('script,style,header,footer,nav,aside'):
            node.decompose()
        article = soup.find("article")
        if not article:
            return "", "rss_excerpt"
        parts = [p.get_text(" ", strip=True) for p in article.select("p")]
        parts = [p for p in parts if len(p) > 28]
        content = " ".join(parts)[:4600]
        return (content, "publisher_article") if len(content) >= 250 else ("", "rss_excerpt")
    except requests.RequestException:
        return "", "rss_excerpt"


def translate(article: dict, session: requests.Session, client) -> dict:
    body, basis = full_article_excerpt(article["url"], session)
    source_text = body if body else article.get("excerpt", "")
    article["source_basis"] = basis if body else "rss_excerpt"
    if client is None:
        article["title_ko"] = article["title_original"] if is_korean(article["title_original"]) else None
        article["summary_ko"] = clean_html(article["excerpt"]) if is_korean(article["excerpt"]) else None
        article["why_it_matters"] = None
        return article
    prompt = {
        "title": article["title_original"],
        "publisher": article["source"], "date": article["published_at"],
        "source_basis": article["source_basis"], "source_text": source_text[:4600],
        "section": article["section"], "existing_category": article["category"],
        "teams_from_keyword_matching": article["teams"],
    }
    instruction = (
        "너는 엄밀한 NBA 한국어 뉴스 편집자다. 제공된 제목과 기사 텍스트만 사용해 사실관계를 왜곡하지 않고 한국어로 요약하라. "
        "자료가 RSS 요약문이라면 전문을 읽은 것처럼 서술하지 말고 주어진 사실만 간결하게 번역하라. "
        "중요한 세부내용이나 통계가 없는 경우 추정해서 채우지 말라. "
        "기사 내용을 길게 인용하거나 원문 전문 번역을 하지 말라. "
        "루머/보도/공식 발표의 확인 상태를 분명하게 구분하고, 다루지 않은 미래 결과를 예측하지 말라. "
        "한국어 제목과 최대 3~4문장의 요약, 중요성 한 문장, 분야 이름을 JSON 객체로 제공. "
        "JSON 키: title_ko, summary_ko, why_it_matters, category. "
        "category는 다음 중 하나만: " + ", ".join(CATEGORIES)
    )
    try:
        result = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": instruction},
                      {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
            temperature=0,
            timeout=30,
        )
        content = json.loads(result.choices[0].message.content)
        for key in ("title_ko", "summary_ko", "why_it_matters"):
            value = content.get(key)
            article[key] = value.strip()[:900] if isinstance(value, str) else None
        if content.get("category") in CATEGORIES:
            article["category"] = content["category"]
    except Exception as exc:
        print(f"Translation failed ({article['title_original'][:40]}): {exc}", file=sys.stderr)
        article["title_ko"] = article["title_original"] if is_korean(article["title_original"]) else None
        article["summary_ko"] = article["excerpt"] if is_korean(article["excerpt"]) else None
        article["why_it_matters"] = None
    return article


def is_korean(text: str) -> bool:
    return bool(re.search("[가-힣]", text or ""))


def load_previous() -> dict:
    try:
        return json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"articles": []}


def build(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    window_start, cutoff = cutoff_for(now)
    previous = load_previous()
    session = requests.Session()
    warnings = []
    candidates = []
    successes = 0
    for feed in feeds():
        try:
            entries = fetch_feed(feed, session)
            candidates.extend(entries)
            successes += 1
            print(f"[OK] {feed.source}/{feed.section}: {len(entries)}")
        except Exception as exc:
            warnings.append(f"{feed.source}({feed.section}) 피드 조회 실패")
            print(f"[WARN] {feed.url}: {exc}", file=sys.stderr)
    if successes == 0:
        return {**previous, "warnings": ["모든 뉴스 피드 조회에 실패했습니다. 이전 데이터가 유지됩니다."]}
    # The current edition is a fixed KST 09:00-to-09:00 interval.
    recent = [a for a in candidates if window_start <= datetime.fromisoformat(a["published_at"]).astimezone(KST) < cutoff]
    recent.sort(key=lambda a: (a["priority"], -datetime.fromisoformat(a["published_at"]).timestamp()))
    unique_titles, unique_urls, unique = set(), set(), []
    for item in recent:
        key = normalized_title(item["title_original"])
        if key in unique_titles or item["url"] in unique_urls:
            continue
        unique_titles.add(key)
        unique_urls.add(item["url"])
        unique.append(item)
    # Include up to 8 NBA, 3 general G League, 5 player-watch updates.
    selected = []
    for section, max_count in (("lee", 5), ("nba", 8), ("gleague", 3)):
        selected.extend([a for a in unique if a["section"] == section][:max_count])
    previous_articles = {a["id"]: a for a in previous.get("articles", []) if a.get("id")}
    client = None
    if os.getenv("OPENAI_API_KEY"):
        try:
            from openai import OpenAI
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        except Exception as exc:
            warnings.append("한국어 번역 서비스 초기화 실패")
            print(exc, file=sys.stderr)
    else:
        warnings.append("OPENAI_API_KEY 미설정: 새 영문 기사 한국어 번역 불가")
    enriched = []
    for item in selected:
        if item["id"] in previous_articles:
            old = previous_articles[item["id"]]
            enriched.append(old)
            continue
        enriched.append(translate(item, session, client))
        time.sleep(0.15)
    # The historic articles, unlike this run, may be originally manually seeded.
    latest = {a["id"]: a for a in previous.get("articles", []) if a.get("id")}
    latest.update({a["id"]: a for a in enriched})
    thirty_five_days_ago = (cutoff - timedelta(days=35)).date()
    history = []
    for item in latest.values():
        try:
            day = published_kst_date(item["published_at"])
            if day < thirty_five_days_ago:
                continue
        except (ValueError, TypeError, KeyError):
            continue
        item = dict(item)
        item.pop("excerpt", None)
        item.pop("priority", None)
        history.append(item)
    history.sort(key=lambda a: str(a["published_at"]), reverse=True)
    return {
        "schema_version": 1, "mode": "automatic", "generated_at": now.isoformat(timespec="seconds"),
        "window_start": window_start.isoformat(timespec="seconds"),
        "cutoff_at": cutoff.isoformat(timespec="seconds"),
        "warnings": warnings, "articles": history[:400]
    }


def main():
    result = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(result['articles'])} articles -> {OUTPUT}")
    if result.get("warnings"):
        print("Warnings:", *result["warnings"], sep="\n  - ")


if __name__ == "__main__":
    main()

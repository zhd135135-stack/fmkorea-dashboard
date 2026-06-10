#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
에펨코리아 FC온라인 게시판 크롤러
- 브라우저 쿠키 기반 Cloudflare 우회
- 전체게시판 + FSL/프로게이머 탭 수집
- Claude API로 감성분석
- data_all.json / data_fsl.json 저장
"""

import json
import os
import re
import time
from datetime import datetime, timezone, timedelta

import requests
import urllib3
from bs4 import BeautifulSoup
import anthropic

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

KST = timezone(timedelta(hours=9))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FMKOREA_COOKIE = os.environ.get("FMKOREA_COOKIE", "")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

TARGETS = {
    "all": {
        "url": "https://www.fmkorea.com/fifa_online",
        "output": "data_all.json"
    },
    "fsl": {
        "url": "https://www.fmkorea.com/index.php?mid=fifa_online&category=8064047289",
        "output": "data_fsl.json"
    }
}

def make_session(cookie_str):
    session = requests.Session()
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": "https://www.fmkorea.com/",
        "Cache-Control": "no-cache"
    })
    if cookie_str:
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                session.cookies.set(k.strip(), v.strip(), domain=".fmkorea.com")
    return session


def parse_posts(html):
    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for li in soup.select("li.li_post"):
        title_el = li.select_one(".title a")
        if not title_el:
            continue

        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        url = "https://www.fmkorea.com" + href if href.startswith("/") else href

        view_el = li.select_one(".m_no") or li.select_one(".count")
        views = 0
        if view_el:
            v_match = re.search(r"[\d,]+", view_el.get_text(strip=True))
            if v_match:
                views = int(v_match.group().replace(",", ""))

        comment_el = li.select_one(".replyCount") or li.select_one(".reply_num")
        comments = 0
        if comment_el:
            c_match = re.search(r"\d+", comment_el.get_text(strip=True))
            if c_match:
                comments = int(c_match.group())

        date_el = li.select_one(".regdate") or li.select_one(".date")
        date = date_el.get_text(strip=True) if date_el else ""

        if len(title) > 2:
            posts.append({
                "title": title,
                "url": url,
                "views": views,
                "comments": comments,
                "date": date,
                "sentiment": "pending"
            })

    total_match = re.search(r"총\s*([\d,]+)\s*개", html)
    total_posts = int(total_match.group(1).replace(",", "")) if total_match else len(posts)

    return posts, total_posts


def analyze_sentiment(posts, source):
    if not posts or not ANTHROPIC_API_KEY:
        return {
            "sentiments": ["neutral"] * len(posts),
            "sentiment_summary": {"positive": 33, "neutral": 34, "negative": 33},
            "top_issues": {"positive": "분석 불가", "negative": "분석 불가", "neutral": "분석 불가"},
            "keywords": [],
            "churn_signals": [],
            "impact_score": 5.0,
            "fsl_mention_rate": "0%",
            "viewer_engagement_estimate": "0K"
        }

    sample = posts[:30]
    titles_text = "\n".join(f"{i+1}. {p['title']}" for i, p in enumerate(sample))

    if source == "fsl":
        prompt = f"""당신은 FC온라인 이스포츠(FSL) 커뮤니티 분석 전문가입니다.
아래 FSL/프로게이머 관련 게시글 제목들을 분석하여 JSON만 응답하세요. 다른 텍스트 없이 순수 JSON만.

게시글 제목:
{titles_text}

응답 형식:
{{
  "sentiments": ["positive"|"neutral"|"negative", ...],
  "sentiment_summary": {{ "positive": 숫자(%), "neutral": 숫자(%), "negative": 숫자(%) }},
  "top_issues": {{
    "positive": "긍정 이슈 핵심 한 줄 (20자 이내)",
    "negative": "부정 이슈 핵심 한 줄 (20자 이내)",
    "neutral": "중립 이슈 핵심 한 줄 (20자 이내)"
  }},
  "keywords": [
    {{ "topic": "키워드명", "count": 추정언급수, "pct": "X.X%", "tags": "관련 서브키워드들" }}
  ],
  "esports_disengagement_signals": ["이스포츠 이탈/무관심 신호1", "신호2"],
  "fsl_mention_rate": "X.X%",
  "impact_score": 5.0,
  "viewer_engagement_estimate": "XXK"
}}"""
    else:
        prompt = f"""당신은 FC온라인 커뮤니티 분석 전문가입니다.
아래 게시글 제목들을 분석하여 JSON만 응답하세요. 다른 텍스트 없이 순수 JSON만.

게시글 제목:
{titles_text}

응답 형식:
{{
  "sentiments": ["positive"|"neutral"|"negative", ...],
  "sentiment_summary": {{ "positive": 숫자(%), "neutral": 숫자(%), "negative": 숫자(%) }},
  "top_issues": {{
    "positive": "긍정 이슈 핵심 한 줄 (20자 이내)",
    "negative": "부정 이슈 핵심 한 줄 (20자 이내)",
    "neutral": "중립 이슈 핵심 한 줄 (20자 이내)"
  }},
  "keywords": [
    {{ "topic": "키워드명", "count": 추정언급수, "pct": "X.X%", "tags": "관련 서브키워드들" }}
  ],
  "churn_signals": ["이탈/불만 신호 문장1", "문장2"],
  "impact_score": 5.0
}}"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"JSON 파싱 실패: {raw[:200]}")
        return {
            "sentiments": ["neutral"] * len(sample),
            "sentiment_summary": {"positive": 33, "neutral": 34, "negative": 33},
            "top_issues": {"positive": "파싱 실패", "negative": "파싱 실패", "neutral": "파싱 실패"},
            "keywords": [],
            "churn_signals": [],
            "impact_score": 5.0
        }


def main():
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    print(f"크롤링 시작: {now_kst}")

    if not FMKOREA_COOKIE:
        print("경고: FMKOREA_COOKIE 환경변수가 없습니다.")

    session = make_session(FMKOREA_COOKIE)

    for source, config in TARGETS.items():
        url = config["url"]
        output = config["output"]

        print(f"\n[{source.upper()}] 크롤링 중: {url}")

        try:
            resp = session.get(url, timeout=30, verify=False)
            print(f"응답 코드: {resp.status_code}")

            if resp.status_code != 200:
                print(f"실패: {resp.status_code}")
                continue

            html = resp.text

            if "에펨코리아 보안 시스템" in html or "cf-turnstile" in html:
                print("Cloudflare 챌린지 감지 - 쿠키를 갱신해야 합니다.")
                continue

        except Exception as e:
            print(f"크롤링 실패: {e}")
            continue

        posts, total_posts = parse_posts(html)
        print(f"파싱된 게시글: {len(posts)}개 / 총: {total_posts}개")

        if not posts:
            print("게시글 없음 - 스킵")
            continue

        total_views = sum(p["views"] for p in posts)
        avg_views = total_views // len(posts) if posts else 0

        print(f"Claude 감성분석 중...")
        analysis = analyze_sentiment(posts, source)

        sentiments = analysis.get("sentiments", [])
        for i, post in enumerate(posts):
            post["sentiment"] = sentiments[i] if i < len(sentiments) else "neutral"

        data = {
            "source": source,
            "total_posts": total_posts,
            "total_views": total_views,
            "avg_views_per_post": avg_views,
            "posts": posts,
            "sentiment": analysis.get("sentiment_summary", {"positive": 33, "neutral": 34, "negative": 33}),
            "top_issues": analysis.get("top_issues", {}),
            "keywords": analysis.get("keywords", []),
            "churn_signals": analysis.get("churn_signals", []),
            "esports_disengagement_signals": analysis.get("esports_disengagement_signals", []),
            "fsl_mention_rate": analysis.get("fsl_mention_rate", "0%"),
            "impact_score": analysis.get("impact_score", 5.0),
            "viewer_engagement_estimate": analysis.get("viewer_engagement_estimate", "0K"),
            "last_updated": now_kst
        }

        with open(output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"{output} 저장 완료")
        time.sleep(2)

    print("\n크롤링 완료!")


if __name__ == "__main__":
    main()

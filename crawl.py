#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
에펨코리아 FC온라인 게시판 크롤러
- 브라우저 쿠키 기반 Cloudflare 우회
- 전체게시판 + FSL/프로게이머 탭 수집
- 전날 10:01 ~ 당일 09:59 범위 데이터 수집
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

MAX_PAGES = 150  # 최대 150페이지 = 최대 3,000개


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


def parse_date(date_str, now_kst):
    """
    날짜 문자열을 datetime으로 변환
    - "10:59" → 오늘 날짜 + 시:분
    - "06.09" → 올해 월.일
    - "2025.06.09" → 연.월.일
    """
    date_str = date_str.strip()

    # 시:분 형식 (오늘 글)
    if re.match(r'^\d{1,2}:\d{2}$', date_str):
        h, m = map(int, date_str.split(":"))
        return now_kst.replace(hour=h, minute=m, second=0, microsecond=0)

    # 월.일 형식
    if re.match(r'^\d{2}\.\d{2}$', date_str):
        month, day = map(int, date_str.split("."))
        year = now_kst.year
        try:
            return datetime(year, month, day, tzinfo=KST)
        except:
            return None

    # 연.월.일 형식
    if re.match(r'^\d{4}\.\d{2}\.\d{2}$', date_str):
        year, month, day = map(int, date_str.split("."))
        try:
            return datetime(year, month, day, tzinfo=KST)
        except:
            return None

    return None


def is_in_range(post_dt, start_dt, end_dt):
    if post_dt is None:
        return False
    return start_dt <= post_dt <= end_dt


def parse_posts_from_html(html, now_kst, start_dt, end_dt):
    """HTML에서 게시글 파싱, 날짜 범위 필터링"""
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    out_of_range_count = 0

    for tr in soup.select("table.bd_lst tbody tr"):
        # 공지글 스킵
        if "notice" in tr.get("class", []):
            continue

        title_el = tr.select_one("td.title a")
        if not title_el:
            continue

        title = title_el.get_text(strip=True)
        if len(title) <= 2:
            continue

        href = title_el.get("href", "")
        url = "https://www.fmkorea.com" + href if href.startswith("/") else href

        # 날짜
        date_el = tr.select_one("td.time")
        date_str = date_el.get_text(strip=True) if date_el else ""
        post_dt = parse_date(date_str, now_kst)

        # 범위 밖이면 카운트
        if post_dt and post_dt < start_dt:
            out_of_range_count += 1

        # 범위 내 글만 수집
        if not is_in_range(post_dt, start_dt, end_dt):
            continue

        # 조회수
        views = 0
        m_no_els = tr.select("td.m_no")
        for el in m_no_els:
            if "voted" in el.get("class", []):
                continue
            txt = el.get_text(strip=True)
            # "10만" → 100000
            if "만" in txt:
                num = re.search(r"[\d.]+", txt)
                if num:
                    views = int(float(num.group()) * 10000)
            else:
                num = re.search(r"[\d,]+", txt)
                if num:
                    views = int(num.group().replace(",", ""))
            if views > 0:
                break

        # 댓글수
        comments = 0
        reply_el = tr.select_one(".replyNum")
        if reply_el:
            c = re.search(r"\d+", reply_el.get_text(strip=True))
            if c:
                comments = int(c.group())

        # 카테고리
        cate_el = tr.select_one("td.cate")
        category = cate_el.get_text(strip=True) if cate_el else ""

        posts.append({
            "title": title,
            "url": url,
            "views": views,
            "comments": comments,
            "date": date_str,
            "category": category,
            "sentiment": "pending"
        })

    return posts, out_of_range_count


def crawl_with_date_range(session, base_url, source, start_dt, end_dt, now_kst):
    """날짜 범위 기반 페이지 순회 크롤링"""
    all_posts = []

    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = base_url
        else:
            sep = "&" if "?" in base_url else "?"
            url = f"{base_url}{sep}page={page}"

        print(f"  [{source.upper()}] 페이지 {page} 크롤링: {url}")

        try:
            resp = session.get(url, timeout=30, verify=False)
            print(f"  응답 코드: {resp.status_code}")

            if resp.status_code != 200:
                print(f"  실패: {resp.status_code}")
                break

            html = resp.text

            if "에펨코리아 보안 시스템" in html or "cf-turnstile" in html:
                print("  Cloudflare 챌린지 감지 - 쿠키를 갱신해야 합니다.")
                break

        except Exception as e:
            print(f"  크롤링 실패: {e}")
            break

        posts, out_of_range_count = parse_posts_from_html(html, now_kst, start_dt, end_dt)
        all_posts.extend(posts)
        print(f"  수집된 게시글: {len(posts)}개 (누적: {len(all_posts)}개)")

        # 범위 밖 글이 5개 이상이면 더 이전 페이지는 수집 불필요
        if out_of_range_count >= 5:
            print(f"  범위 이전 글 {out_of_range_count}개 감지 → 수집 종료")
            break

        time.sleep(1)

    return all_posts


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

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    all_sentiments = []
    last_result = None

    # 100개씩 배치 처리
    for batch_start in range(0, len(posts), 100):
        batch = posts[batch_start:batch_start + 100]
        titles_text = "\n".join(f"{i+1}. {p['title']}" for i, p in enumerate(batch))

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

        try:
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = message.content[0].text.strip()
            raw = re.sub(r"```json|```", "", raw).strip()
            batch_result = json.loads(raw)
            batch_sents = batch_result.get("sentiments", ["neutral"] * len(batch))
            all_sentiments.extend(batch_sents[:len(batch)])
            last_result = batch_result
            print(f"  배치 {batch_start//100+1} 분석 완료 ({len(batch)}개)")
        except Exception as e:
            print(f"  배치 {batch_start//100+1} 분석 실패: {e}")
            all_sentiments.extend(["neutral"] * len(batch))

    if last_result:
        last_result["sentiments"] = all_sentiments
        # sentiment_summary 재계산
        total = len(all_sentiments)
        if total:
            pos = all_sentiments.count("positive")
            neu = all_sentiments.count("neutral")
            neg = all_sentiments.count("negative")
            last_result["sentiment_summary"] = {
                "positive": round(pos/total*100),
                "neutral": round(neu/total*100),
                "negative": round(neg/total*100)
            }
        return last_result

    return {
        "sentiments": all_sentiments,
        "sentiment_summary": {"positive": 33, "neutral": 34, "negative": 33},
        "top_issues": {},
        "keywords": [],
        "churn_signals": [],
        "impact_score": 5.0
    }


def main():
    now_kst = datetime.now(KST)
    now_str = now_kst.strftime("%Y-%m-%d %H:%M")
    print(f"크롤링 시작: {now_str}")

    # 수집 범위: 전날 10:01 ~ 당일 09:59
    end_dt = now_kst  # 실행 시점까지
    start_dt = (now_kst - timedelta(days=1)).replace(hour=10, minute=1, second=0, microsecond=0)

    print(f"수집 범위: {start_dt.strftime('%Y-%m-%d %H:%M')} ~ {end_dt.strftime('%Y-%m-%d %H:%M')}")

    if not FMKOREA_COOKIE:
        print("경고: FMKOREA_COOKIE 환경변수가 없습니다.")

    session = make_session(FMKOREA_COOKIE)

    for source, config in TARGETS.items():
        base_url = config["url"]
        output = config["output"]

        print(f"\n[{source.upper()}] 크롤링 시작")

        posts = crawl_with_date_range(session, base_url, source, start_dt, end_dt, now_kst)
        print(f"[{source.upper()}] 총 수집: {len(posts)}개")

        if not posts:
            print(f"[{source.upper()}] 게시글 없음 - 빈 데이터로 저장")
            data = {
                "source": source,
                "collection_range": {
                    "start": start_dt.strftime("%Y-%m-%d %H:%M"),
                    "end": end_dt.strftime("%Y-%m-%d %H:%M")
                },
                "total_posts": 0,
                "total_views": 0,
                "avg_views_per_post": 0,
                "posts": [],
                "sentiment": {"positive": 0, "neutral": 0, "negative": 0},
                "top_issues": {},
                "keywords": [],
                "churn_signals": [],
                "esports_disengagement_signals": [],
                "fsl_mention_rate": "0%",
                "impact_score": 0,
                "viewer_engagement_estimate": "0K",
                "last_updated": now_str
            }
            with open(output, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
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
            "collection_range": {
                "start": start_dt.strftime("%Y-%m-%d %H:%M"),
                "end": end_dt.strftime("%Y-%m-%d %H:%M")
            },
            "total_posts": len(posts),
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
            "last_updated": now_str
        }

        with open(output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"{output} 저장 완료")
        time.sleep(2)

    print("\n크롤링 완료!")


if __name__ == "__main__":
    main()

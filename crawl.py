import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright
import anthropic

KST = timezone(timedelta(hours=9))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

URLS = {
    "all": "https://www.fmkorea.com/fifa_online",
    "fsl": "https://www.fmkorea.com/index.php?mid=fifa_online&category=8064047289"
}

# ── 크롤링 ──────────────────────────────────────────
def crawl(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="ko-KR",
        )
        page = context.new_page()

        # 봇 탐지 우회
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        print(f"접속 중: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Cloudflare 챌린지 대기 (최대 15초)
        for _ in range(15):
            title = page.title()
            if "보안" not in title and "Just a moment" not in title:
                break
            print("Cloudflare 챌린지 대기 중...")
            time.sleep(1)

        time.sleep(2)
        html = page.content()
        browser.close()
        return html


# ── HTML 파싱 ──────────────────────────────────────
def parse_posts(html: str) -> list:
    posts = []

    # li.li_post 패턴
    li_pattern = re.compile(
        r'<li[^>]*class="[^"]*li_post[^"]*"[^>]*>([\s\S]*?)</li>', re.IGNORECASE
    )

    for match in li_pattern.finditer(html):
        block = match.group(1)

        # 제목 + URL
        title_match = re.search(
            r'class="[^"]*title[^"]*"[^>]*>[\s\S]*?<a[^>]*href="([^"]+)"[^>]*>([^<]+)<',
            block
        )
        if not title_match:
            continue

        url = "https://www.fmkorea.com" + title_match.group(1)
        title = title_match.group(2).strip().replace(r'\s+', ' ')

        # 조회수
        view_match = re.search(r'조회[^\d]*(\d[\d,]*)', block) or \
                     re.search(r'(\d[\d,]*)\s*읽음', block)
        views = int(view_match.group(1).replace(',', '')) if view_match else 0

        # 댓글수
        comment_match = re.search(r'\[(\d+)\]', block)
        comments = int(comment_match.group(1)) if comment_match else 0

        # 날짜
        date_match = re.search(
            r'(\d{4}\.\d{2}\.\d{2}|\d{2}\.\d{2}|\d+분 전|\d+시간 전|방금)',
            block
        )
        date = date_match.group(1) if date_match else ""

        if len(title) > 2:
            posts.append({
                "title": title,
                "url": url,
                "views": views,
                "comments": comments,
                "date": date,
                "sentiment": "pending"
            })

    return posts


# ── Claude 감성분석 ─────────────────────────────────
def analyze_sentiment(posts: list, source: str) -> dict:
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
  "impact_score": 4.0,
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
    raw = re.sub(r'```json|```', '', raw).strip()

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


# ── 메인 ───────────────────────────────────────────
def main():
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    print(f"크롤링 시작: {now_kst}")

    for source, url in URLS.items():
        print(f"\n[{source.upper()}] 크롤링 중...")

        try:
            html = crawl(url)
        except Exception as e:
            print(f"크롤링 실패: {e}")
            continue

        posts = parse_posts(html)
        print(f"파싱된 게시글: {len(posts)}개")

        if not posts:
            print("게시글 없음 - 스킵")
            continue

        # 총 게시글 수 추출
        total_match = re.search(r'총\s*([\d,]+)\s*개', html) or \
                      re.search(r'전체\s*([\d,]+)', html)
        total_posts = int(total_match.group(1).replace(',', '')) if total_match else len(posts)

        total_views = sum(p["views"] for p in posts)
        avg_views = total_views // len(posts) if posts else 0

        print(f"Claude 감성분석 중...")
        analysis = analyze_sentiment(posts, source)

        # 게시글에 감성 적용
        for i, post in enumerate(posts):
            sentiments = analysis.get("sentiments", [])
            post["sentiment"] = sentiments[i] if i < len(sentiments) else "neutral"

        # 최종 데이터 구성
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

        filename = f"data_{source}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"{filename} 저장 완료")

    print("\n크롤링 완료!")


if __name__ == "__main__":
    main()

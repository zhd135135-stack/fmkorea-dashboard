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

    # 시:분 형식 (오늘 글 또는 어제 글)
    if re.match(r'^\d{1,2}:\d{2}$', date_str):
        h, m = map(int, date_str.split(":"))
        candidate = now_kst.replace(hour=h, minute=m, second=0, microsecond=0)
        # 현재 시각보다 미래면 어제 날짜로 처리
        if candidate > now_kst:
            candidate = candidate - timedelta(days=1)
        return candidate

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

[FC온라인 커뮤니티 감성 판단 기준]
- 이 커뮤니티는 20~30대 남성 게이머 중심으로 욕설·비속어가 섞인 긍정 표현이 매우 흔합니다.
- 긍정 표현 예시: "ㅅㅅ", "ㄷㄷ", "지렸다", "미쳤다", "야호", "개좋다", "갓", "레전드", "완성", "성공", "붙였다", "떴다", "득", "ㅋㅋ(성공/기쁨 맥락)", "씨발(성공 감탄)", "개쩐다"
- 부정 표현 예시: "망했다", "ㅈ됐다", "억까", "보정", "안붙는다", "실패", "ㅡㅡ", "개같다(불만)", "환불", "접겠다", "왜이래", "버그", "오류"
- 중립 표현: 선수/팀 정보 질문, 가격 질문, 스쿼드 추천 요청, 단순 정보 공유
- 욕설이 있어도 성공/기쁨 맥락이면 반드시 긍정으로 분류하세요.
- 선수 이름 + 감탄사 조합("살라 야호", "민재 ㄷㄷ")은 긍정입니다.
- "ㅋㅋ"는 맥락에 따라 긍정 또는 중립이며 부정이 아닙니다.

[Few-shot 예시 - 반드시 이 기준을 따르세요]
- "살라 야호~~" → positive (선수에 대한 감탄/기쁨)
- "와우 홀란 2골 1어시 ㄷㄷ" → positive (선수 활약 감탄)
- "진짜 존나 억울하네 시발" → negative (불만/억울함)
- "설기현 어떤가요?" → neutral (선수 정보 질문)
- "요즘 아놀드 볼란치 괜찮나요?" → neutral (선수 질문)
- "ws 만두찐빵 하한가 ㄷㄷ" → neutral (시세 정보 공유)
- "다음주 토츠 관련 시세질문입니당" → neutral (시세 질문)
- "신특크포하나에 6천조 태울만 한가요 형님들?" → neutral (구매 상담 질문)
- "이번 여름 넥슨의 개로 전직할듯" → positive (게임에 더 빠져들겠다는 자조적 긍정 표현)
- 시세 질문, 선수 질문, 추천 요청은 웬만하면 중립으로 분류하세요.

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

[FC온라인 커뮤니티 감성 판단 기준]
- 이 커뮤니티는 20~30대 남성 게이머 중심으로 욕설·비속어가 섞인 긍정 표현이 매우 흔합니다.
- 긍정 표현 예시: "ㅅㅅ", "ㄷㄷ", "지렸다", "미쳤다", "야호", "개좋다", "갓", "레전드", "완성", "성공", "붙였다", "떴다", "득", "ㅋㅋ(성공/기쁨 맥락)", "씨발(성공 감탄)", "개쩐다", "드디어", "왔다", "ㄱㄱ", "달성"
- 부정 표현 예시: "망했다", "ㅈ됐다", "억까", "보정", "안붙는다", "실패", "ㅡㅡ", "개같다(불만)", "환불", "접겠다", "왜이래", "버그", "오류", "너프", "개사기(불만)", "열받", "빡침"
- 중립 표현: 선수/팀 정보 질문, 가격 질문, 스쿼드 추천 요청, 단순 정보 공유, "~어떤가요", "~추천좀", "~얼마에요"
- 욕설이 있어도 성공/기쁨 맥락이면 반드시 긍정으로 분류하세요.
- 선수 이름 + 감탄사 조합("살라 야호", "민재 ㄷㄷ", "레전드 선방")은 긍정입니다.
- "ㅋㅋ"는 맥락에 따라 긍정 또는 중립이며 단독으로 부정이 아닙니다.
- 강화 성공("붙였다", "성공", "13카 달성")은 긍정입니다.
- 가격/시세 질문이나 정보 공유는 중립입니다.

[Few-shot 예시 - 반드시 이 기준을 따르세요]
- "살라 야호~~" → positive (선수에 대한 감탄/기쁨)
- "와우 홀란 2골 1어시 ㄷㄷ" → positive (선수 활약 감탄)
- "진짜 존나 억울하네 시발" → negative (불만/억울함)
- "설기현 어떤가요?" → neutral (선수 정보 질문)
- "요즘 아놀드 볼란치 괜찮나요?" → neutral (선수 질문)
- "ws 만두찐빵 하한가 ㄷㄷ" → neutral (시세 정보 공유)
- "다음주 토츠 관련 시세질문입니당" → neutral (시세 질문)
- "신특크포하나에 6천조 태울만 한가요 형님들?" → neutral (구매 상담 질문)
- "이번 여름 넥슨의 개로 전직할듯" → positive (게임에 더 빠져들겠다는 자조적 긍정 표현)
- "13카 달성!!!" → positive (강화 성공 기쁨)
- "강화 또 망했다" → negative (강화 실패 불만)
- "5경 팀 추천해주세요" → neutral (스쿼드 추천 요청)
- 시세 질문, 선수 질문, 추천 요청은 웬만하면 중립으로 분류하세요.

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
    end_dt = now_kst.replace(hour=9, minute=59, second=59, microsecond=0)
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

        # ── 히스토리 누적 ──────────────────────────────
        # 기존 파일에서 히스토리 읽기
        existing_history = []
        try:
            with open(output, "r", encoding="utf-8") as f:
                existing = json.load(f)
                existing_history = existing.get("history", [])
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        # ESI 계산 (T1+T2 기반)
        # T1: 리그/대회/팀/선수 직접 귀속
        T1_KW = [
            # 리그/대회
            "fsl","에프에스엘","fsl spring","fsl summer","fsl winter","fsl 팀배틀","ftb",
            "fc pro","fc pro masters","eacc","ea챔피언스컵",
            "결승전","8강","4강","그룹스테이지","녹아웃","이스포츠","e스포츠",
            # 팀명
            "t1","티원","gen city","젠시티","gct",
            "kt rolster","kt 롤스터",
            "kiwoom drx","키움 디알엑스","krx",
            "bnk fearx","bnk 피어엑스","bfx",
            "ns redforce","농심 레드포스",
            "dn soopers","디엔 수퍼스","dns",
            "dplus kia","디플러스 기아",
            # 선수 콜네임
            "byul","별빛","오펠","ofel","호석","hoseok","navy","네이비","퓨처","future","피어스","pierce",
            "wonder08","원더08","원더공팔","crong","크롱","solid","솔리드","jiffeyjay","지피제이","titan","타이탄선수","attain","아테인",
            "jm","제이엠","uta","우타","tk777","티케이","dike","디케",
            "chan","박찬화","one","이원주","savior","세비어","minion","미니언","탁","tak",
            "kaiser","카이저","noiz","노이즈","taegod","태갓","light","라이트",
            "exito","엑시토","ryuk","류크","box","박스","ppuljebi","뿔제비","aki","아키",
            "9kki","구끼","clutch","클러치","shype","샤이프","chase","체이스",
            "kwak","곽준혁","mibob","미밥","check","체크","tobio","토비오",
            # 선수 성명
            "박기홍","강준호","최호석","김유민","박지호","조성빈",
            "고원재","황세종","임태산","성지원","이준서",
            "김정민","이지환","이태경","강무진",
            "박찬화","이원주","이상민","조민혁","이강혁",
            "송현수","노영진","김태신","김선재",
            "윤형석","윤창근","강성훈","김경식","조영환",
            "김시경","박지민","김승환","권창환",
            "곽준혁","김태현","김준수",
        ]
        t1_count = sum(1 for p in posts if any(k.lower() in p["title"].lower() for k in T1_KW))
        T2_KW = ["포메이션","전술","스쿼드","팀컬러","조합","픽률","선수 추천","선수추천"]
        t2_count = sum(1 for p in posts if any(k in p["title"] for k in T2_KW))
        total = len(posts)
        pos_count = sum(1 for p in posts if p.get("sentiment") == "positive")
        pos_rate = pos_count / total * 100 if total else 0
        sent_coeff = pos_rate / 50
        raw_esi = (t1_count * 1.0 + t2_count * 0.5) / total * sent_coeff * 100 if total else 0
        esi_score = round(min(10, raw_esi), 1)

        # 오늘 날짜 히스토리 항목
        today_label = now_kst.strftime("%m/%d")
        today_entry = {
            "label": today_label,
            "date": now_kst.strftime("%Y-%m-%d"),
            "esi": esi_score,
            "posts": total,
            "esports": t1_count + t2_count,
            "positive": round(pos_rate)
        }

        # 같은 날짜 중복 방지 (오늘 항목 교체)
        existing_history = [h for h in existing_history if h.get("date") != today_entry["date"]]
        existing_history.append(today_entry)

        # 최근 90일치만 유지
        existing_history = existing_history[-90:]
        existing_history.sort(key=lambda x: x.get("date", ""))

        data["history"] = existing_history
        print(f"히스토리 누적: {len(existing_history)}일치")
        # ──────────────────────────────────────────────

        with open(output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"{output} 저장 완료")
        time.sleep(2)

    print("\n크롤링 완료!")


if __name__ == "__main__":
    main()

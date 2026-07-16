#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
에펨코리아 FC온라인 게시판 크롤러
- 브라우저 쿠키 기반 Cloudflare 우회
- FSL 탭 먼저 수집 → 전체게시판 수집 (세션 초기 상태에서 FSL 우선)
- 전날 10:01 ~ 당일 09:59 범위 데이터 수집
- Claude API로 감성분석 (50개 배치, max_tokens 4096)
- data_fsl.json / data_all.json 저장
 
[PATCH 2026-07-16] 22시 조기종료 버그 수정
- 증상: 전날 10:01~22:41 약 12시간치 게시글 누락 (최종 페이지 최고참 글이 22시대에서 끊김)
- 원인: MM.DD(날짜만) 형식 글을 그날 00:00으로 파싱 → start_dt(전날 10:01)보다 이전으로 오판
       → out_of_range_count가 5회 만에 채워져 실제로는 범위 안에 있는 글까지 못 가고 조기종료
- 수정:
  1) parse_date가 (datetime, has_time) 튜플 반환. 시각 정보가 없는 MM.DD/YYYY.MM.DD 글은
     is_in_range에서 '그 날짜 전체(00:00~23:59)'가 [start_dt, end_dt]와 겹치는지로 판정
     (시각을 모르니 날짜 단위로 보수적으로 포함 처리 — 과소수집보다 과다수집이 안전)
  2) 조기종료 카운트는 '확실히' 범위보다 오래된 글(날짜 자체가 start_dt 날짜보다 이전)만 카운트
  3) 조기종료 임계값 5 → 20으로 완화 (안전마진)
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
import requests
import urllib3
from bs4 import BeautifulSoup
import anthropic
# Windows runner 한글 인코딩 깨짐 방지
try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
KST = timezone(timedelta(hours=9))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FMKOREA_COOKIE = os.environ.get("FMKOREA_COOKIE", "")
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BASE_URL = "https://www.fmkorea.com"
# FSL 먼저, ALL 나중 (세션 초기 상태에서 FSL 차단 방지)
TARGETS = {
    "fsl": {
        "url": f"{BASE_URL}/index.php?mid=fifa_online&category=8064047289",
        "referer": f"{BASE_URL}/fifa_online",
        "output": "data_fsl.json"
    },
    "all": {
        "url": f"{BASE_URL}/fifa_online",
        "referer": f"{BASE_URL}/",
        "output": "data_all.json"
    },
}
MAX_PAGES = 150
# 조기종료 판정 임계값 (기존 5 → 20 완화)
OUT_OF_RANGE_THRESHOLD = 20
def make_session(cookie_str):
    session = requests.Session()
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    })
    if cookie_str:
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                session.cookies.set(k.strip(), v.strip(), domain=".fmkorea.com")
    return session
def parse_date(date_str, now_kst, start_dt, end_dt):
    """
    날짜 문자열을 (datetime, has_time) 튜플로 변환.
    - "HH:MM" 형식: 정확한 시:분 보유 (has_time=True)
      → 오늘 기준 파싱 후 범위 밖이면 어제 날짜로 재시도
    - "MM.DD" / "YYYY.MM.DD" 형식: 날짜만 있고 시각 정보 없음 (has_time=False)
      → 해당 날짜의 00:00으로 반환하되, 실제 범위판정은 is_in_range에서
         '그 날짜 전체'가 겹치는지로 별도 처리 (여기서 자정으로 확정짓지 않음)
    """
    date_str = date_str.strip()
    # 시:분 형식 (예: "10:01", "19:24")
    if re.match(r'^\d{1,2}:\d{2}$', date_str):
        h, m = map(int, date_str.split(":"))
        today_candidate = now_kst.replace(hour=h, minute=m, second=0, microsecond=0)
        # 크롤링 시각보다 미래이거나 동일하면 어제 글로 처리
        # (10:01에 실행 시 10:01 글은 어제 글, 09:59 글은 오늘 글)
        if today_candidate >= now_kst:
            return today_candidate - timedelta(days=1), True
        # 크롤링 시각보다 과거면 오늘 글
        return today_candidate, True
    # 월.일 형식 (예: "07.05") - 시각 정보 없음
    if re.match(r'^\d{2}\.\d{2}$', date_str):
        month, day = map(int, date_str.split("."))
        try:
            year = now_kst.year
            candidate = datetime(year, month, day, tzinfo=KST)
            # 연도 경계 보정 (예: 1월에 실행하는데 글은 "12.31" → 작년)
            if candidate.date() > now_kst.date():
                candidate = datetime(year - 1, month, day, tzinfo=KST)
            return candidate, False
        except Exception:
            return None, False
    # 연.월.일 형식 (예: "2026.07.05") - 시각 정보 없음
    if re.match(r'^\d{4}\.\d{2}\.\d{2}$', date_str):
        year, month, day = map(int, date_str.split("."))
        try:
            return datetime(year, month, day, tzinfo=KST), False
        except Exception:
            return None, False
    return None, False
def is_in_range(post_dt, has_time, start_dt, end_dt):
    """
    post_dt: parse_date가 반환한 datetime
    has_time: True면 정확한 시:분 보유 → 기존처럼 정밀 비교
              False면 날짜만 알고 시각은 모름 → 그 날짜(00:00~23:59)가
              [start_dt, end_dt] 구간과 하루 단위로라도 겹치면 in-range로 간주.
              (시각을 모르는 상태에서 자정으로 단정해 잘라내면 실제로는 범위 안에
               있는 글까지 누락되므로, 과소수집보다 과다수집 쪽으로 보수적으로 판정)
    """
    if post_dt is None:
        return False
    if has_time:
        return start_dt <= post_dt <= end_dt
    day_start = post_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = post_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day_start <= end_dt and day_end >= start_dt
def parse_posts_from_html(html, now_kst, start_dt, end_dt):
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    out_of_range_count = 0
    for tr in soup.select("table.bd_lst tbody tr"):
        if "notice" in tr.get("class", []):
            continue
        title_el = tr.select_one("td.title a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if len(title) <= 2:
            continue
        href = title_el.get("href", "")
        url = BASE_URL + href if href.startswith("/") else href
        date_el = tr.select_one("td.time")
        date_str = date_el.get_text(strip=True) if date_el else ""
        post_dt, has_time = parse_date(date_str, now_kst, start_dt, end_dt)
        in_range = is_in_range(post_dt, has_time, start_dt, end_dt)
        # 조기종료 카운트: '확실히' 범위보다 오래된 글만 카운트한다.
        # - has_time=True: 기존처럼 post_dt < start_dt면 카운트
        # - has_time=False: 그 글의 '날짜' 자체가 start_dt의 날짜보다도 이전인 경우만 카운트
        #   (start_dt와 같은 날짜인데 시각을 몰라 in-range로 판정된 경우는 카운트하지 않음
        #    → 이게 기존 버그의 핵심: 전날 날짜 글을 무조건 out-of-range로 세서 5개 만에 조기종료됐음)
        if post_dt is not None:
            if has_time:
                definitely_too_old = post_dt < start_dt
            else:
                definitely_too_old = post_dt.date() < start_dt.date()
            if definitely_too_old:
                out_of_range_count += 1
        if not in_range:
            continue
        views = 0
        for el in tr.select("td.m_no"):
            if "voted" in el.get("class", []):
                continue
            txt = el.get_text(strip=True)
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
        comments = 0
        reply_el = tr.select_one(".replyNum")
        if reply_el:
            c = re.search(r"\d+", reply_el.get_text(strip=True))
            if c:
                comments = int(c.group())
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
def crawl_with_date_range(session, base_url, referer, source, start_dt, end_dt, now_kst):
    all_posts = []
    for page in range(1, MAX_PAGES + 1):
        sep = "&" if "?" in base_url else "?"
        url = base_url if page == 1 else f"{base_url}{sep}page={page}"
        prev_url = base_url if page <= 2 else f"{base_url}{sep}page={page-1}"
        print(f"  [{source.upper()}] 페이지 {page} 크롤링: {url}")
        try:
            # 페이지마다 Referer 갱신 (자연스러운 네비게이션 위장)
            session.headers.update({"Referer": referer if page == 1 else prev_url})
            resp = session.get(url, timeout=30, verify=False)
            # 인코딩 자동 감지 (EUC-KR 대응)
            detected = resp.apparent_encoding or "utf-8"
            if detected.lower() in ("utf-8", "utf8"):
                resp.encoding = "utf-8"
            else:
                resp.encoding = detected
            print(f"  응답 코드: {resp.status_code} (인코딩: {resp.encoding})")
            if resp.status_code == 430:
                print(f"  430 Cloudflare 차단 - 쿠키 갱신 필요")
                break
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
        print(f"  수집된 게시글: {len(posts)}개 (누적: {len(all_posts)}개, 확실히 범위밖: {out_of_range_count}개)")
        if out_of_range_count >= OUT_OF_RANGE_THRESHOLD:
            print(f"  범위 이전 글 {out_of_range_count}개 감지 -> 수집 종료")
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
    for batch_start in range(0, len(posts), 50):
        batch = posts[batch_start:batch_start + 50]
        titles_text = "\n".join(f"{i+1}. {p['title']}" for i, p in enumerate(batch))
        if source == "fsl":
            prompt = f"""당신은 FC온라인 이스포츠(FSL) 커뮤니티 분석 전문가입니다.
아래 FSL/프로게이머 관련 게시글 제목들을 분석하여 JSON만 응답하세요. 다른 텍스트 없이 순수 JSON만.
[감성 판단 기준]
- 20~30대 남성 게이머 커뮤니티, 욕설 섞인 긍정 표현 매우 흔함
- 긍정: "ㅅㅅ","ㄷㄷ","지렸다","미쳤다","야호","개좋다","갓","레전드","성공","붙였다","득","씨발(감탄)","개쩐다"
- 부정: "망했다","ㅈ됐다","억까","보정","실패","ㅡㅡ","환불","접겠다","버그","오류"
- 중립: 선수/팀 정보 질문, 가격 질문, 스쿼드 추천, 단순 정보 공유
- 욕설+성공 맥락 = 긍정 / 선수이름+감탄사 = 긍정 / "ㅋㅋ" = 맥락따라 긍정or중립
게시글 제목:
{titles_text}
응답 형식 (순수 JSON만):
{{
  "sentiments": ["positive"|"neutral"|"negative", ...],
  "sentiment_summary": {{"positive": 숫자(%), "neutral": 숫자(%), "negative": 숫자(%)}},
  "top_issues": {{
    "positive": "긍정 이슈 한 줄 (20자 이내)",
    "negative": "부정 이슈 한 줄 (20자 이내)",
    "neutral": "중립 이슈 한 줄 (20자 이내)"
  }},
  "keywords": [
    {{"topic": "키워드명", "count": 추정언급수, "pct": "X.X%", "tags": "서브키워드들"}}
  ],
  "esports_disengagement_signals": ["이탈신호1", "이탈신호2"],
  "fsl_mention_rate": "X.X%",
  "impact_score": 5.0,
  "viewer_engagement_estimate": "XXK"
}}"""
        else:
            prompt = f"""당신은 FC온라인 커뮤니티 분석 전문가입니다.
아래 게시글 제목들을 분석하여 JSON만 응답하세요. 다른 텍스트 없이 순수 JSON만.
[감성 판단 기준]
- 20~30대 남성 게이머 커뮤니티, 욕설 섞인 긍정 표현 매우 흔함
- 긍정: "ㅅㅅ","ㄷㄷ","지렸다","미쳤다","야호","개좋다","갓","레전드","성공","붙였다","득","씨발(감탄)","개쩐다","드디어","달성","ㄱㄱ"
- 부정: "망했다","ㅈ됐다","억까","보정","실패","ㅡㅡ","환불","접겠다","버그","오류","너프","열받","빡침"
- 중립: 선수/팀 질문, 가격/시세 질문, 스쿼드 추천, 단순 정보
- 욕설+성공 맥락 = 긍정 / 강화성공("붙였다","13카달성") = 긍정
게시글 제목:
{titles_text}
응답 형식 (순수 JSON만):
{{
  "sentiments": ["positive"|"neutral"|"negative", ...],
  "sentiment_summary": {{"positive": 숫자(%), "neutral": 숫자(%), "negative": 숫자(%)}},
  "top_issues": {{
    "positive": "긍정 이슈 한 줄 (20자 이내)",
    "negative": "부정 이슈 한 줄 (20자 이내)",
    "neutral": "중립 이슈 한 줄 (20자 이내)"
  }},
  "keywords": [
    {{"topic": "키워드명", "count": 추정언급수, "pct": "X.X%", "tags": "서브키워드들"}}
  ],
  "churn_signals": ["이탈신호1", "이탈신호2"],
  "impact_score": 5.0
}}"""
        try:
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = message.content[0].text.strip()
            raw = re.sub(r"```json|```", "", raw).strip()
            batch_result = json.loads(raw)
            batch_sents = batch_result.get("sentiments", ["neutral"] * len(batch))
            all_sentiments.extend(batch_sents[:len(batch)])
            last_result = batch_result
            print(f"  배치 {batch_start//50+1} 분석 완료 ({len(batch)}개)")
        except Exception as e:
            print(f"  배치 {batch_start//50+1} 분석 실패: {e}")
            all_sentiments.extend(["neutral"] * len(batch))
    if last_result:
        last_result["sentiments"] = all_sentiments
        total = len(all_sentiments)
        if total:
            pos = all_sentiments.count("positive")
            neu = all_sentiments.count("neutral")
            neg = all_sentiments.count("negative")
            last_result["sentiment_summary"] = {
                "positive": round(pos / total * 100),
                "neutral": round(neu / total * 100),
                "negative": round(neg / total * 100)
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
def calc_esi(posts, pos_rate):
    """
    ESI 계산 (개선된 공식)
    - mention_rate: (T1*1.0 + T2*0.5) / total
    - sent_coeff: 긍정률 기반 0~2 보정값
    - 스케일 x2 → 0~10 범위에서 의미있는 분포
    """
    T1_KW = [
        "fsl", "에프에스엘", "fsl spring", "fsl summer", "fsl winter", "fsl 팀배틀", "ftb",
        "fc pro", "fc pro masters", "eacc", "ea챔피언스컵",
        "결승전", "8강", "4강", "그룹스테이지", "녹아웃", "이스포츠", "e스포츠",
        "프로게이머", "프로선수", "프로팀",
        "중계", "해설", "캐스터",
        "스크림", "팬미팅", "사인회", "세트스코어",
        "t1", "티원", "gen city", "젠시티", "gct",
        "kt rolster", "kt 롤스터", "kt",
        "kiwoom drx", "키움 디알엑스", "krx",
        "bnk fearx", "bnk 피어엑스", "bfx",
        "ns redforce", "농심 레드포스", "ns",
        "dn soopers", "디엔 수퍼스", "dns",
        "dplus kia", "디플러스 기아", "dk",
        "byul", "별빛", "오펠", "ofel", "호석", "hoseok", "navy", "네이비",
        "퓨처", "future", "피어스", "pierce",
        "wonder08", "원더08", "원더공팔", "crong", "크롱", "solid", "솔리드",
        "jiffeyjay", "지피제이", "titan", "타이탄선수", "attain", "아테인",
        "jm", "제이엠", "uta", "우타", "tk777", "티케이", "dike", "디케",
        "chan", "박찬화", "one", "이원주", "savior", "세비어", "minion", "미니언", "탁", "tak",
        "kaiser", "카이저", "noiz", "노이즈", "taegod", "태갓", "light", "라이트",
        "exito", "엑시토", "ryuk", "류크", "box", "박스", "ppuljebi", "뿔제비", "aki", "아키",
        "9kki", "구끼", "clutch", "클러치", "shype", "샤이프", "chase", "체이스",
        "kwak", "곽준혁", "mibob", "미밥", "check", "체크", "tobio", "토비오",
        "박기홍", "강준호", "최호석", "김유민", "박지호", "조성빈",
        "고원재", "황세종", "임태산", "성지원", "이준서",
        "김정민", "이지환", "이태경", "강무진",
        "이원주", "이상민", "조민혁", "이강혁",
        "송현수", "노영진", "김태신", "김선재",
        "윤형석", "윤창근", "강성훈", "김경식", "조영환",
        "김시경", "박지민", "김승환", "권창환",
        "김태현", "김준수",
    ]
    T2_KW = ["포메이션", "전술", "스쿼드", "팀컬러", "조합", "픽률", "선수 추천", "선수추천"]
    total = len(posts)
    if total == 0:
        return 0.0, 0, 0
    t1_count = sum(1 for p in posts if any(k.lower() in p["title"].lower() for k in T1_KW))
    t2_count = sum(1 for p in posts if any(k in p["title"] for k in T2_KW))
    esports_weighted = t1_count * 1.0 + t2_count * 0.5
    mention_rate = esports_weighted / total
    sent_coeff = (pos_rate / 100) * 2  # 0~2
    raw_esi = mention_rate * sent_coeff * 100 * 1.5
    esi_score = round(min(10.0, raw_esi), 1)
    return esi_score, t1_count, t2_count
def main():
    now_kst = datetime.now(KST)
    now_str = now_kst.strftime("%Y-%m-%d %H:%M")
    print(f"크롤링 시작: {now_str}")
    end_dt = now_kst.replace(hour=9, minute=59, second=59, microsecond=0)
    start_dt = (now_kst - timedelta(days=1)).replace(hour=10, minute=1, second=0, microsecond=0)
    print(f"수집 범위: {start_dt.strftime('%Y-%m-%d %H:%M')} ~ {end_dt.strftime('%Y-%m-%d %H:%M')}")
    if not FMKOREA_COOKIE:
        print("경고: FMKOREA_COOKIE 환경변수가 없습니다.")
    session = make_session(FMKOREA_COOKIE)
    for source, config in TARGETS.items():
        base_url = config["url"]
        referer = config["referer"]
        output = config["output"]
        print(f"\n[{source.upper()}] 크롤링 시작")
        posts = crawl_with_date_range(session, base_url, referer, source, start_dt, end_dt, now_kst)
        print(f"[{source.upper()}] 총 수집: {len(posts)}개")
        if not posts:
            print(f"[{source.upper()}] 게시글 없음 - 빈 데이터로 저장")
            # 기존 파일에서 히스토리 유지
            existing_history = []
            try:
                with open(output, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                    existing_history = existing.get("history", [])
            except (FileNotFoundError, json.JSONDecodeError):
                pass
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
                "last_updated": now_str,
                "history": existing_history
            }
            with open(output, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            if source == "fsl":
                print("  FSL 수집 후 세션 안정화 대기 (3초)...")
                time.sleep(3)
            continue
        total_views = sum(p["views"] for p in posts)
        avg_views = total_views // len(posts) if posts else 0
        print(f"Claude 감성분석 중...")
        analysis = analyze_sentiment(posts, source)
        sentiments = analysis.get("sentiments", [])
        for i, post in enumerate(posts):
            post["sentiment"] = sentiments[i] if i < len(sentiments) else "neutral"
        # ESI 계산 (개선된 공식)
        total = len(posts)
        pos_count = sum(1 for p in posts if p.get("sentiment") == "positive")
        pos_rate = pos_count / total * 100 if total else 0
        esi_score, t1_count, t2_count = calc_esi(posts, pos_rate)
        print(f"ESI: {esi_score} (T1={t1_count}, T2={t2_count}, pos={round(pos_rate)}%)")
        data = {
            "source": source,
            "collection_range": {
                "start": start_dt.strftime("%Y-%m-%d %H:%M"),
                "end": end_dt.strftime("%Y-%m-%d %H:%M")
            },
            "total_posts": total,
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
        # 히스토리 누적
        existing_history = []
        try:
            with open(output, "r", encoding="utf-8") as f:
                existing = json.load(f)
                existing_history = existing.get("history", [])
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        today_label = now_kst.strftime("%m/%d")
        today_entry = {
            "label": today_label,
            "date": now_kst.strftime("%Y-%m-%d"),
            "esi": esi_score,
            "posts": total,
            "esports": t1_count + t2_count,
            "positive": round(pos_rate)
        }
        existing_history = [h for h in existing_history if h.get("date") != today_entry["date"]]
        existing_history.append(today_entry)
        existing_history = existing_history[-90:]
        existing_history.sort(key=lambda x: x.get("date", ""))
        data["history"] = existing_history
        print(f"히스토리 누적: {len(existing_history)}일치")
        with open(output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"{output} 저장 완료")
        # FSL 완료 후 대기 (all 요청 전 세션 안정화)
        if source == "fsl":
            print("  FSL 완료 - 세션 안정화 대기 (3초)...")
            time.sleep(3)
        else:
            time.sleep(2)
    print("\n크롤링 완료!")
if __name__ == "__main__":
    main()

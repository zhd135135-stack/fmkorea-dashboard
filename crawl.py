#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
에펨코리아 FC온라인 게시판 크롤러 (v2)
- 브라우저 쿠키 기반 Cloudflare/Akamai 우회
- Cookie 헤더 직접 주입 (도메인 파싱 누락 방지)
- 쿠키 유효성 사전 검증 + 봇차단/만료 감지
- 요청 실패 시 자동 재시도(backoff)
- 전체게시판 + FSL/프로게이머 탭 수집
- 전날 10:01 ~ 당일 09:59 범위 데이터 수집
- Claude Haiku API로 감성분석 (100개 배치)
- data_all.json / data_fsl.json 저장 (90일 ESI 히스토리 누적)
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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Windows self-hosted 러너(cp949 콘솔)에서 한글/기호 출력 시
# UnicodeEncodeError가 나는 것을 방지 — stdout/stderr를 UTF-8로 강제.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ──────────────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FMKOREA_COOKIE = os.environ.get("FMKOREA_COOKIE", "")

# 쿠키를 딴 브라우저와 UA를 맞추는 게 봇차단 회피에 유리.
# (DevTools 149 = Chrome 최신 계열이므로 131로 설정)
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

TARGETS = {
    "all": {
        "url": "https://www.fmkorea.com/index.php?mid=fifa_online",
        "output": "data_all.json",
    },
    "fsl": {
        "url": "https://www.fmkorea.com/index.php?mid=fifa_online&category=8064047289",
        "output": "data_fsl.json",
    },
}

MAX_PAGES = 150          # 최대 150페이지 = 최대 3,000개
REQUEST_TIMEOUT = 30
MAX_RETRY = 3            # 요청 실패 시 재시도 횟수
RETRY_BACKOFF = 3        # 재시도 간 대기(초) — 시도마다 곱연산
PAGE_DELAY = 1.2         # 페이지 간 딜레이(초)


# ──────────────────────────────────────────────────────────
# 세션 / 요청
# ──────────────────────────────────────────────────────────
def make_session(cookie_str):
    """
    쿠키는 session.cookies.set(domain=...) 대신 Cookie 헤더로 통째로 주입.
    fmkorea/google/akamai 등 도메인이 섞인 브라우저 쿠키를 그대로 넘겨야
    PHPSESSID, __FSK, ak_bmsc 같은 인증/봇방어 토큰이 누락 없이 전달됨.
    """
    session = requests.Session()
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.fmkorea.com/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str.strip()
    session.headers.update(headers)
    return session


def is_blocked(html):
    """봇 차단 / 챌린지 / 쿠키 만료 감지"""
    if not html:
        return True
    markers = [
        "에펨코리아 보안 시스템",
        "cf-turnstile",
        "Just a moment",
        "Checking your browser",
        "Access Denied",
        "잠시 후 다시",
        "비정상적인 접근",
    ]
    return any(m in html for m in markers)


def fetch(session, url):
    """재시도 포함 GET. 성공 시 html, 실패 시 None 반환."""
    for attempt in range(1, MAX_RETRY + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT, verify=False)
            if resp.status_code == 200:
                return resp.text
            print(f"    응답 {resp.status_code} (시도 {attempt}/{MAX_RETRY})")
            # 403/429 = 차단/속도제한 → 백오프 후 재시도
            if resp.status_code in (403, 429, 503):
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            return None
        except Exception as e:
            print(f"    요청 예외: {e} (시도 {attempt}/{MAX_RETRY})")
            time.sleep(RETRY_BACKOFF * attempt)
    return None


def validate_cookie(session):
    """
    크롤링 시작 전 쿠키 유효성 확인.
    첫 페이지를 받아 차단 여부 / 정상 목록 노출 여부 체크.
    """
    print("쿠키 유효성 검증 중...")
    html = fetch(session, TARGETS["all"]["url"])
    if html is None:
        print("  [FAIL] 첫 페이지 응답 실패")
        return False
    if is_blocked(html):
        print("  [FAIL] 봇 차단/챌린지 감지 → 쿠키 갱신 필요")
        return False
    # 게시판 목록 마커가 보이는지 확인 (tbody 비의존)
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select("table.bd_lst tr"):
        print("  [FAIL] 게시글 목록 셀렉터 매칭 실패 (구조 변경 또는 차단 의심)")
        return False
    print("  [OK] 쿠키 정상")
    return True


# ──────────────────────────────────────────────────────────
# 파싱
# ──────────────────────────────────────────────────────────
def parse_date(date_str, now_kst):
    """
    "10:59"      → 오늘(미래면 어제) 시:분
    "06.09"      → 올해 월.일
    "2025.06.09" → 연.월.일
    """
    date_str = date_str.strip()

    if re.match(r"^\d{1,2}:\d{2}$", date_str):
        h, m = map(int, date_str.split(":"))
        candidate = now_kst.replace(hour=h, minute=m, second=0, microsecond=0)
        # 게시판 표기 시각과 크롤 시각의 미세한 오차(분 단위)로 글이 살짝 '미래'로
        # 보일 수 있음. 10분까지는 오늘로 인정하고, 그 이상 미래면 어제 글로 처리.
        if candidate > now_kst + timedelta(minutes=10):
            candidate -= timedelta(days=1)
        return candidate

    if re.match(r"^\d{1,2}\.\d{1,2}$", date_str):
        month, day = map(int, date_str.split("."))
        try:
            return datetime(now_kst.year, month, day, tzinfo=KST)
        except ValueError:
            return None

    if re.match(r"^\d{4}\.\d{1,2}\.\d{1,2}$", date_str):
        year, month, day = map(int, date_str.split("."))
        try:
            return datetime(year, month, day, tzinfo=KST)
        except ValueError:
            return None

    return None


def is_in_range(post_dt, start_dt, end_dt):
    if post_dt is None:
        return False
    return start_dt <= post_dt <= end_dt


def parse_posts_from_html(html, now_kst, start_dt, end_dt):
    """HTML에서 게시글 파싱 + 날짜 범위 필터링"""
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    out_of_range_count = 0

    # tbody에 의존하지 않음 — fmkorea의 일부 페이지(특히 메인 진입 /fifa_online)는
    # 광고·위젯이 섞이며 html.parser가 tbody 경계를 깨뜨려서 'tbody tr'이 0개가 됨.
    # 'table.bd_lst tr'로 모든 행을 잡고, 제목 링크가 글 패턴(/숫자)인지로 필터링.
    for tr in soup.select("table.bd_lst tr"):
        cls = tr.get("class", [])
        # 공지 / 공지더보기 / 헤더(th 포함) 행 제외
        if "notice" in cls or "show_folded_notice" in cls:
            continue
        if tr.find("th"):
            continue

        # 제목 링크: td.title 안에서 href가 '/숫자' 형태인 a (글 본문 링크)
        title_el = None
        for a in tr.select("td.title a[href]"):
            href = a.get("href", "")
            if re.match(r"^/\d+$", href):  # 글 링크는 /10022784598 처럼 /숫자
                title_el = a
                break
        if not title_el:
            continue

        title = title_el.get_text(strip=True)
        if len(title) <= 2:
            continue

        href = title_el.get("href", "")
        url = "https://www.fmkorea.com" + href if href.startswith("/") else href

        date_el = tr.select_one("td.time")
        date_str = date_el.get_text(strip=True) if date_el else ""
        post_dt = parse_date(date_str, now_kst)

        if post_dt and post_dt < start_dt:
            out_of_range_count += 1

        if not is_in_range(post_dt, start_dt, end_dt):
            continue

        # 조회수
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

        # 댓글수
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
            "sentiment": "pending",
        })

    return posts, out_of_range_count


def crawl_with_date_range(session, base_url, source, start_dt, end_dt, now_kst):
    """날짜 범위 기반 페이지 순회 크롤링"""
    all_posts = []
    empty_streak = 0  # 연속 빈 페이지 카운트 (차단/끝 감지)

    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = base_url
        else:
            sep = "&" if "?" in base_url else "?"
            url = f"{base_url}{sep}page={page}"

        print(f"  [{source.upper()}] p{page}: {url}")

        html = fetch(session, url)
        if html is None:
            print("  응답 실패 → 수집 종료")
            break

        if is_blocked(html):
            print("  봇 차단/챌린지 감지 → 쿠키 갱신 필요. 수집 종료")
            break

        # ── 진단(1페이지 한정): 받은 HTML의 실제 구조를 로그로 남김 ──
        if page == 1:
            from bs4 import BeautifulSoup as _BS
            _soup = _BS(html, "html.parser")
            _tbl = _soup.select("table.bd_lst")
            _rows = _soup.select("table.bd_lst tr")
            _titles = _soup.select("table.bd_lst td.title a")
            print(f"  [진단] HTML 길이={len(html)} / "
                  f"table.bd_lst={len(_tbl)}개 / tr={len(_rows)}개 / td.title a={len(_titles)}개")
            # 받은 HTML을 파일로 저장 (커밋되어 직접 열어볼 수 있게)
            try:
                with open(f"debug_{source}.html", "w", encoding="utf-8") as _f:
                    _f.write(html)
                print(f"  [진단] debug_{source}.html 저장")
            except Exception as _e:
                print(f"  [진단] 저장 실패: {_e}")
        # ───────────────────────────────────────────────────────

        posts, oor = parse_posts_from_html(html, now_kst, start_dt, end_dt)
        all_posts.extend(posts)
        print(f"  수집 {len(posts)}개 (누적 {len(all_posts)})")

        # 파싱 결과가 0이고 범위밖도 0이면 구조 이상/차단 의심
        if len(posts) == 0 and oor == 0:
            empty_streak += 1
            if empty_streak >= 2:
                print("  연속 빈 페이지 → 수집 종료")
                break
        else:
            empty_streak = 0

        # 범위 이전 글이 충분히 나오면 더 볼 필요 없음
        if oor >= 5:
            print(f"  범위 이전 글 {oor}개 → 수집 종료")
            break

        time.sleep(PAGE_DELAY)

    return all_posts


# ──────────────────────────────────────────────────────────
# 감성분석 (Claude Haiku)
# ──────────────────────────────────────────────────────────
SENTIMENT_GUIDE = """[FC온라인 커뮤니티 감성 판단 기준]
- 20~30대 남성 게이머 중심. 욕설·비속어가 섞인 긍정 표현이 매우 흔합니다.
- 긍정 예시: "ㅅㅅ","ㄷㄷ","지렸다","미쳤다","야호","개좋다","갓","레전드","완성","성공","붙였다","떴다","득","ㅋㅋ(성공/기쁨)","씨발(성공 감탄)","개쩐다","드디어","왔다","ㄱㄱ","달성"
- 부정 예시: "망했다","ㅈ됐다","억까","보정","안붙는다","실패","ㅡㅡ","개같다(불만)","환불","접겠다","왜이래","버그","오류","너프","개사기(불만)","열받","빡침"
- 중립 예시: 선수/팀 정보 질문, 가격·시세 질문, 스쿼드 추천 요청, 단순 정보 공유, "~어떤가요","~추천좀","~얼마에요"
- 욕설이 있어도 성공/기쁨 맥락이면 반드시 긍정.
- 선수 이름 + 감탄사("살라 야호","민재 ㄷㄷ")는 긍정.
- "ㅋㅋ" 단독은 부정 아님(긍정/중립).
- 강화 성공("붙였다","13카 달성")은 긍정.
- 가격/시세/추천 질문은 웬만하면 중립.

[Few-shot]
- "살라 야호~~" → positive
- "와우 홀란 2골 1어시 ㄷㄷ" → positive
- "진짜 존나 억울하네 시발" → negative
- "설기현 어떤가요?" → neutral
- "ws 만두찐빵 하한가 ㄷㄷ" → neutral
- "신특크포하나에 6천조 태울만 한가요 형님들?" → neutral
- "이번 여름 넥슨의 개로 전직할듯" → positive
- "13카 달성!!!" → positive
- "강화 또 망했다" → negative
- "5경 팀 추천해주세요" → neutral
"""


def build_prompt(titles_text, source):
    base = (
        "당신은 FC온라인 이스포츠(FSL) 커뮤니티 분석 전문가입니다.\n"
        if source == "fsl"
        else "당신은 FC온라인 커뮤니티 분석 전문가입니다.\n"
    )
    base += "아래 게시글 제목들을 분석하여 JSON만 응답하세요. 다른 텍스트 없이 순수 JSON만.\n\n"
    base += SENTIMENT_GUIDE
    base += f"\n게시글 제목:\n{titles_text}\n\n"

    if source == "fsl":
        schema = """응답 형식:
{
  "sentiments": ["positive"|"neutral"|"negative", ...],
  "sentiment_summary": { "positive": 숫자(%), "neutral": 숫자(%), "negative": 숫자(%) },
  "top_issues": {
    "positive": "긍정 이슈 핵심 한 줄 (20자 이내)",
    "negative": "부정 이슈 핵심 한 줄 (20자 이내)",
    "neutral": "중립 이슈 핵심 한 줄 (20자 이내)"
  },
  "keywords": [
    { "topic": "키워드명", "count": 추정언급수, "pct": "X.X%", "tags": "관련 서브키워드들" }
  ],
  "esports_disengagement_signals": ["이스포츠 이탈/무관심 신호1", "신호2"],
  "fsl_mention_rate": "X.X%",
  "impact_score": 5.0,
  "viewer_engagement_estimate": "XXK"
}"""
    else:
        schema = """응답 형식:
{
  "sentiments": ["positive"|"neutral"|"negative", ...],
  "sentiment_summary": { "positive": 숫자(%), "neutral": 숫자(%), "negative": 숫자(%) },
  "top_issues": {
    "positive": "긍정 이슈 핵심 한 줄 (20자 이내)",
    "negative": "부정 이슈 핵심 한 줄 (20자 이내)",
    "neutral": "중립 이슈 핵심 한 줄 (20자 이내)"
  },
  "keywords": [
    { "topic": "키워드명", "count": 추정언급수, "pct": "X.X%", "tags": "관련 서브키워드들" }
  ],
  "churn_signals": ["이탈/불만 신호 문장1", "문장2"],
  "impact_score": 5.0
}"""

    return base + schema


def analyze_sentiment(posts, source):
    if not posts or not ANTHROPIC_API_KEY:
        return {
            "sentiments": ["neutral"] * len(posts),
            "sentiment_summary": {"positive": 33, "neutral": 34, "negative": 33},
            "top_issues": {"positive": "분석 불가", "negative": "분석 불가", "neutral": "분석 불가"},
            "keywords": [],
            "churn_signals": [],
            "esports_disengagement_signals": [],
            "impact_score": 5.0,
            "fsl_mention_rate": "0%",
            "viewer_engagement_estimate": "0K",
        }

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    all_sentiments = []
    last_result = None

    for batch_start in range(0, len(posts), 100):
        batch = posts[batch_start:batch_start + 100]
        titles_text = "\n".join(f"{i+1}. {p['title']}" for i, p in enumerate(batch))
        prompt = build_prompt(titles_text, source)

        try:
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            raw = re.sub(r"```json|```", "", raw).strip()
            batch_result = json.loads(raw)
            batch_sents = batch_result.get("sentiments", ["neutral"] * len(batch))
            all_sentiments.extend(batch_sents[:len(batch)])
            # 부족분 패딩
            if len(batch_sents) < len(batch):
                all_sentiments.extend(["neutral"] * (len(batch) - len(batch_sents)))
            last_result = batch_result
            print(f"  배치 {batch_start//100+1} 분석 완료 ({len(batch)}개)")
        except Exception as e:
            print(f"  배치 {batch_start//100+1} 분석 실패: {e}")
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
                "negative": round(neg / total * 100),
            }
        return last_result

    return {
        "sentiments": all_sentiments,
        "sentiment_summary": {"positive": 33, "neutral": 34, "negative": 33},
        "top_issues": {},
        "keywords": [],
        "churn_signals": [],
        "esports_disengagement_signals": [],
        "impact_score": 5.0,
    }


# ──────────────────────────────────────────────────────────
# ESI 키워드
# ──────────────────────────────────────────────────────────
T1_KW = [
    # 리그/대회
    "fsl", "에프에스엘", "fsl spring", "fsl summer", "fsl winter", "fsl 팀배틀", "ftb",
    "fc pro", "fc pro masters", "eacc", "ea챔피언스컵",
    "결승전", "8강", "4강", "그룹스테이지", "녹아웃", "이스포츠", "e스포츠",
    # 팀명
    "t1", "티원", "gen city", "젠시티", "gct",
    "kt rolster", "kt 롤스터",
    "kiwoom drx", "키움 디알엑스", "krx",
    "bnk fearx", "bnk 피어엑스", "bfx",
    "ns redforce", "농심 레드포스",
    "dn soopers", "디엔 수퍼스", "dns",
    "dplus kia", "디플러스 기아",
    # 선수 콜네임
    "byul", "별빛", "오펠", "ofel", "호석", "hoseok", "navy", "네이비", "퓨처", "future", "피어스", "pierce",
    "wonder08", "원더08", "원더공팔", "crong", "크롱", "solid", "솔리드", "jiffeyjay", "지피제이", "titan", "타이탄선수", "attain", "아테인",
    "jm", "제이엠", "uta", "우타", "tk777", "티케이", "dike", "디케",
    "chan", "박찬화", "one", "이원주", "savior", "세비어", "minion", "미니언", "탁", "tak",
    "kaiser", "카이저", "noiz", "노이즈", "taegod", "태갓", "light", "라이트",
    "exito", "엑시토", "ryuk", "류크", "box", "박스", "ppuljebi", "뿔제비", "aki", "아키",
    "9kki", "구끼", "clutch", "클러치", "shype", "샤이프", "chase", "체이스",
    "kwak", "곽준혁", "mibob", "미밥", "check", "체크", "tobio", "토비오",
    # 선수 성명
    "박기홍", "강준호", "최호석", "김유민", "박지호", "조성빈",
    "고원재", "황세종", "임태산", "성지원", "이준서",
    "김정민", "이지환", "이태경", "강무진",
    "이상민", "조민혁", "이강혁",
    "송현수", "노영진", "김태신", "김선재",
    "윤형석", "윤창근", "강성훈", "김경식", "조영환",
    "김시경", "박지민", "김승환", "권창환",
    "김태현", "김준수",
]
T2_KW = ["포메이션", "전술", "스쿼드", "팀컬러", "조합", "픽률", "선수 추천", "선수추천"]


def compute_esi(posts):
    total = len(posts)
    if not total:
        return 0.0, 0, 0
    t1 = sum(1 for p in posts if any(k.lower() in p["title"].lower() for k in T1_KW))
    t2 = sum(1 for p in posts if any(k in p["title"] for k in T2_KW))
    pos = sum(1 for p in posts if p.get("sentiment") == "positive")
    pos_rate = pos / total * 100
    sent_coeff = pos_rate / 50
    raw = (t1 * 1.0 + t2 * 0.5) / total * sent_coeff * 100
    return round(min(10, raw), 1), t1, t2


# ──────────────────────────────────────────────────────────
# 저장
# ──────────────────────────────────────────────────────────
def empty_payload(source, start_dt, end_dt, now_str):
    return {
        "source": source,
        "collection_range": {
            "start": start_dt.strftime("%Y-%m-%d %H:%M"),
            "end": end_dt.strftime("%Y-%m-%d %H:%M"),
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
        "history": load_history(source),
        "last_updated": now_str,
    }


def load_history(output_or_source):
    """기존 파일에서 history 배열만 읽어옴"""
    output = output_or_source
    if not output.endswith(".json"):
        output = TARGETS.get(output_or_source, {}).get("output", "")
    try:
        with open(output, "r", encoding="utf-8") as f:
            return json.load(f).get("history", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def update_history(existing, now_kst, esi_score, total, t1, t2, pos_rate):
    today_entry = {
        "label": now_kst.strftime("%m/%d"),
        "date": now_kst.strftime("%Y-%m-%d"),
        "esi": esi_score,
        "posts": total,
        "esports": t1 + t2,
        "positive": round(pos_rate),
    }
    existing = [h for h in existing if h.get("date") != today_entry["date"]]
    existing.append(today_entry)
    existing = existing[-90:]
    existing.sort(key=lambda x: x.get("date", ""))
    return existing


# ──────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────
def main():
    now_kst = datetime.now(KST)
    now_str = now_kst.strftime("%Y-%m-%d %H:%M")
    print(f"크롤링 시작: {now_str}")

    # 수집 범위: 실행 시점 기준 최근 24시간.
    # 아침 10시 자동 실행이든, 낮 수동 실행이든 항상 직전 24시간 글을 수집.
    # end_dt에 10분 여유 — 게시판 표기 시각이 크롤 시각보다 몇 분 앞설 수 있어
    # 직전 글이 누락되는 것을 방지.
    end_dt = now_kst + timedelta(minutes=10)
    start_dt = now_kst - timedelta(hours=24)
    print(f"수집 범위: {start_dt:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M}")

    if not FMKOREA_COOKIE:
        print("경고: FMKOREA_COOKIE 환경변수가 없습니다. 차단될 가능성이 높습니다.")
    if not ANTHROPIC_API_KEY:
        print("경고: ANTHROPIC_API_KEY 환경변수가 없습니다. 감성분석은 중립 처리됩니다.")

    session = make_session(FMKOREA_COOKIE)

    # 쿠키 사전 검증 — 실패 시 즉시 중단(exit 1)하여 워크플로우가 빨간불로 표시되게 함
    if FMKOREA_COOKIE and not validate_cookie(session):
        print("\n쿠키가 만료/차단 상태입니다. 브라우저에서 쿠키를 다시 따서 "
              "FMKOREA_COOKIE Secret을 갱신하세요.")
        sys.exit(1)

    for source, config in TARGETS.items():
        base_url = config["url"]
        output = config["output"]
        print(f"\n[{source.upper()}] 크롤링 시작")

        posts = crawl_with_date_range(session, base_url, source, start_dt, end_dt, now_kst)
        print(f"[{source.upper()}] 총 수집: {len(posts)}개")

        if not posts:
            print(f"[{source.upper()}] 게시글 없음 - 빈 데이터로 저장")
            with open(output, "w", encoding="utf-8") as f:
                json.dump(empty_payload(source, start_dt, end_dt, now_str),
                          f, ensure_ascii=False, indent=2)
            continue

        total_views = sum(p["views"] for p in posts)
        avg_views = total_views // len(posts)

        print("Claude 감성분석 중...")
        analysis = analyze_sentiment(posts, source)
        sentiments = analysis.get("sentiments", [])
        for i, post in enumerate(posts):
            post["sentiment"] = sentiments[i] if i < len(sentiments) else "neutral"

        esi_score, t1, t2 = compute_esi(posts)
        pos_rate = sum(1 for p in posts if p.get("sentiment") == "positive") / len(posts) * 100
        history = update_history(load_history(output), now_kst, esi_score,
                                 len(posts), t1, t2, pos_rate)
        print(f"히스토리 누적: {len(history)}일치 (오늘 ESI {esi_score})")

        data = {
            "source": source,
            "collection_range": {
                "start": start_dt.strftime("%Y-%m-%d %H:%M"),
                "end": end_dt.strftime("%Y-%m-%d %H:%M"),
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
            "history": history,
            "last_updated": now_str,
        }

        with open(output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"{output} 저장 완료")
        time.sleep(2)

    print("\n크롤링 완료!")


if __name__ == "__main__":
    main()

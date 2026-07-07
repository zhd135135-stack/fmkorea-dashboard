#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESI 스케일 재계산 스크립트
- 크롤링 없이 기존 data_all.json / data_fsl.json에서 ESI만 재산정
- 스케일 2.0 → 1.5로 변경
"""

import json
import os

ESI_SCALE = 1.5  # 변경할 스케일

T1_KW = [
    "fsl", "에프에스엘", "fsl spring", "fsl summer", "fsl winter", "fsl 팀배틀", "ftb",
    "fc pro", "fc pro masters", "eacc", "ea챔피언스컵",
    "결승전", "8강", "4강", "그룹스테이지", "녹아웃", "이스포츠", "e스포츠",
    "t1", "티원", "gen city", "젠시티", "gct",
    "kt rolster", "kt 롤스터",
    "kiwoom drx", "키움 디알엑스", "krx",
    "bnk fearx", "bnk 피어엑스", "bfx",
    "ns redforce", "농심 레드포스",
    "dn soopers", "디엔 수퍼스", "dns",
    "dplus kia", "디플러스 기아",
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


def calc_esi(posts, pos_rate, scale=ESI_SCALE):
    total = len(posts)
    if total == 0:
        return 0.0, 0, 0
    t1_count = sum(1 for p in posts if any(k.lower() in p["title"].lower() for k in T1_KW))
    t2_count = sum(1 for p in posts if any(k in p["title"] for k in T2_KW))
    esports_weighted = t1_count * 1.0 + t2_count * 0.5
    mention_rate = esports_weighted / total
    sent_coeff = (pos_rate / 100) * 2
    raw_esi = mention_rate * sent_coeff * 100 * scale
    return round(min(10.0, raw_esi), 1), t1_count, t2_count


def recalc_file(filepath):
    if not os.path.exists(filepath):
        print(f"  파일 없음: {filepath}")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    posts = data.get("posts", [])
    if not posts:
        print(f"  {filepath}: 게시글 없음, 스킵")
        return

    sentiment = data.get("sentiment", {})
    pos_rate = sentiment.get("positive", 0)

    old_esi = data.get("history", [{}])[-1].get("esi", "없음") if data.get("history") else "없음"
    new_esi, t1, t2 = calc_esi(posts, pos_rate)

    print(f"  {filepath}: ESI {old_esi} → {new_esi} (T1={t1}, T2={t2}, pos={pos_rate}%)")

    # history의 오늘 항목 ESI 업데이트
    history = data.get("history", [])
    if history:
        history[-1]["esi"] = new_esi
        history[-1]["esports"] = t1 + t2
        data["history"] = history

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  저장 완료")


if __name__ == "__main__":
    print(f"ESI 재계산 시작 (스케일 x{ESI_SCALE})")
    recalc_file("data_all.json")
    recalc_file("data_fsl.json")
    print("완료!")

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
        "Referer": "https://www.fmkorea.com/

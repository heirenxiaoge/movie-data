#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集/归一化公共库：苹果 CMS 接口、标题归一化、分类映射、SQL 转义。

只做「资源站 -> 内存结构」，不碰豆瓣。豆瓣元数据由 App 上报（表 douban_cache）或
douban_seed.json 打底，见 match.py。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}

# 苹果 CMS 的 pagesize 基本被忽略（固定 20 条/页），必须靠 pagecount 翻页
PAGE_SLEEP = 0.4
SITE_SLEEP = 1.2
TIMEOUT = 25


def log(msg: str) -> None:
    print(msg, flush=True)


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# 标题归一化（与 App 端 normTitleKey 保持一致：去掉空白/标点/括号）
# --------------------------------------------------------------------------
_PUNCT = re.compile(r"[\s《》〈〉·・:：;；,，.。!！?？'\"“”‘’()（）\[\]【】{}<>《》\-_/\\|~—]+")
_SUFFIX = re.compile(
    r"(国语|粤语|日语|韩语|英语|法语|德语|印地语|泰语|中字|中文字幕|双语|"
    r"高清|超清|蓝光|4k|1080p|720p|hdr|完整版|未删减版|加长版|导演剪辑版|"
    r"tc|ts|hd|bd|web|remux|抢先版|枪版|原声|国语版|粤语版)$"
)


def norm_title(value: str) -> str:
    s = _PUNCT.sub("", (value or "").lower())
    # 反复削掉尾部语言/画质标记（「某某 国语」和「某某」要能对上）
    for _ in range(3):
        new = _SUFFIX.sub("", s)
        if new == s:
            break
        s = new
    return s


# --------------------------------------------------------------------------
# 分类映射：CMS 的 vod_class 五花八门，压成豆瓣那套顶层分类
# --------------------------------------------------------------------------
_KIND_RULES = [
    ("综艺", ("综艺", "真人秀", "脱口秀", "晚会", "选秀", "访谈")),
    ("纪录片", ("纪录", "记录")),
    ("动漫", ("动漫", "动画", "番剧", "国漫", "日漫", "漫画", "漫剧")),
    ("体育", ("体育", "足球", "篮球", "赛事", "NBA", "世界杯")),
    ("电影", ("电影", "影片", "剧场版", "电影片")),
    ("剧集", ("剧", "连续剧", "电视剧", "美剧", "韩剧", "日剧", "港剧", "台剧", "短剧")),
]


def map_kind(vod_class: str, remarks: str = "", ep_count: int = 0) -> str:
    text = f"{vod_class or ''} {remarks or ''}".lower()
    for kind, keys in _KIND_RULES:
        for k in keys:
            if k.lower() in text:
                return kind
    # 分类是空的（约四成条目如此）时按集数兜底：多集=剧集，单集=电影
    if ep_count > 1:
        return "剧集"
    if ep_count == 1:
        return "电影"
    return "其他"


# 地区映射：CMS 常用「大陆/内地/香港/台湾/美国/韩国/日本/泰国/印度/欧洲」
_AREA_RULES = [
    ("中国大陆", ("大陆", "内地", "中国", "国产")),
    ("中国香港", ("香港", "港剧", "港台")),
    ("中国台湾", ("台湾", "台剧")),
    ("韩国", ("韩国", "韩剧", "韩影")),
    ("日本", ("日本", "日剧", "日漫", "日影")),
    ("美国", ("美国", "美剧", "美影")),
    ("英国", ("英国", "英剧")),
    ("泰国", ("泰国", "泰剧")),
    ("印度", ("印度", "印剧")),
    ("法国", ("法国",)),
    ("其他", ()),
]


def map_area(vod_area: str) -> str:
    text = (vod_area or "").strip()
    if not text:
        return ""
    for area, keys in _AREA_RULES:
        for k in keys:
            if k and k in text:
                return area
    return text[:12]


# --------------------------------------------------------------------------
# 播放地址解析：`线路1$$$线路2`，线路内 `集名$url#集名$url`
# --------------------------------------------------------------------------
def parse_play_url(raw: str):
    """返回 [[{n,name,url}, ...], ...]，只保留 http(s) 的 m3u8/mp4。"""
    lines = []
    if not raw:
        return lines
    for block in str(raw).split("$$$"):
        eps = []
        for chunk in block.split("#"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "$" in chunk:
                name, url = chunk.split("$", 1)
            else:
                name, url = "", chunk
            name, url = name.strip(), url.strip()
            low = url.lower()
            if not low.startswith(("http://", "https://")):
                continue
            if ".m3u8" not in low and ".mp4" not in low:
                continue
            eps.append({"n": len(eps) + 1, "name": name or f"第{len(eps) + 1}集", "url": url})
        if eps:
            lines.append(eps)
    return lines


def parse_time(value: str):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
    return None


def day_of(value: str) -> str:
    """vod_time -> YYYY-MM-DD；解析失败用今天。"""
    dt = parse_time(value)
    return dt.strftime("%Y-%m-%d") if dt else today()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def get_json(api: str, params: dict, timeout: int = TIMEOUT):
    r = requests.get(api, params=params, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    # 部分站返回 text/html 但内容是 json
    return r.json()


def host_ok(url: str) -> bool:
    try:
        return bool(urlparse(url).netloc)
    except Exception:
        return False


# --------------------------------------------------------------------------
# SQL 字面量
# --------------------------------------------------------------------------
def sql_str(value) -> str:
    if value is None:
        return "NULL"
    s = str(value).replace("\\", "\\\\").replace("'", "''")
    s = s.replace("\r", " ").replace("\n", " ").replace("\x00", "")
    return "'" + s + "'"


def sql_num(value) -> str:
    if value is None or value == "":
        return "NULL"
    try:
        return str(float(value))
    except (TypeError, ValueError):
        return "NULL"


def sql_int(value) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "0"


def dump_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取苹果 CMS 资源站 -> 生成 latest.json

设计要点（与方案评审一致）：
1. 只用资源站字段，不访问豆瓣；
2. 苹果 CMS 的 pagesize 普遍被忽略（固定 20 条），必须靠 pagecount 翻页；
3. 同一部片子跨站合并成「多线路」lines，而不是只留一条；
4. 抽检只标记 checked=ok/fail/unknown，不丢弃条目；
5. h 参数实测有效，但仍在本地按 vod_time 二次过滤，防止某些站忽略 h。
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(ROOT, "sources.json")
OUT_FILE = os.path.join(ROOT, "latest.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}

MAX_PAGES = 20
SLEEP_BETWEEN_SITES = 1.5
CHECK_WORKERS = 4
CHECK_TIMEOUT = 5
CHECK_SAMPLE_RATIO = 0.3
CHECK_SAMPLE_MAX = int(os.environ.get("CHECK_SAMPLE_MAX") or "100")
SCHEMA = 1


def log(msg: str) -> None:
    print(msg, flush=True)


def load_sources():
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [s for s in data if s.get("enabled")]


def parse_time(value: str):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def parse_play_url(raw: str):
    """`线路1$$$线路2`，线路内 `集名$url#集名$url`。只保留 m3u8。"""
    lines = []
    if not raw:
        return lines
    for block in raw.split("$$$"):
        episodes = []
        for chunk in block.split("#"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "$" in chunk:
                name, url = chunk.split("$", 1)
            else:
                name, url = "", chunk
            name, url = name.strip(), url.strip()
            if not url.lower().startswith(("http://", "https://")):
                continue
            if ".m3u8" not in url.lower():
                continue
            episodes.append({"name": name or f"第{len(episodes) + 1}集", "url": url})
        if episodes:
            lines.append(episodes)
    return lines


def fetch_site(source: dict, window_hours: int):
    api = source["api"]
    key = source["key"]
    got = []
    page = 1
    pagecount = 1
    while page <= min(pagecount, MAX_PAGES):
        params = {"ac": "detail", "h": window_hours, "pagesize": 100, "pg": page}
        try:
            r = requests.get(api, params=params, headers=HEADERS, timeout=25)
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:  # 单站失败不影响整轮
            log(f"[{key}] pg={page} 请求失败 {type(exc).__name__}: {exc}")
            break
        pagecount = int(payload.get("pagecount") or payload.get("page_count") or 1)
        batch = payload.get("list") or []
        if not batch:
            break
        got.extend(batch)
        log(f"[{key}] pg={page}/{min(pagecount, MAX_PAGES)} +{len(batch)} 累计 {len(got)}")
        page += 1
        time.sleep(0.6)
    time.sleep(SLEEP_BETWEEN_SITES)
    return got


def normalize(source_key: str, raw: dict, window_hours: int):
    title = (raw.get("vod_name") or "").strip()
    if not title:
        return None
    updated = parse_time(raw.get("vod_time") or "")
    if updated and updated < datetime.now() - timedelta(hours=window_hours + 12):
        # 站点忽略 h 时的兜底：多留 12 小时冗余
        return None
    lines = parse_play_url(raw.get("vod_play_url") or "")
    if not lines:
        return None
    douban_id = str(raw.get("vod_douban_id") or "0").strip()
    if douban_id in ("", "0"):
        douban_id = ""
    vod_id = str(raw.get("vod_id") or "").strip()
    uniq = f"douban:{douban_id}" if douban_id else f"{source_key}:{vod_id}"
    return {
        "uniq_key": uniq,
        "douban_id": douban_id,
        "title": title,
        "year": str(raw.get("vod_year") or "").strip(),
        "poster": (raw.get("vod_pic") or "").strip(),
        "class": (raw.get("vod_class") or "").strip(),
        "area": (raw.get("vod_area") or "").strip(),
        "remarks": (raw.get("vod_remarks") or "").strip(),
        "source_key": source_key,
        "vod_id": vod_id,
        "episodes": lines[0],
        "extra_lines": lines[1:],
        "updated_at": (raw.get("vod_time") or "").strip(),
    }


def pick_check_targets(items):
    """抽检：只抽一部分，避免短时间几百请求被 CDN 限流后大面积误判。"""
    limit = min(CHECK_SAMPLE_MAX, max(1, int(len(items) * CHECK_SAMPLE_RATIO)))
    return items[:limit]


def check_one(item):
    """抽检首集。只有「拿到响应但不是 m3u8」才记 fail；
    连接超时 / DNS 失败这类（Actions 海外 IP 常见）记 unknown，免得把好线路标死。"""
    url = item["episodes"][0]["url"]
    host = urlparse(url).netloc
    got_response = False
    for headers in (
        {"User-Agent": UA, "Range": "bytes=0-511"},
        {"User-Agent": UA, "Range": "bytes=0-511", "Referer": f"https://{host}/"},
    ):
        try:
            r = requests.get(url, headers=headers, timeout=CHECK_TIMEOUT, stream=True)
            got_response = True
            chunk = next(r.iter_content(512), b"") or b""
            r.close()
            if b"#EXTM3U" in chunk:
                return "ok"
        except Exception:
            continue
    return "fail" if got_response else "unknown"


def main():
    window_hours = int(os.environ.get("WINDOW_HOURS") or "72")
    sources = load_sources()
    log(f"[start] window_hours={window_hours} sources={[s['key'] for s in sources]}")

    by_source = {}
    for src in sources:
        raw_items = fetch_site(src, window_hours)
        normalized = [x for x in (normalize(src["key"], r, window_hours) for r in raw_items) if x]
        by_source[src["key"]] = normalized
        log(f"[{src['key']}] 原始 {len(raw_items)} -> 可用 {len(normalized)}")

    total_raw = sum(len(v) for v in by_source.values())

    # 跨站合并：同一 uniq_key 收成多线路
    merged: dict[str, dict] = {}
    without_url = 0
    for key, items in by_source.items():
        for it in items:
            slot = merged.get(it["uniq_key"])
            if slot is None:
                merged[it["uniq_key"]] = {
                    "uniq_key": it["uniq_key"],
                    "douban_id": it["douban_id"],
                    "title": it["title"],
                    "year": it["year"],
                    "poster": it["poster"],
                    "class": it["class"],
                    "area": it["area"],
                    "remarks": it["remarks"],
                    "lines": [
                        {
                            "source_key": key,
                            "vod_id": it["vod_id"],
                            "checked": "unknown",
                            "checked_at": 0,
                            "episodes": it["episodes"],
                        }
                    ],
                }
                continue
            if any(l["source_key"] == key for l in slot["lines"]):
                continue
            slot["lines"].append(
                {
                    "source_key": key,
                    "vod_id": it["vod_id"],
                    "checked": "unknown",
                    "checked_at": 0,
                    "episodes": it["episodes"],
                }
            )
            if not slot["douban_id"] and it["douban_id"]:
                slot["douban_id"] = it["douban_id"]

    # 二次合并：没有豆瓣 ID 的条目（多为 zuidazy）按「标题+年份」挂到有豆瓣 ID 的同名片子上，
    # 这样同一部片子就有第二条线路可切，而不是两条互不相干的卡片。
    def norm_title(value: str) -> str:
        return "".join(ch for ch in (value or "").lower() if ch.isalnum())

    by_title = {}
    for item in merged.values():
        if item["douban_id"]:
            by_title.setdefault((norm_title(item["title"]), item["year"]), item)
    merged_count_before = len(merged)
    for key in list(merged.keys()):
        item = merged.get(key)
        if item is None or item["douban_id"]:
            continue
        host = by_title.get((norm_title(item["title"]), item["year"]))
        if host is None or host is item:
            continue
        known = {l["source_key"] for l in host["lines"]}
        added = 0
        for line in item["lines"]:
            if line["source_key"] in known:
                continue
            host["lines"].append(line)
            known.add(line["source_key"])
            added += 1
        if added:
            merged.pop(key)
    log(f"[merge] 标题+年份 二次合并：{merged_count_before} -> {len(merged)}")

    items = list(merged.values())

    # 滚动累积：把上一版 latest.json 里还留有价值的条目并回来。
    # 每次只抓 WINDOW_HOURS 的新片，但老片不该从片库里消失（否则每天只剩最近 24h 的几十条）。
    RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS") or "14")
    now_ms0 = int(time.time() * 1000)
    keep_until = now_ms0 - RETENTION_DAYS * 86400 * 1000
    # 停用某个源以后，旧片库里它的线路不能又被 carry 带回来（否则 chip 上会一直躺着死线路）
    src_keys = {s["key"] for s in sources}
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, encoding="utf-8") as f:
                old_payload = json.load(f)
            old_items = old_payload.get("items") or []
        except Exception:
            old_items = []
        index = {it.get("uniq_key"): it for it in items}
        carried = 0
        for oi in old_items:
            key = oi.get("uniq_key")
            if not key:
                continue
            slot = index.get(key)
            if slot is None:
                last_seen = int(oi.get("last_seen") or now_ms0)
                if last_seen >= keep_until:
                    oi["lines"] = [l for l in (oi.get("lines") or []) if l.get("source_key") in src_keys]
                    if not oi["lines"]:
                        continue
                    oi["last_seen"] = last_seen
                    index[key] = oi
                    carried += 1
                continue
            # 同一条片子：新抓的线路覆盖旧线路，旧线路里的其它站保留（只保留还启用的站）
            known = {l["source_key"] for l in slot["lines"]}
            for line in oi.get("lines") or []:
                if line.get("source_key") not in known and line.get("source_key") in src_keys:
                    slot["lines"].append(line)
        for it in index.values():
            it["last_seen"] = now_ms0
        items = list(index.values())
        log(f"[carry] 承接旧片库 {carried} 条（保留 {RETENTION_DAYS} 天），合并后 {len(items)} 条")

    # 抽检第一批线路
    flat = [(item, line) for item in items for line in item["lines"] if not line["checked_at"]]
    targets = flat[: min(CHECK_SAMPLE_MAX, max(1, int(len(flat) * CHECK_SAMPLE_RATIO)))]
    if targets:
        log(f"[check] 抽检 {len(targets)}/{len(flat)} 条线路")
        with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
            results = list(pool.map(lambda pair: check_one({"episodes": pair[1]["episodes"]}), targets))
        now_ms = int(time.time() * 1000)
        for (item, line), result in zip(targets, results):
            line["checked"] = result
            line["checked_at"] = now_ms

    ok = sum(1 for i in items for l in i["lines"] if l["checked"] == "ok")
    fail = sum(1 for i in items for l in i["lines"] if l["checked"] == "fail")
    unknown = sum(1 for i in items for l in i["lines"] if l["checked"] == "unknown")
    has_douban = sum(1 for i in items if i["douban_id"])

    payload = {
        "schema": SCHEMA,
        "generated_at": int(time.time() * 1000),
        "window_hours": window_hours,
        "count": len(items),
        "stats": {
            "total_raw": total_raw,
            "merged_uniq": len(items),
            "multi_line_items": sum(1 for i in items if len(i["lines"]) > 1),
            "without_play_url": without_url,
            "lines_checked_ok": ok,
            "lines_checked_fail": fail,
            "lines_unchecked": unknown,
            "has_douban_id": has_douban,
            "without_douban_id": len(items) - has_douban,
        },
        "items": items,
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(OUT_FILE) / 1024
    log(
        "[done] count={} lines_ok={} lines_fail={} douban={} size={:.0f}KB window={}h".format(
            len(items), ok, fail, has_douban, size_kb, window_hours
        )
    )
    return 0 if items else 1


if __name__ == "__main__":
    sys.exit(main())

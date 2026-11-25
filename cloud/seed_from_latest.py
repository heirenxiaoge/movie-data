#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 movie-data/latest.json 转成 out/raw.json（与 fetch.py 同格式），用于首次灌库。

latest.json 里一条长这样：
  {"uniq_key":"douban:38382368","douban_id":"38382368","title":"战斗之魂","year":"2026",
   "poster":"https://...jpg","class":"日韩动漫","area":"日本","remarks":"更新至08集",
   "lines":[{"source_key":"wujin","vod_id":"172323","episodes":[{"name":"第01集","url":"https://.../202610/07/x/video/index.m3u8"}]}]}

「更新时间」在 latest.json 里没有字段，只能从分集 URL 的 /YYYYMM/DD/ 推断；
推不出就给抓取当天（灌库时不会算成「今天更新」以外的问题）。
"""

from __future__ import annotations

import json
import os
import re
import sys

from common import log, map_area, map_kind, norm_title, now_iso, today

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "out")


def _first_existing(*paths: str) -> str:
    for p in paths:
        if os.path.exists(p):
            return p
    return paths[0]


# 三种放法都认：cloud/ 在仓库根（../latest.json）、本地并列目录（../movie-data/）、同目录。
LATEST = _first_existing(
    os.path.join(ROOT, "..", "latest.json"),
    os.path.join(ROOT, "..", "movie-data", "latest.json"),
    os.path.join(ROOT, "latest.json"),
)

URL_DAY = re.compile(r"/(20\d{2})(\d{2})/(\d{2})/")


def day_from_url(url: str) -> str:
    m = URL_DAY.search(url or "")
    if not m:
        return ""
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def convert(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items") or []
    log(f"latest.json 共 {len(items)} 条")

    out: list[dict] = []
    for item in items:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        norm = norm_title(title)
        year = str(item.get("year") or "").strip()
        cls = item.get("class") or ""
        remarks = item.get("remarks") or ""
        kind = ""  # 集数后才好判断，见下面按线路补
        area = map_area(item.get("area") or cls)
        poster = item.get("poster") or ""
        douban_id = str(item.get("douban_id") or "").strip()

        for line in item.get("lines") or []:
            site = (line.get("source_key") or "").strip()
            vod_id = str(line.get("vod_id") or "").strip()
            eps_raw = line.get("episodes") or []
            if not site or not vod_id or not eps_raw:
                continue
            eps = []
            for idx, ep in enumerate(eps_raw, start=1):
                url = ep.get("url") or ""
                if not url.startswith("http"):
                    continue
                eps.append(
                    {
                        "n": idx,
                        "name": (ep.get("name") or f"第{idx:02d}集").strip(),
                        "url": url,
                    }
                )
            if not eps:
                continue
            days = [day_from_url(e["url"]) for e in eps]
            days = [d for d in days if d]
            updated_at = max(days) if days else today()
            kind = map_kind(cls, remarks, len(eps))
            out.append(
                {
                    "site": site,
                    "vod_id": vod_id,
                    "douban_id": douban_id,
                    "title": title,
                    "title_norm": norm,
                    "year": year,
                    "area": area,
                    "kind": kind,
                    "remarks": remarks,
                    "poster": poster,
                    "updated_at": updated_at,
                    "ep_count": len(eps),
                    "eps": eps,
                }
            )
    return out


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else LATEST
    if not os.path.exists(src):
        log(f"找不到 {src}")
        return 2
    items = convert(src)
    os.makedirs(OUT_DIR, exist_ok=True)
    dst = os.path.join(OUT_DIR, "raw.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(
            {"generated_at": now_iso(), "window_hours": 0, "items": items},
            f,
            ensure_ascii=False,
        )
    with_douban = sum(1 for i in items if i["douban_id"])
    log(f"已写出 {dst}：{len(items)} 条线路记录，其中带豆瓣 id {with_douban} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())

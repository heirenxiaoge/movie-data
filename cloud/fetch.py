#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""增量采集：从苹果 CMS 资源站拉「最近更新」的条目。

三级降级链（越靠前越省请求）：
  L1  ac=detail&h=<window>             —— 服务端按更新时间过滤，最理想
  L2  ac=list&h=<window> 只取 id，再 ac=detail&ids=a,b,c 批量取详情
  L3  ac=list&pg=N 翻页，本地按 vod_id 水位线过滤（CMS 的 vod_id 基本单调递增）

产物：out/raw.json —— 归一化后的条目列表，交给 match.py 做聚合。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from common import (
    PAGE_SLEEP,
    SITE_SLEEP,
    day_of,
    dump_json,
    get_json,
    log,
    map_area,
    map_kind,
    norm_title,
    now_iso,
    parse_play_url,
    today,
)

ROOT = os.path.dirname(os.path.abspath(__file__))


def _first_existing(*paths: str) -> str:
    for p in paths:
        if os.path.exists(p):
            return p
    return paths[0]


# 站点清单复用 movie-data 仓库里那份，避免两处维护。
# 三种放法都认：cloud/ 在仓库根（../sources.json）、本地并列目录（../movie-data/）、同目录。
SOURCES_FILE = _first_existing(
    os.path.join(ROOT, "..", "sources.json"),
    os.path.join(ROOT, "..", "movie-data", "sources.json"),
    os.path.join(ROOT, "sources.json"),
)
STATE_FILE = os.path.join(ROOT, "out", "state.json")

MAX_PAGES_L1 = 40        # 单站单次最多翻 40 页（约 800 条）
MAX_PAGES_L3 = 30        # 水位线扫描上限，防止首次跑太久
BATCH_IDS = 60           # ac=detail&ids= 每批条数


def load_sources(path: str = SOURCES_FILE):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [s for s in data if s.get("enabled") and s.get("api")]


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------
def normalize(site: str, vod: dict, fallback_day: str = "") -> dict | None:
    vid = str(vod.get("vod_id") or "").strip()
    title = (vod.get("vod_name") or "").strip()
    if not vid or not title:
        return None
    lines = parse_play_url(vod.get("vod_play_url") or "")
    if not lines:
        return None
    # 合并同一站的多条线路，集数取最长的一条
    eps = max(lines, key=len) if lines else []
    remarks = (vod.get("vod_remarks") or "").strip()
    day = day_of(vod.get("vod_time") or "") or fallback_day or today()
    return {
        "site": site,
        "vod_id": vid,
        "douban_id": str(vod.get("vod_douban_id") or "").strip(),
        "title": title,
        "title_norm": norm_title(title),
        "year": str(vod.get("vod_year") or "").strip(),
        "area": map_area(vod.get("vod_area") or ""),
        "kind": map_kind(vod.get("vod_class") or "", remarks, len(eps)),
        "remarks": remarks,
        "poster": (vod.get("vod_pic") or "").strip(),
        "updated_at": day,
        "ep_count": len(eps),
        "eps": eps,
    }


# --------------------------------------------------------------------------
# L1 / L2 / L3
# --------------------------------------------------------------------------
def _detail_pages(api: str, h: int, max_pages: int):
    """ac=detail&h= 翻页，返回 (vods, pagecount)。"""
    vods: list[dict] = []
    first = get_json(api, {"ac": "detail", "h": h, "pg": 1})
    pagecount = int(first.get("pagecount") or 1)
    vods.extend(first.get("list") or [])
    for pg in range(2, min(pagecount, max_pages) + 1):
        time.sleep(PAGE_SLEEP)
        try:
            data = get_json(api, {"ac": "detail", "h": h, "pg": pg})
        except Exception as exc:  # 单页失败不放弃整站
            log(f"    L1 pg={pg} failed: {exc}")
            break
        batch = data.get("list") or []
        if not batch:
            break
        vods.extend(batch)
    return vods, pagecount


def fetch_l1(api: str, window: int) -> list[dict]:
    vods, pagecount = _detail_pages(api, window, MAX_PAGES_L1)
    log(f"    L1 ac=detail&h={window} -> {len(vods)} 条 / {pagecount} 页")
    return vods


def fetch_l2(api: str, window: int) -> list[dict]:
    """ac=list&h= 只拿 id，再批量 ac=detail&ids= 取详情。"""
    ids: list[str] = []
    try:
        first = get_json(api, {"ac": "list", "h": window, "pg": 1})
    except Exception as exc:
        log(f"    L2 ac=list failed: {exc}")
        return []
    pagecount = int(first.get("pagecount") or 1)
    for row in first.get("list") or []:
        vid = str(row.get("vod_id") or "").strip()
        if vid:
            ids.append(vid)
    for pg in range(2, min(pagecount, MAX_PAGES_L1) + 1):
        time.sleep(PAGE_SLEEP)
        try:
            data = get_json(api, {"ac": "list", "h": window, "pg": pg})
        except Exception:
            break
        batch = data.get("list") or []
        if not batch:
            break
        for row in batch:
            vid = str(row.get("vod_id") or "").strip()
            if vid:
                ids.append(vid)

    ids = list(dict.fromkeys(ids))
    if not ids:
        log("    L2 ac=list 没返回 id")
        return []

    vods: list[dict] = []
    for i in range(0, len(ids), BATCH_IDS):
        chunk = ids[i : i + BATCH_IDS]
        time.sleep(PAGE_SLEEP)
        try:
            data = get_json(api, {"ac": "detail", "ids": ",".join(chunk)})
        except Exception as exc:
            log(f"    L2 ids 批次 {i // BATCH_IDS + 1} failed: {exc}")
            continue
        vods.extend(data.get("list") or [])
    log(f"    L2 ac=list -> {len(ids)} id, 取回详情 {len(vods)} 条")
    return vods


def fetch_l3(api: str, watermark: int) -> tuple[list[dict], int]:
    """翻页扫 vod_id > watermark，返回 (vods, 新水位线)。"""
    vods: list[dict] = []
    new_mark = watermark
    for pg in range(1, MAX_PAGES_L3 + 1):
        if pg > 1:
            time.sleep(PAGE_SLEEP)
        try:
            data = get_json(api, {"ac": "detail", "pg": pg})
        except Exception as exc:
            log(f"    L3 pg={pg} failed: {exc}")
            break
        batch = data.get("list") or []
        if not batch:
            break
        fresh = []
        for row in batch:
            try:
                vid = int(row.get("vod_id") or 0)
            except (TypeError, ValueError):
                vid = 0
            if vid > watermark:
                fresh.append(row)
            new_mark = max(new_mark, vid)
        vods.extend(fresh)
        # 这一页已经全部是旧条目，说明水位线过了最新区间
        if not fresh:
            break
    log(f"    L3 水位线 {watermark} -> {new_mark}，新增 {len(vods)} 条")
    return vods, new_mark


# --------------------------------------------------------------------------
# 单站
# --------------------------------------------------------------------------
def fetch_site(site: dict, window: int, watermark: int) -> tuple[list[dict], dict]:
    key, api = site["key"], site["api"]
    log(f"[{key}] {site.get('name', '')} {api}")
    state = {"watermark": watermark, "method": "", "count": 0}

    vods: list[dict] = []
    method = ""
    try:
        vods = fetch_l1(api, window)
        method = "L1"
    except Exception as exc:
        log(f"  L1 失败：{exc}")

    if not vods:
        try:
            vods = fetch_l2(api, window)
            method = "L2"
        except Exception as exc:
            log(f"  L2 失败：{exc}")

    new_mark = watermark
    if not vods:
        try:
            vods, new_mark = fetch_l3(api, watermark)
            method = "L3"
        except Exception as exc:
            log(f"  L3 失败：{exc}")

    items = []
    for vod in vods:
        row = normalize(key, vod)
        if row:
            items.append(row)
    # L1/L2 也要推进水位线，避免下次仍从旧位点扫
    for row in items:
        try:
            new_mark = max(new_mark, int(row["vod_id"]))
        except (TypeError, ValueError):
            pass

    state.update({"watermark": new_mark, "method": method or "none", "count": len(items)})
    log(f"[{key}] 归一化后 {len(items)} 条（{method or 'none'}），水位线 {new_mark}")
    return items, state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=30, help="增量时间窗（小时）")
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "raw.json"))
    ap.add_argument("--sites", default="", help="只跑指定站，逗号分隔")
    ap.add_argument("--full", action="store_true", help="忽略水位线，走 L1 全量窗口")
    args = ap.parse_args()

    sources = load_sources()
    if args.sites:
        want = {s.strip() for s in args.sites.split(",") if s.strip()}
        sources = [s for s in sources if s["key"] in want]

    state = load_state()
    all_items: list[dict] = []
    for idx, site in enumerate(sources):
        if idx:
            time.sleep(SITE_SLEEP)
        key = site["key"]
        watermark = 0 if args.full else int(state.get(key, {}).get("watermark") or 0)
        try:
            items, st = fetch_site(site, args.window, watermark)
        except Exception as exc:
            log(f"[{key}] 整体失败：{exc}")
            continue
        state[key] = {**st, "updated_at": now_iso()}
        all_items.extend(items)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(
            {"generated_at": now_iso(), "window_hours": args.window, "items": all_items},
            f,
            ensure_ascii=False,
        )
    save_state(state)
    log(f"共 {len(all_items)} 条 -> {args.out}")
    log("站点状态：" + dump_json(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())

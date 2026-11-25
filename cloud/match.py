#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聚合 + 生成 D1 SQL。

输入：out/raw.json（fetch.py 产物，或 seed_from_latest.py 产物）
     data/douban_seed.json（可选：[{sid,title,year,area,kind,rating,poster,title_norm}]）
输出：out/upsert.sql（subjects / links / facets / state 的 upsert 语句）
     out/pending.jsonl（低置信度匹配，留给人工确认）

聚合规则：
  1. 有 vod_douban_id 的直接归到 sid=<douban_id>；
  2. 没有豆瓣 id 的先按 (title_norm, year) 找已有豆瓣主体（含种子），命中就挂成它的线路；
  3. 还找不到就按 (title_norm, year) 跨站聚类，sid 用 `cms:<site>:<vod_id>` 占位；
  4. 同一 sid 的多个线路合并进 lines_json，同站多条只留集数最多的一条。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

from common import dump_json, log, now_iso, today

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "out")
SEED_FILE = os.path.join(ROOT, "data", "douban_seed.json")

# 每 120 集一段：D1 单条语句 100KB 上限，长剧必须切开
EP_CHUNK = 120


def combo_key(kind: str = "", area: str = "", year: str = "") -> str:
    """分面 key，Worker 端必须用完全相同的拼法；无条件是 'all'。"""
    parts = []
    if kind:
        parts.append("kind=" + kind)
    if area:
        parts.append("area=" + area)
    if year:
        parts.append("year=" + year)
    return "|".join(parts) if parts else "all"


def load_seed() -> list[dict]:
    if not os.path.exists(SEED_FILE):
        return []
    try:
        with open(SEED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log(f"豆瓣种子读取失败：{exc}")
        return []


def title_key(norm: str, year: str) -> str:
    return f"{norm}|{year or ''}"


def pick(counter: Counter, default: str = "") -> str:
    if not counter:
        return default
    return counter.most_common(1)[0][0]


def aggregate(raw_items: list[dict], seed: list[dict]):
    """返回 (subjects, links, pending)。"""
    seed_by_sid: dict[str, dict] = {}
    seed_by_key: dict[str, str] = {}
    for row in seed:
        sid = str(row.get("sid") or "").strip()
        if not sid:
            continue
        seed_by_sid[sid] = row
        norm = row.get("title_norm") or ""
        if norm:
            seed_by_key.setdefault(title_key(norm, str(row.get("year") or "")), sid)

    buckets: dict[str, dict] = {}
    known_keys: dict[str, str] = dict(seed_by_key)
    pending: list[dict] = []

    # 第一轮：带豆瓣 id 的直接占位
    for row in raw_items:
        did = row.get("douban_id") or ""
        if not did:
            continue
        bucket = buckets.setdefault(did, {"sid": did, "rows": [], "hint": ""})
        bucket["rows"].append(row)
        known_keys.setdefault(title_key(row["title_norm"], row["year"]), did)

    # 第二轮：无豆瓣 id 的按标题+年份挂靠；挂不上就跨站聚类
    orphans: dict[str, list[dict]] = defaultdict(list)
    for row in raw_items:
        if row.get("douban_id"):
            continue
        key = title_key(row["title_norm"], row["year"])
        sid = known_keys.get(key)
        if sid:
            buckets.setdefault(sid, {"sid": sid, "rows": [], "hint": ""})["rows"].append(row)
        else:
            orphans[key].append(row)

    for key, rows in orphans.items():
        best = sorted(rows, key=lambda r: (r["ep_count"], r["updated_at"]), reverse=True)[0]
        norm, year = key.split("|", 1)
        # 没有年份、标题又太短，跨站聚类很容易把不同片子并到一起 —— 只进 pending 不上线
        if not year and len(norm) <= 3 and len({r["site"] for r in rows}) > 1:
            for r in rows:
                pending.append({**r, "reason": "ambiguous_without_year"})
            continue
        sid = f"cms:{best['site']}:{best['vod_id']}"
        buckets[sid] = {"sid": sid, "rows": rows, "hint": best["title"]}
        known_keys.setdefault(key, sid)

    subjects: list[dict] = []
    links: list[dict] = []
    line_rows: list[dict] = []
    for sid, bucket in buckets.items():
        rows = bucket["rows"]
        if not rows:
            continue
        by_site: dict[str, dict] = {}
        for row in rows:
            old = by_site.get(row["site"])
            if old is None or row["ep_count"] > old["ep_count"]:
                by_site[row["site"]] = row

        seed_row = seed_by_sid.get(sid, {})
        titles = Counter(r["title"] for r in rows)
        years = Counter(r["year"] for r in rows if r["year"])
        areas = Counter(r["area"] for r in rows if r["area"])
        kinds = Counter(r["kind"] for r in rows if r["kind"] and r["kind"] != "其他")
        remarks = [r["remarks"] for r in rows if r["remarks"]]
        posters = [r["poster"] for r in rows if r["poster"]]

        title = seed_row.get("title") or pick(titles, bucket["hint"])
        year = str(seed_row.get("year") or pick(years, ""))
        area = seed_row.get("area") or pick(areas, "")
        kind = seed_row.get("kind") or pick(kinds, "其他")
        # 备注取最长的：「更新至12集」比「更新至3集」新
        remark = max(remarks, key=len) if remarks else ""

        lines = []
        for site, row in sorted(by_site.items(), key=lambda kv: -kv[1]["ep_count"]):
            eps = row["eps"]
            # subjects.lines_json 只留摘要：整份分集走 subject_lines 分块表，
            # 否则 700 集的动漫会让单条 INSERT 超过 D1 的 100KB 语句上限。
            lines.append({"site": site, "vod_id": row["vod_id"], "ep": len(eps)})
            for part, start in enumerate(range(0, len(eps), EP_CHUNK)):
                chunk = eps[start : start + EP_CHUNK]
                if not chunk:
                    continue
                line_rows.append(
                    {
                        "sid": sid,
                        "site": site,
                        "vod_id": row["vod_id"],
                        "part": part,
                        "ep_from": int(chunk[0]["n"]),
                        "ep_to": int(chunk[-1]["n"]),
                        "eps": [{"n": e["n"], "name": e["name"], "url": e["url"]} for e in chunk],
                    }
                )
            has_id = bool(row.get("douban_id"))
            links.append(
                {
                    "sid": sid,
                    "site": site,
                    "vod_id": row["vod_id"],
                    "match_type": "exact_id" if has_id else ("title_year" if sid in seed_by_sid else "cluster"),
                    "confidence": 1.0 if has_id else (0.9 if sid in seed_by_sid else 0.8),
                    "ep_count": len(eps),
                    "last_seen_at": row["updated_at"],
                }
            )

        subjects.append(
            {
                "sid": sid,
                "douban_id": sid if sid.isdigit() else "",
                "title": title,
                "title_norm": seed_row.get("title_norm") or rows[0]["title_norm"],
                "year": year,
                "area": area,
                "kind": kind,
                "rating": seed_row.get("rating"),
                "poster": seed_row.get("poster") or (posters[0] if posters else ""),
                "remarks": remark,
                "ep_count": max(r["ep_count"] for r in rows),
                "updated_at": max(r["updated_at"] for r in rows),
                "lines": lines,
                "meta_src": "douban" if seed_row else "cms",
            }
        )

    subjects.sort(key=lambda s: (s["updated_at"], s["sid"]), reverse=True)
    return subjects, links, pending, line_rows


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------
def sql_str(value) -> str:
    if value is None:
        return "NULL"
    s = str(value).replace("'", "''").replace("\r", " ").replace("\n", " ")
    return "'" + s + "'"


def sql_num(value) -> str:
    if value in (None, ""):
        return "NULL"
    try:
        return str(float(value))
    except (TypeError, ValueError):
        return "NULL"


def emit_sql(subjects: list[dict], links: list[dict], line_rows: list[dict], out_path: str) -> None:
    stamp = now_iso()
    day = today()
    chunks: list[str] = ["-- generated by match.py at " + stamp, "PRAGMA foreign_keys = ON;"]

    for sub in subjects:
        chunks.append(
            "INSERT INTO subjects(sid,douban_id,title,title_norm,year,area,kind,rating,poster,remarks,"
            "ep_count,has_play,updated_at,lines_json,meta_src,created_at) VALUES ("
            + ",".join(
                [
                    sql_str(sub["sid"]),
                    sql_str(sub["douban_id"] or None),
                    sql_str(sub["title"]),
                    sql_str(sub["title_norm"]),
                    sql_str(sub["year"] or None),
                    sql_str(sub["area"] or None),
                    sql_str(sub["kind"] or None),
                    sql_num(sub["rating"]),
                    sql_str(sub["poster"] or None),
                    sql_str(sub["remarks"] or None),
                    str(int(sub["ep_count"])),
                    "1",
                    sql_str(sub["updated_at"]),
                    sql_str(dump_json(sub["lines"])),
                    sql_str(sub["meta_src"]),
                    sql_str(stamp),
                ]
            )
            + ") ON CONFLICT(sid) DO UPDATE SET "
            "title=excluded.title,title_norm=excluded.title_norm,year=excluded.year,area=excluded.area,"
            "kind=excluded.kind,rating=COALESCE(excluded.rating,subjects.rating),"
            "poster=COALESCE(excluded.poster,subjects.poster),remarks=excluded.remarks,"
            "ep_count=excluded.ep_count,has_play=1,lines_json=excluded.lines_json,"
            "meta_src=excluded.meta_src,"
            # 只有集数变多或线路内容变了才算「今天更新」，否则会天天把全表刷成今天
            "updated_at=CASE WHEN excluded.ep_count > subjects.ep_count "
            "OR excluded.lines_json <> subjects.lines_json "
            "THEN excluded.updated_at ELSE subjects.updated_at END;"
        )

    for ln in links:
        chunks.append(
            "INSERT INTO links(sid,site,vod_id,match_type,confidence,status,ep_count,last_seen_at,created_at) VALUES ("
            + ",".join(
                [
                    sql_str(ln["sid"]),
                    sql_str(ln["site"]),
                    sql_str(ln["vod_id"]),
                    sql_str(ln["match_type"]),
                    sql_num(ln["confidence"]),
                    "'approved'",
                    str(int(ln["ep_count"])),
                    sql_str(ln["last_seen_at"]),
                    sql_str(stamp),
                ]
            )
            + ") ON CONFLICT(sid,site,vod_id) DO UPDATE SET "
            "match_type=excluded.match_type,confidence=excluded.confidence,status='approved',"
            "ep_count=excluded.ep_count,last_seen_at=excluded.last_seen_at;"
        )

    # 分集明细：先按 sid 清掉旧块，再写入新块（线路/集数变了也不会残留脏数据）
    for sid in {r["sid"] for r in line_rows}:
        chunks.append("DELETE FROM subject_lines WHERE sid=" + sql_str(sid) + ";")
    for row in line_rows:
        chunks.append(
            "INSERT INTO subject_lines(sid,site,vod_id,part,ep_from,ep_to,eps_json) VALUES ("
            + ",".join(
                [
                    sql_str(row["sid"]),
                    sql_str(row["site"]),
                    sql_str(row["vod_id"]),
                    str(int(row["part"])),
                    str(int(row["ep_from"])),
                    str(int(row["ep_to"])),
                    sql_str(dump_json(row["eps"])),
                ]
            )
            + ") ON CONFLICT(sid,site,vod_id,part) DO UPDATE SET "
            "ep_from=excluded.ep_from,ep_to=excluded.ep_to,eps_json=excluded.eps_json;"
        )

    # 分面整表重建：几百行，成本可忽略
    chunks.append("DELETE FROM facets;")
    chunks.append(
        "INSERT INTO facets(combo_key,total,refreshed_at) SELECT 'all', COUNT(*), "
        + sql_str(day)
        + " FROM subjects WHERE has_play=1;"
    )
    for cols in ["kind", "area", "year", "kind, area", "kind, year", "area, year", "kind, area, year"]:
        names = [c.strip() for c in cols.split(",")]
        key_expr = "||'|'||".join(f"'{c}='||COALESCE({c},'')" for c in names)
        chunks.append(
            "INSERT INTO facets(combo_key,total,refreshed_at) SELECT "
            + key_expr
            + ", COUNT(*), "
            + sql_str(day)
            + " FROM subjects WHERE has_play=1 GROUP BY "
            + cols
            + ";"
        )

    chunks.append(
        "UPDATE links SET status='stale' WHERE status='approved' AND last_seen_at IS NOT NULL "
        f"AND last_seen_at < date({sql_str(day)}, '-30 day');"
    )
    chunks.append(
        "UPDATE subjects SET has_play = CASE WHEN EXISTS("
        "SELECT 1 FROM links l WHERE l.sid = subjects.sid AND l.status='approved') THEN 1 ELSE 0 END;"
    )
    chunks.append(
        "INSERT INTO state(k,v,updated_at) VALUES ('last_run',"
        + sql_str(stamp)
        + ","
        + sql_str(stamp)
        + ") ON CONFLICT(k) DO UPDATE SET v=excluded.v,updated_at=excluded.updated_at;"
    )

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(chunks))
    log(f"SQL 已写出：{out_path}（{len(subjects)} subjects / {len(links)} links / {len(chunks)} 条语句）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=os.path.join(OUT_DIR, "raw.json"))
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "upsert.sql"))
    ap.add_argument("--pending", default=os.path.join(OUT_DIR, "pending.jsonl"))
    args = ap.parse_args()

    if not os.path.exists(args.raw):
        log(f"找不到 {args.raw}，先跑 fetch.py 或 seed_from_latest.py")
        return 2

    with open(args.raw, "r", encoding="utf-8") as f:
        payload = json.load(f)
    raw_items = payload.get("items") or []
    log(f"输入 {len(raw_items)} 条线路记录")

    seed = load_seed()
    log(f"豆瓣种子 {len(seed)} 条")

    subjects, links, pending, line_rows = aggregate(raw_items, seed)
    with_douban = sum(1 for s in subjects if s["douban_id"])
    log(
        f"聚合出 {len(subjects)} 部片子（有豆瓣 id {with_douban} 部），"
        f"pending {len(pending)} 条，分集分块 {len(line_rows)} 行"
    )

    emit_sql(subjects, links, line_rows, args.out)

    if pending:
        os.makedirs(os.path.dirname(args.pending) or ".", exist_ok=True)
        with open(args.pending, "w", encoding="utf-8") as f:
            for row in pending:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

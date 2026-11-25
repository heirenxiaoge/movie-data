/**
 * NPLike 云端片库 API（Cloudflare Worker + D1）
 *
 * 设计要点：
 *   1. 只读为主：列表 / 详情 / 播放三条 GET。App 不下载任何基础数据。
 *   2. 列表用 keyset 分页（updated_at + sid 游标），不用 OFFSET —— 每页固定读 ~N 行，
 *      D1 免费额度（500 万行读/天）能撑住个人使用。
 *   3. 列表不返回 lines_json，只有详情才返回，单页响应 6~8KB。
 *   4. 分面总数从 facets 表读（每日任务整表重建），不做 COUNT(*)。
 *   5. 全部接口校验 X-App-Key，避免被当成公开接口刷。
 */

export interface Env {
  DB: D1Database;
  APP_KEY?: string;
  MAX_PAGE_SIZE?: string;
}

type Row = Record<string, unknown>;

const DEFAULT_PAGE_SIZE = 24;

const json = (data: unknown, init: ResponseInit = {}) =>
  new Response(JSON.stringify(data), {
    ...init,
    headers: {
      "content-type": "application/json; charset=utf-8",
      ...(init.headers || {}),
    },
  });

const bad = (msg: string, code = 400) => json({ error: msg }, { status: code });

/** base64url 编码，Workers 里没有 Buffer */
function b64url(s: string): string {
  return btoa(unescape(encodeURIComponent(s)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function unb64url(s: string): string {
  const pad = s.length % 4 === 0 ? "" : "=".repeat(4 - (s.length % 4));
  const raw = atob(s.replace(/-/g, "+").replace(/_/g, "/") + pad);
  return decodeURIComponent(escape(raw));
}

function parseCursor(raw: string | null): { key: string; sid: string } | null {
  if (!raw) return null;
  try {
    const s = unb64url(raw);
    const i = s.lastIndexOf("|");
    if (i <= 0) return null;
    return { key: s.slice(0, i), sid: s.slice(i + 1) };
  } catch {
    return null;
  }
}

function makeCursor(key: string, sid: string): string {
  return b64url(`${key}|${sid}`);
}

/** 组合键必须在 Python 端保持一致：kind=剧集|area=韩国|year=2026 */
function comboKey(kind: string, area: string, year: string): string {
  const parts: string[] = [];
  if (kind) parts.push(`kind=${kind}`);
  if (area) parts.push(`area=${area}`);
  if (year) parts.push(`year=${year}`);
  return parts.length ? parts.join("|") : "all";
}

function authed(req: Request, env: Env): boolean {
  const expect = env.APP_KEY || "";
  if (!expect) return true; // 没配置口令就不校验（本地 wrangler dev 方便）
  const got = req.headers.get("x-app-key") || "";
  return got === expect;
}

// ---------------------------------------------------------------------------
// 列表：GET /api/v1/subjects?kind=&area=&year=&q=&sort=updated|rating&cursor=&size=
// ---------------------------------------------------------------------------
async function listSubjects(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const kind = (url.searchParams.get("kind") || "").trim();
  const area = (url.searchParams.get("area") || "").trim();
  const year = (url.searchParams.get("year") || "").trim();
  const q = (url.searchParams.get("q") || "").trim();
  const sort = (url.searchParams.get("sort") || "updated").trim();
  const maxSize = Number(env.MAX_PAGE_SIZE || 48);
  const size = Math.min(Math.max(Number(url.searchParams.get("size") || DEFAULT_PAGE_SIZE), 1), maxSize);
  const cursor = parseCursor(url.searchParams.get("cursor"));

  const where: string[] = ["has_play = 1"];
  const args: unknown[] = [];
  if (kind) {
    where.push("kind = ?");
    args.push(kind);
  }
  if (area) {
    where.push("area = ?");
    args.push(area);
  }
  if (year) {
    where.push("year = ?");
    args.push(year);
  }
  if (q) {
    where.push("title LIKE ?");
    args.push(`%${q}%`);
  }

  const orderKey = sort === "rating" ? "rating" : "updated_at";
  // rating 可能有 NULL，统一用 COALESCE 参与游标，避免 NULL 排序不稳
  const keyExpr = sort === "rating" ? "COALESCE(rating, 0)" : "updated_at";
  if (cursor) {
    where.push(`(${keyExpr} < ? OR (${keyExpr} = ? AND sid < ?))`);
    args.push(sort === "rating" ? Number(cursor.key) : cursor.key, sort === "rating" ? Number(cursor.key) : cursor.key, cursor.sid);
  }

  const sql =
    `SELECT sid, title, year, area, kind, rating, poster, remarks, ep_count, updated_at, has_play ` +
    `FROM subjects WHERE ${where.join(" AND ")} ` +
    `ORDER BY ${keyExpr} DESC, sid DESC LIMIT ?`;
  const rows = await env.DB.prepare(sql)
    .bind(...args, size + 1)
    .all<Row>();

  const list = rows.results || [];
  const hasMore = list.length > size;
  const page = hasMore ? list.slice(0, size) : list;
  const last = page[page.length - 1];
  const nextCursor =
    hasMore && last
      ? makeCursor(sort === "rating" ? String(last.rating ?? 0) : String(last.updated_at), String(last.sid))
      : null;

  // 总数取分面表；筛选条件没命中分面组合时退回 all
  let total: number | null = null;
  const key = comboKey(kind, area, year);
  try {
    const f = await env.DB.prepare("SELECT total FROM facets WHERE combo_key = ?").bind(key).first<{ total: number }>();
    total = f?.total ?? null;
    if (total === null && (kind || area || year)) {
      const f2 = await env.DB.prepare("SELECT total FROM facets WHERE combo_key = 'all'").first<{ total: number }>();
      total = f2?.total ?? null;
    }
  } catch {
    total = null;
  }

  const body = {
    items: page,
    nextCursor,
    hasMore,
    total,
    sort,
    pageSize: size,
  };

  // 稳定查询走边缘缓存：同一筛选 60 秒内不重复打 D1
  const etag = `W/"${size}-${list.length}-${nextCursor ?? ""}-${page.map((r) => r.sid).join(",").length}"`;
  const cache = caches.default;
  const cacheKey = new Request(url.toString(), { method: "GET" });
  if (req.headers.get("if-none-match") === etag) {
    return new Response(null, { status: 304, headers: { etag } });
  }
  const hit = await cache.match(cacheKey);
  if (hit) {
    const clone = new Response(hit.body, hit);
    clone.headers.set("etag", etag);
    clone.headers.set("x-cache", "hit");
    return clone;
  }
  const res = json(body, {
    headers: {
      "cache-control": "public, max-age=60",
      etag,
      "x-cache": "miss",
    },
  });
  await cache.put(cacheKey, res.clone());
  return res;
}

// ---------------------------------------------------------------------------
// 详情：GET /api/v1/subjects/:sid
// ---------------------------------------------------------------------------
type LineMeta = { site?: string; vod_id?: string; ep?: number };
type Ep = { n?: number; name?: string; url?: string };

// 分集明细存在 subject_lines（每 120 集一块），这里按 site 合并回 lines[].eps
async function loadEps(env: Env, sid: string): Promise<Map<string, Ep[]>> {
  const rows = await env.DB.prepare(
    `SELECT site, vod_id, part, eps_json FROM subject_lines
     WHERE sid = ? ORDER BY site, vod_id, part`
  )
    .bind(sid)
    .all<{ site: string; vod_id: string; part: number; eps_json: string }>();
  const out = new Map<string, Ep[]>();
  for (const r of rows.results || []) {
    const key = `${r.site}\u0000${r.vod_id}`;
    let list = out.get(key);
    if (!list) {
      list = [];
      out.set(key, list);
    }
    try {
      const chunk = JSON.parse(String(r.eps_json || "[]")) as Ep[];
      for (const e of chunk) list.push(e);
    } catch {
      // 单块坏了不影响其它块
    }
  }
  return out;
}

async function getSubject(env: Env, sid: string): Promise<Response> {
  const row = await env.DB.prepare(
    `SELECT sid, douban_id, title, title_norm, year, area, kind, rating, poster, remarks,
            ep_count, has_play, updated_at, lines_json, meta_src
     FROM subjects WHERE sid = ?`
  )
    .bind(sid)
    .first<Row>();
  if (!row) return bad("not found", 404);

  let metas: LineMeta[] = [];
  try {
    metas = JSON.parse(String(row.lines_json || "[]")) as LineMeta[];
  } catch {
    metas = [];
  }
  const epsByLine = await loadEps(env, sid);
  const lines = metas.map((m) => ({
    site: m.site,
    vod_id: m.vod_id,
    ep: m.ep ?? (epsByLine.get(`${m.site}\u0000${m.vod_id}`)?.length || 0),
    eps: epsByLine.get(`${m.site}\u0000${m.vod_id}`) || [],
  }));

  const { lines_json, ...rest } = row;
  return json(
    { ...rest, lines },
    { headers: { "cache-control": "public, max-age=120" } }
  );
}

// ---------------------------------------------------------------------------
// 播放：GET /api/v1/subjects/:sid/play?site=&ep=  → 302 真实地址
// ---------------------------------------------------------------------------
async function play(env: Env, sid: string, url: URL): Promise<Response> {
  const site = (url.searchParams.get("site") || "").trim();
  const ep = Number(url.searchParams.get("ep") || 0);

  const row = await env.DB.prepare("SELECT lines_json FROM subjects WHERE sid = ?")
    .bind(sid)
    .first<{ lines_json: string }>();
  if (!row) return bad("not found", 404);

  let metas: LineMeta[] = [];
  try {
    metas = JSON.parse(row.lines_json || "[]") as LineMeta[];
  } catch {
    return bad("broken lines", 500);
  }
  if (!metas.length) return bad("no source", 404);

  const line = site ? metas.find((l) => l.site === site) : metas[0];
  if (!line) return bad("no source for site", 404);

  // 只取需要的块：ep 落在 ep_from..ep_to 之间
  const want = ep > 0 ? ep : 1;
  const chunk = await env.DB.prepare(
    `SELECT eps_json FROM subject_lines
     WHERE sid = ? AND site = ? AND vod_id = ? AND ep_from <= ? AND ep_to >= ?
     ORDER BY part LIMIT 1`
  )
    .bind(sid, String(line.site || ""), String(line.vod_id || ""), want, want)
    .first<{ eps_json: string }>();

  let eps: Ep[] = [];
  if (chunk) {
    try {
      eps = JSON.parse(String(chunk.eps_json || "[]")) as Ep[];
    } catch {
      eps = [];
    }
  } else {
    // 集号不一定连续，退化成取该线路第一块
    const first = await env.DB.prepare(
      `SELECT eps_json FROM subject_lines
       WHERE sid = ? AND site = ? AND vod_id = ? ORDER BY part LIMIT 1`
    )
      .bind(sid, String(line.site || ""), String(line.vod_id || ""))
      .first<{ eps_json: string }>();
    try {
      eps = JSON.parse(String(first?.eps_json || "[]")) as Ep[];
    } catch {
      eps = [];
    }
  }
  if (!eps.length) return bad("no source for site", 404);

  const target = ep > 0 ? eps.find((e) => Number(e.n) === ep) : eps[0];
  const playUrl = target?.url || eps[0]?.url || "";
  if (!/^https?:\/\//i.test(playUrl)) return bad("bad url", 500);

  // 302 到真实地址：App 端 ExoPlayer 直接跟随，同时这里可做播放统计/签名
  return new Response(null, {
    status: 302,
    headers: {
      location: playUrl,
      "cache-control": "no-store",
      "x-source-site": String(line.site || ""),
      "x-episode": String(target?.n || 1),
    },
  });
}

// ---------------------------------------------------------------------------
// 分面：GET /api/v1/facets  → { all: 489, "kind=剧集": 229, ... }
// ---------------------------------------------------------------------------
async function getFacets(env: Env): Promise<Response> {
  const rows = await env.DB.prepare("SELECT combo_key, total FROM facets").all<{ combo_key: string; total: number }>();
  const map: Record<string, number> = {};
  for (const r of rows.results || []) map[r.combo_key] = r.total;
  return json(map, { headers: { "cache-control": "public, max-age=300" } });
}

// ---------------------------------------------------------------------------
// 上报豆瓣元数据：POST /api/v1/report/subjects
// body: [{sid,title,year,area,kind,rating,poster}]
// ---------------------------------------------------------------------------
async function reportSubjects(req: Request, env: Env): Promise<Response> {
  let payload: unknown;
  try {
    payload = await req.json();
  } catch {
    return bad("bad json");
  }
  const list = Array.isArray(payload) ? payload : [];
  if (!list.length) return json({ ok: true, upserted: 0 });

  const now = new Date().toISOString();
  const stmt = env.DB.prepare(
    `INSERT INTO douban_cache(sid, title, year, area, kind, rating, poster, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(sid) DO UPDATE SET
       title=excluded.title, year=excluded.year, area=excluded.area, kind=excluded.kind,
       rating=excluded.rating, poster=excluded.poster, updated_at=excluded.updated_at`
  );
  const batch = list.slice(0, 200).map((raw) => {
    const r = raw as Row;
    return stmt.bind(
      String(r.sid ?? ""),
      String(r.title ?? ""),
      String(r.year ?? ""),
      String(r.area ?? ""),
      String(r.kind ?? ""),
      r.rating == null ? null : Number(r.rating),
      String(r.poster ?? ""),
      now
    );
  });
  await env.DB.batch(batch);
  return json({ ok: true, upserted: batch.length });
}

// ---------------------------------------------------------------------------
// 上报坏链：POST /api/v1/report/dead  body: [{sid, site, vod_id}]
// ---------------------------------------------------------------------------
async function reportDead(req: Request, env: Env): Promise<Response> {
  let payload: unknown;
  try {
    payload = await req.json();
  } catch {
    return bad("bad json");
  }
  const list = Array.isArray(payload) ? payload : [];
  if (!list.length) return json({ ok: true, marked: 0 });

  const now = new Date().toISOString().slice(0, 10);
  const stmt = env.DB.prepare(
    "UPDATE links SET status='dead', last_seen_at=? WHERE sid=? AND site=? AND vod_id=?"
  );
  const batch = list.slice(0, 200).map((raw) => {
    const r = raw as Row;
    return stmt.bind(now, String(r.sid ?? ""), String(r.site ?? ""), String(r.vod_id ?? ""));
  });
  await env.DB.batch(batch);

  // 该片如果没有其它存活线路，就把 has_play 置 0，列表里不再出现
  const sids = Array.from(new Set(list.map((raw) => String((raw as Row).sid ?? "")))).filter(Boolean);
  const fix = env.DB.prepare(
    `UPDATE subjects SET has_play = CASE WHEN EXISTS(
       SELECT 1 FROM links l WHERE l.sid = subjects.sid AND l.status='approved') THEN 1 ELSE 0 END
     WHERE sid = ?`
  );
  await env.DB.batch(sids.map((sid) => fix.bind(sid)));

  return json({ ok: true, marked: batch.length });
}

// ---------------------------------------------------------------------------
export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    if (req.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-headers": "x-app-key, content-type, if-none-match",
          "access-control-allow-methods": "GET, POST, OPTIONS",
        },
      });
    }

    if (url.pathname === "/api/v1/health") {
      return json({ ok: true, ts: Date.now() });
    }

    if (!authed(req, env)) return bad("invalid app key", 401);

    const p = url.pathname;
    try {
      if (req.method === "GET" && p === "/api/v1/subjects") return await listSubjects(req, env);
      if (req.method === "GET" && p === "/api/v1/facets") return await getFacets(env);

      const playMatch = p.match(/^\/api\/v1\/subjects\/([^/]+)\/play$/);
      if (req.method === "GET" && playMatch) return await play(env, decodeURIComponent(playMatch[1]), url);

      const detailMatch = p.match(/^\/api\/v1\/subjects\/([^/]+)$/);
      if (req.method === "GET" && detailMatch) return await getSubject(env, decodeURIComponent(detailMatch[1]));

      if (req.method === "POST" && p === "/api/v1/report/subjects") return await reportSubjects(req, env);
      if (req.method === "POST" && p === "/api/v1/report/dead") return await reportDead(req, env);

      return bad("not found", 404);
    } catch (err) {
      return json({ error: String((err as Error)?.message || err) }, { status: 500 });
    }
  },
};

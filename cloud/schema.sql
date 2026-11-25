-- NPLike 云端片库（Cloudflare D1）
--
-- 设计约束：
--   1. 只存「有播放源」的条目 —— 原始 20~25 万条留在 Actions 中间产物里，不进库。
--   2. App 不下载任何基础数据，列表/详情/播放全部走 Worker。
--   3. 列表接口不读 lines_json，只读索引列；单页固定 24 行（keyset 分页）。
--
-- 用法（首次）：
--   wrangler d1 create nplike
--   wrangler d1 execute nplike --remote --file=./schema.sql
--   wrangler d1 execute nplike --remote --file=./out/seed.sql

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 影片主表：一条 = App 列表里的一张卡
-- sid 优先用豆瓣 id；没有豆瓣 id 的用 cms:<site>:<vod_id> 占位
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subjects (
  sid         TEXT PRIMARY KEY,
  douban_id   TEXT,                     -- 有豆瓣 id 时非空，便于详情页跳转
  title       TEXT    NOT NULL,
  title_norm  TEXT    NOT NULL,         -- 归一化标题，供匹配用
  year        TEXT,
  area        TEXT,
  kind        TEXT,                     -- 电影/剧集/动漫/综艺/纪录片…
  rating      REAL,                     -- 豆瓣评分，没有则 NULL
  poster      TEXT,
  remarks     TEXT,                     -- 「更新至12集」「全24集」
  ep_count    INTEGER NOT NULL DEFAULT 0,
  has_play    INTEGER NOT NULL DEFAULT 1,
  updated_at  TEXT    NOT NULL,         -- 资源站最近更新日 YYYY-MM-DD
  lines_json  TEXT    NOT NULL,         -- [{site,vod_id,eps:[{n,name,url}]}]
  meta_src    TEXT,                     -- douban / cms / report
  created_at  TEXT    NOT NULL,
  CHECK (length(sid) > 0)
);

-- 列表主索引：kind + area + year，按更新时间倒序 + sid 兜底保证 keyset 稳定
CREATE INDEX IF NOT EXISTS idx_subjects_filter
  ON subjects(kind, area, year, updated_at DESC, sid DESC);
CREATE INDEX IF NOT EXISTS idx_subjects_updated
  ON subjects(has_play, updated_at DESC, sid DESC);
CREATE INDEX IF NOT EXISTS idx_subjects_rating
  ON subjects(has_play, rating DESC, sid DESC);
CREATE INDEX IF NOT EXISTS idx_subjects_norm
  ON subjects(year, title_norm);

-- ---------------------------------------------------------------------------
-- 线路明细：一条 = 某站的某个 vod_id 对到某个 sid
-- status: approved / pending / stale / dead
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS links (
  sid          TEXT NOT NULL,
  site         TEXT NOT NULL,
  vod_id       TEXT NOT NULL,
  match_type   TEXT,                    -- exact_id / title_year / fuzzy / manual
  confidence   REAL,
  status       TEXT NOT NULL DEFAULT 'approved',
  ep_count     INTEGER NOT NULL DEFAULT 0,
  last_seen_at TEXT,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (sid, site, vod_id)
);
CREATE INDEX IF NOT EXISTS idx_links_sid  ON links(sid, status);
CREATE INDEX IF NOT EXISTS idx_links_seen ON links(last_seen_at);

-- ---------------------------------------------------------------------------
-- 分集明细：一条 = 某条线路的一段分集（长剧按 chunk 切开）
--
-- 为什么要拆：D1 单条 SQL 语句有 100KB 上限，700 集的动漫把整个 eps 塞进
-- subjects.lines_json 会触发 SQLITE_TOOBIG。这里每 120 集一行，
-- subjects.lines_json 只留 {site,vod_id,ep} 的摘要。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subject_lines (
  sid      TEXT    NOT NULL,
  site     TEXT    NOT NULL,
  vod_id   TEXT    NOT NULL,
  part     INTEGER NOT NULL,            -- 从 0 开始
  ep_from  INTEGER NOT NULL,            -- 本块第一集的 n
  ep_to    INTEGER NOT NULL,            -- 本块最后一集的 n
  eps_json TEXT    NOT NULL,            -- [{n,name,url}]
  PRIMARY KEY (sid, site, vod_id, part)
);
CREATE INDEX IF NOT EXISTS idx_lines_sid_site ON subject_lines(sid, site, part);

-- ---------------------------------------------------------------------------
-- 分面计数：避免列表接口 COUNT(*)
-- combo_key 例：'all'、'kind=剧集'、'kind=剧集|area=韩国'、'kind=|area=|year=2026'
-- 由每日增量任务整表重建（几百行，成本可忽略）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facets (
  combo_key    TEXT PRIMARY KEY,
  total        INTEGER NOT NULL,
  refreshed_at TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- 元数据缓存：App 上报的豆瓣条目 + 匹配时补全的信息
-- 云端不主动抓豆瓣（公共云 IP 会被封），靠 App 浏览时上报自然生长
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS douban_cache (
  sid         TEXT PRIMARY KEY,
  title       TEXT,
  year        TEXT,
  area        TEXT,
  kind        TEXT,
  rating      REAL,
  poster      TEXT,
  updated_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 运行状态：水位线、上次全量时间等（key-value）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS state (
  k          TEXT PRIMARY KEY,
  v          TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

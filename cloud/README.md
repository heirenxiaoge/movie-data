# 云片库（Cloudflare Worker + D1）

豆瓣条目 × 4 个苹果CMS 资源站的「匹配 + 播放地址」在云端每天算一次，App 只调 Worker。
不下载基础数据、不在手机上做匹配，也不依赖 R2。

```
movie-data/latest.json ─┐
                        ├─► match.py ─► out/upsert.sql ─► D1 (nplike)
4 个 CMS 站 (fetch.py) ─┘                                   │
                                                            ▼
                                        Worker  /api/v1/...  ◄── App
```

## 目录

| 文件 | 作用 |
| --- | --- |
| `schema.sql` | D1 表结构（subjects / subject_lines / links / facets / douban_cache / state） |
| `common.py` | 标题归一化、类型/地区映射、播放地址解析、SQL 转义 |
| `fetch.py` | 增量采集：L1 `ac=detail&h` → L2 `ac=list&h` 取 id 再 `ac=detail&ids` → L3 `vod_id` 水位线 |
| `match.py` | 聚合 + 匹配（exact_id 1.0 / title_year 0.9 / cluster 0.8），产出 `out/upsert.sql` 与 `out/pending.jsonl` |
| `seed_from_latest.py` | 用 `movie-data/latest.json` 冷启动一轮（没有 token 也能先跑） |
| `worker/src/index.ts` | Worker 五接口（列表 / 详情 / 302 播放 / facets / 上报） |
| `.github/workflows/catalog.yml` | 每天 18:30 UTC 增量 + 直写 D1 |

## 一次性初始化

```bash
wrangler login
cd cloud/worker && wrangler d1 create nplike      # 把 database_id 写回 wrangler.toml
wrangler d1 execute nplike --remote --yes --file=../schema.sql
wrangler deploy                                    # 得到 https://nplike-catalog.<subdomain>.workers.dev
```

已有数据库时（本项目当前状态）：

- `database_id = a33bf158-a973-4607-a57f-10bf36129758`
- 线上地址 `https://nplike-catalog.heirenxiaoge.workers.dev`
- 首轮灌库：`wrangler d1 execute nplike --remote --yes --file=cloud/out/upsert.sql`

## 日常增量

GitHub Actions（`.github/workflows/catalog.yml`）需要两个 secret：

- `CLOUDFLARE_API_TOKEN`：带 `D1:Edit` 权限
- `CLOUDFLARE_ACCOUNT_ID`：`b11b5b7deeaeb924237023a71f553307`

手动跑一次：

```bash
cd cloud
python fetch.py --window 30
python match.py
cd worker && npx wrangler@4 d1 execute nplike --remote --yes --file=../out/upsert.sql
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/subjects?kind=&area=&year=&q=&sort=updated\|rating&cursor=&size=24` | keyset 分页；`sort=updated` 即 App 的「只看最新更新」 |
| GET | `/api/v1/subjects/:sid` | 详情，`lines[].eps[]` 已按 120 集分块合并 |
| GET | `/api/v1/subjects/:sid/play?site=&ep=` | 302 到真实 m3u8，带 `x-source-site` / `x-episode` |
| GET | `/api/v1/facets` | 预计算的分面总数（列表页 total 来源） |
| POST | `/api/v1/report/subjects` | App 上报豆瓣元数据（补 `rating` / `douban_id`） |
| POST | `/api/v1/report/dead` | App 上报坏链，接口把对应 line 标 dead 并重算 `has_play` |

除 `/api/v1/health` 与 `OPTIONS` 外都校验 `x-app-key`；`APP_KEY` 未设置时放行（方便本地 dev），
设置后 App 设置页的「口令」必须填同一个值：

```bash
cd cloud/worker && wrangler secret put APP_KEY
```

## App 侧

`设置 → 云片库`：打开开关 + 填 Worker 根地址（不要结尾斜杠）+ 口令。
云请求走「设置」里的代理/订阅节点（`*.workers.dev` 在国内常被 DNS 污染），
所以要先能翻墙；请求失败时影视列表自动回落到豆瓣 + 本地资源站缓存。

本地联调（不想部署时）：

```bash
cd cloud/worker && npx wrangler@4 dev --local --port 8787
adb reverse tcp:8787 tcp:8787        # 模拟器/真机都能用 127.0.0.1:8787
# App 设置页地址填 http://127.0.0.1:8787
```

## 排障

**线上地址打不开 / 列表回落到豆瓣**

本机与模拟器实测：`nplike-catalog.heirenxiaoge.workers.dev` 的 A 记录被投毒成
`103.73.161.52`（另有假 IPv6 `2a03:2880:*:face:b00c:0:25de`），直连、换 CF edge IP、
`curl --resolve` 全部超时。这是链路问题，不是 Worker 的问题。

- 真机：开启「设置 → 网络代理 → 用订阅节点」，内核（Xray）带 sniffing，会按 SNI 重新解析，
  能把请求救回来；装好后先用 `设置 → 云片库` 填线上地址试一次列表。
- 模拟器：arm64 的 `libxray.so` 跑不了（SIGILL），回退的自研代理不做域名嗅探，公网地址必失败；
  模拟器只能用 `adb reverse` + `http://127.0.0.1:8787` 联调。
- 判断是哪种失败：`记录 logcat | grep CloudApi`，或看 `wrangler tail nplike-catalog` 有没有请求。
  tail 也会因为同样的污染连不上，必要时在自己能翻墙的机器上跑。

**改 `library.json` 手滑**

App 内改设置优先；直接改文件要先 `adb shell am force-stop com.example.nplike`，再
`run-as com.example.nplike cat/cp`，否则被内存状态盖回去。用 `adb shell input text` 填长 URL 前
先确认旧值长度，删多删少都会拼出脏值。

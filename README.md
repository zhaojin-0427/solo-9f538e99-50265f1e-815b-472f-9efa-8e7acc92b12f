# PM2.5 修订采集与查询平台

市环境监测中心会补正各站点的小时 PM2.5,分析作业需按原版复算。本平台包含:

- **source**(模拟源站,FastAPI):分页发布修订。每次重置或服务重启都会生成新
  **代际**(`generation`),版号从 v1 重新编号:首版(v1)含全量站点,此后每版只列
  新增、改值(`upsert`)和删除(`delete`)。所有数据接口响应都携带 `generation`、
  连续版号 `version`、页号 `page`、总页数 `total_pages`。管理接口可发布/切换修订、
  重置(换代际)、注入缺页、超时、同版同站冲突、分页期间重置等故障。
- **collector**(采集与查询 API,FastAPI + SQLite):采集某 UTC 小时前先固定源站
  **代际与目标版**;代际不变则逐版补齐增量,代际变化则从新代际首版**全量重建**,
  增量只叠加同代际快照;每个确认版的完整快照均可按 (代际, 版号) 查询;
  数据与当前指针(代际+版号)在同一事务生效。

## 快速开始

```bash
docker compose up --build
```

- 采集/查询 API: http://localhost:8000 (文档 `/docs`)
- 模拟源站: http://localhost:8001 (文档 `/docs`)

端到端冒烟(60+ 项断言,覆盖缺页/超时/冲突/重置换代际/分页期间代际变化):

```bash
python3 scripts/smoke.py
```

## 典型流程

```bash
# 1. 采集某 UTC 小时(先固定源站代际与目标版,再逐版补齐)
curl -X POST localhost:8000/collections -H 'Content-Type: application/json' \
     -d '{"hour": "2026-11-18T10"}'

# 2. 源站发布新修订(可切换修订;也可带 {"ops":[...]} 显式指定)
curl -X POST localhost:8001/admin/publish

# 3. 再次采集,逐版补齐到新固定版(同代际增量)
curl -X POST localhost:8000/collections -H 'Content-Type: application/json' \
     -d '{"hour": "2026-11-18T10"}'

# 4. 查询:当前指针(默认)或任一已确认 (代际, 版号) 当时的完整数据
curl localhost:8000/hours/2026-11-18T10/data
curl 'localhost:8000/hours/2026-11-18T10/data?version=1'
curl 'localhost:8000/hours/2026-11-18T10/data?generation=<代际>&version=3&station_id=S07'
curl localhost:8000/hours/2026-11-18T10/versions
```

## 源站重置(代际切换)

```bash
# 源站重置:生成新代际,版号从 v1 重新编号(服务重启同样产生新代际)
curl -X POST localhost:8001/admin/reset -H 'Content-Type: application/json' \
     -d '{"station_count": 20, "seed": 999}'

# 采集端识别代际变化:从新代际首版全量重建,绝不把新 v1/v2 叠到旧代际快照上;
# 新代际首版确认后,默认查询才切换到新代际
curl -X POST localhost:8000/collections -H 'Content-Type: application/json' \
     -d '{"hour": "2026-11-18T10"}'

# 旧代际已确认版仍可查
curl "localhost:8000/hours/2026-11-18T10/data?generation=<旧代际>&version=6"
```

## 故障注入演示(缺页 / 超时 / 同版同站冲突 / 分页期间重置)

```bash
# 下一版第 1 页:超时 1 次(重试可成功,且不混入上次残页)
curl -X PUT localhost:8001/admin/faults -H 'Content-Type: application/json' -d \
  '{"faults":[{"version":2,"page":1,"kind":"timeout","times":1}]}'

# 分页期间源站重置(代际切换):采集失败并保留原供数,再次采集即按新代际重建
curl -X PUT localhost:8001/admin/faults -H 'Content-Type: application/json' -d \
  '{"faults":[{"version":1,"page":2,"kind":"reset","seed":4242,"times":1}]}'

# kind 还可取 missing(缺页 404)、conflict(同版同站不同值);times=-1 表示持续生效
curl -X DELETE localhost:8001/admin/faults   # 清除全部故障
```

采集端对每版最多重试 `MAX_ATTEMPTS` 次(每次从第 1 页整版重取,并逐页校验代际);
仍失败则**保留原供数**(当前指针不动,默认查询继续提供原完整数据),该小时状态记为
`failed`,清除故障后再次采集即可继续。

## 需求映射

| 需求 | 实现 |
| --- | --- |
| 采集时固定源站代际和目标版 | `collector/app/service.py::_collect`:开头一次 `GET /api/revisions/current` 固定 `(generation, current_version)`,之后逐版抓取 |
| 重置/重启生成新代际,新代际从首版全量重建 | 源站 `_reset` 生成新 `generation`(启动与 `/admin/reset` 都会调用);采集端发现代际不同则从 v1 起重建,v1 以空快照为基 |
| 增量只叠加同代际快照 | 快照按 `(hour, generation, version)` 存储;第 N 版增量只应用于同代际第 N-1 版快照 |
| 同号版不误报"已最新" | `up_to_date` 仅在代际相同且版号相同才成立;代际不同一律重建 |
| 分页期间代际变化/首版失败时继续提供原供数 | 每页校验 `generation`,变化即失败;失败只更新状态元信息,`current_generation/current_version` 指针不动 |
| 重试不沿用残页 | 页面只存内存、从不落库;每次重试从第 1 页整版重取(`_fetch_version`) |
| 新代际首版确认后才切换默认查询 | 首版的快照、版本登记与指针切换在同一事务提交(`db.py::tx`);首版确认前默认查询仍指向旧代际 |
| 按代际、版号读取旧快照 | `GET /hours/{hour}/data?generation=G&version=N`;`generation` 缺省取当前代际,只给代际时取该代际最新确认版 |
| 原 SQLite 卷中的已确认版仍可查,重启后不混代 | 代际是快照主键维度,SQLite WAL + 数据卷持久化;旧版库(无代际列)启动时自动迁移到 `legacy` 代际,已确认数据保留可查,新采集按真实代际重建 |
| 失败或重启不得暴露半版 | 事务原子提交;启动时仅复位 `collecting` 状态元信息 |

## API 一览

**collector(8000)**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/collections` | 采集某 UTC 小时,body `{"hour":"YYYY-MM-DDTHH"}`;采集中返回 409 |
| GET | `/collections/{hour}` | 该小时采集状态/当前代际与版号指针/最近错误 |
| GET | `/hours` | 全部小时及当前指针 |
| GET | `/hours/{hour}/versions` | 已确认版列表(含代际) |
| GET | `/hours/{hour}/data` | 某代际某版完整数据;`generation` 缺省取当前代际,`version` 缺省取当前版,`station_id` 可单站过滤 |

**source(8001)**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/revisions/current` | 当前代际与版号 |
| GET | `/api/revisions` | 全部修订元信息(含代际) |
| GET | `/api/revisions/{v}/pages/{p}` | 某版某页(`generation`/`version`/`page`/`total_pages`/`items`) |
| POST | `/admin/publish` | 发布下一修订(缺省自动生成确定性增量,可传 `ops`) |
| POST | `/admin/reset` | 重置:生成新代际并从首版重新编号(可指定站点数与种子) |
| GET/PUT/DELETE | `/admin/faults` | 故障注入: `missing`/`timeout`/`conflict`/`reset`,`times` 控制触发次数 |
| GET | `/admin/state` | 源站内部状态 |

## 本地开发(不用 Docker)

```bash
pip install -r source/requirements.txt -r collector/requirements.txt
(cd source && uvicorn app.main:app --port 8001) &
(cd collector && SOURCE_BASE_URL=http://localhost:8001 DB_PATH=/tmp/collector.db \
    uvicorn app.main:app --port 8000) &
python3 scripts/smoke.py
```

# 山洪水位预警裁定服务

基于 **Python + FastAPI + SQLite** 的山洪水位预警裁定服务：监测站/设备登记、读数幂等补传、阈值版本管理（防汛方案调整，**支持登记前不落库预演**）、按规则自动裁定预警与解除、审核修订并重算、**防汛责任区跨站联动预警**、发布冻结公开结果。

## 一键启动

### 方式一：Docker Compose（推荐）

```bash
docker compose up --build
```

### 方式二：Docker

```bash
docker build -t flood-warning-service .
docker run -d -p 8000:8000 -v flood-data:/data --name flood-warning flood-warning-service
```

### 方式三：本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动时自动完成 SQLite 建表初始化（含阈值版本表、责任区表），无需额外迁移步骤；已有数据库重启后会自动补齐新表。

## 地址与端口

| 项 | 地址 |
|---|---|
| 服务地址 | http://127.0.0.1:8000 |
| 交互式 API 文档（Swagger） | http://127.0.0.1:8000/docs |
| OpenAPI  schema | http://127.0.0.1:8000/openapi.json |
| 健康检查 | `GET /health` |

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FLOOD_DB_PATH` | `./data/flood.db`（容器内 `/data/flood.db`） | SQLite 数据库文件路径 |
| `FLOOD_MIN_WATER_LEVEL` | `-50` | 水位合法下限，超出报 400 |
| `FLOOD_MAX_WATER_LEVEL` | `1000` | 水位合法上限，超出报 400 |
| `FLOOD_SEGMENT_GAP_MINUTES` | `15` | 相邻读数间隔超过该值即断段 |
| `FLOOD_RISE_MIN_COUNT` | `3` | 预警所需同段连续达上升阈值读数个数 |
| `FLOOD_RISE_MIN_SPAN_MINUTES` | `10` | 预警所需连续段首尾最小间隔（分钟） |
| `FLOOD_CLEAR_MIN_COUNT` | `2` | 解除所需连续不高于解除阈值读数个数 |
| `FLOOD_CLEAR_MIN_SPAN_MINUTES` | `5` | 解除所需连续段首尾最小间隔（分钟） |

## 裁定规则

1. 有效（未作废）读数按**观测时间**升序重排，相邻间隔 **> 15 分钟**即断段；
2. **预警**：同段内连续 **≥ 3** 个读数水位 **≥ 该读数当时生效的上升阈值**，且该连续段首尾相隔 **≥ 10 分钟**——在首次同时满足两项条件的读数时刻发出预警；
3. **解除**：预警状态下连续 **≥ 2** 个读数水位 **≤ 该读数当时生效的解除阈值**，且首尾相隔 **≥ 5 分钟**——在满足条件的读数时刻解除；
4. 断段只重置上升计数；**断段本身不自动解除预警**，解除计数可跨段延续；
5. **阈值版本**：每条有效读数按观测时间采用**最近已生效**的阈值版本，**同一生效时刻采用新版本**；早于首个版本生效时间的读数使用站点**登记基准阈值**；跨版本的连续读数分别按各自阈值判断，断段与解除规则不变；
6. **阈值调整本身不产生预警或解除事件**，只改变之后（及重算时）各读数采用的阈值；
7. 审核员**作废/更正**读数后，待发布裁定按**全部有效读数**立即重算；**发布**将各站时间线冻结为公开结果，之后的补传/修订/阈值调整只影响待发布结果，**重新发布**才更新公开结果，历史发布快照保持不变。

## 阈值版本（防汛方案调整）

防汛方案调整后，可对监测站登记带**生效时间（必带时区）**、**上升阈值**、**解除阈值**和**原因**的阈值版本，并查询版本历史：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/stations/{id}/threshold-versions` | 登记一版阈值：`{effective_at, rise_threshold, clear_threshold, reason}` |
| GET | `/stations/{id}/threshold-versions` | 版本历史（按生效时间升序），含登记基准 `baseline` |

校验与语义：

- `effective_at` 必须带时区（如 `2026-10-02T08:00:00+08:00` 或 `...Z`），否则 400；服务端按同一瞬间归一化比较，**同站重复生效时间 409**（不同时区表示同一时刻也算重复；不同站互不影响）；
- 阈值必须为**有限数值**（NaN / Infinity 拒绝），且**上升阈值严格大于解除阈值**，否则 400；`reason` 必填；
- 站点不存在返回 **404**；
- 登记成功的响应附带 `pending_adjudication`（重算后的待发布状态），但其中**不会因调整动作本身新增任何事件**——所有事件仍由读数判定产生；
- 版本按 `effective_at <= 观测时间` 取最晚一版（边界包含，同刻用新版）；未命中时回退登记阈值；
- 时间线响应新增每读数采用的阈值信息：`readings[].{reading_id, observed_at, water_level, rise_threshold, clear_threshold, threshold_version_id}`（`threshold_version_id` 为 `null` 表示基准值）；`events[]` 也附带触发当时的 `rise_threshold / clear_threshold / threshold_version_id`。原有顶层字段与其他接口的请求响应保持不变。

## 阈值调整预演（登记前不落库演练）

防汛人员在**正式登记阈值版本前**，可先预演该项调整对**待发布预警**的影响：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/stations/{id}/threshold-versions/preview` | 预演一版阈值调整的影响，**不落库、不改变待发布查询或已冻结发布** |

- 请求体与正式登记**完全相同**：`{effective_at, rise_threshold, clear_threshold, reason}`，沿用同一套校验与冲突规则——必带时区、阈值有限且上升严格大于解除、原因必填（非法 → **400**）；未知站 → **404**；既有版本与候选**同一生效瞬间**（不同时区表示同一时刻也算）→ **409**；
- 预演版本不写入数据库：版本历史不可见、待发布时间线/区域结果不变、历史发布快照不变；
- 裁定语义与正式登记后完全一致：各读数按观测时间在“既有版本 + 候选版本”中取最近已生效版本，**生效同刻采用候选新值**；候选之后若还有既有版本，到其生效时刻仍由既有版本接替；候选版本在时间线中以临时 id **`-1`** 标识（正式登记后换成真实版本 id）；
- 责任区联动：返回**该站及所有包含它的责任区**的 `current`（现状）与 `preview`（预演），区域**同一时刻合并所有站变化后再按 K 判定**；责任区内**其他成员站按现有待发布结果参与联动**（仅目标站套用候选版本）；
- 整个结果取自**同一只读数据视图**（单连接、进程内锁，全程无写入）；在无其他写入时，随后正式登记得到的**状态、事件类型与时刻与预演一致**。

响应结构（时间线字段与 `GET /stations/{id}/timeline`、`GET /zones/{id}/timeline` 相同）：

```json
{
  "dry_run": true,
  "station_id": 6,
  "candidate": {
    "effective_at": "2026-10-01T03:00:00Z",
    "rise_threshold": 9.0,
    "clear_threshold": 7.0,
    "reason": "入汛提级预演",
    "threshold_version_id": -1
  },
  "current": { "published": false, "status": "normal",  "events": [], "readings": [] },
  "preview": { "published": false, "status": "warning", "events": [], "readings": [] },
  "zones": [
    {
      "zone_id": 3,
      "zone_name": "南岭片区",
      "trigger_count": 2,
      "current": {
        "published": false, "status": "normal",
        "warning_station_ids": [7], "warning_station_count": 1,
        "events": [],
        "members": [ {"station_id": 6, "warning": false}, {"station_id": 7, "warning": true} ]
      },
      "preview": {
        "published": false, "status": "warning",
        "warning_station_ids": [6, 7], "warning_station_count": 2,
        "events": [ {"type": "warning", "at": "2026-10-01T03:10:00Z",
                     "warning_station_ids": [6, 7], "warning_station_count": 2} ],
        "members": []
      }
    }
  ]
}
```

> 预演不改变任何数据：反复调用安全；确认无误后再调
> `POST /stations/{id}/threshold-versions` 正式登记。

## 防汛责任区（跨站联动预警）

防汛人员按责任区查看多个监测站的**跨站联动**结果。登记责任区时提供：

- `name`：**非空**名称（纯空白拒绝；同名责任区允许并存）；
- `effective_at`：**必带时区**的生效时间；
- `station_ids`：成员监测站 ID 列表，**互不重复**且至少 1 个，均须为已登记站；
- `trigger_count`：触发站数 K，**1 ≤ K ≤ 成员站数量**。

校验：未知站 → **404**（响应给出 `unknown_station_ids`）；重复成员 / 无时区 / 空白名称 / 空成员 / K 越界 → **400**。**登记后配置保持不变**（不提供修改、删除接口）。

联动裁定规则：

1. **自生效时刻起**，按**观测时间**重放成员站待发布时间线中的预警 / 解除事件；生效时刻之前的区域不产生事件；
2. **此前仍在预警的站计入初始状态**：成员站在生效时刻之前最后一个事件是预警（其后无解除）即视为生效时仍预警；**初始即满足 K** 时，在**生效时刻**发出区域预警；
3. **同一时刻先合并所有站的状态变化**，再对预警站数计数：达到 K 发出区域预警、降到 K 以下解除；**每个时刻最多一个区域事件**（同刻一站解除、另一站预警时按合并结果一次性判定，不抖动）；
4. 区域可反复预警 / 解除；查询返回区域当前 `status`、事件 `events` 时间线，以及**每个事件发生时仍在预警的站 ID 列表** `warning_station_ids`（升序）与计数；
5. 成员站的**补传、审核修订、阈值版本调整**后，待发布区域结果**立即整体重算**（与单站重算语义一致：不成立的历史事件随之消失）；
6. **发布时与各站时间线一同冻结**：之后的任何变化只影响待发布结果，重新发布才更新公开结果，**历史发布快照保持不变**；责任区在某次发布之后才登记的，该次历史发布不含它；**尚无发布时公开查询明确返回 404 无结果**。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/zones` | 登记责任区，响应含配置与 `pending_zone`（实时重算的待发布区域结果） |
| GET | `/zones` | 责任区列表（含成员站及登记顺序） |
| GET | `/zones/{id}` | 责任区配置（登记后不变） |
| GET | `/zones/{id}/timeline` | 待发布区域联动结果（实时重算） |
| GET | `/public/zones/{id}/timeline` | 该责任区最近一次发布冻结的联动结果（无发布 → 404） |

待发布区域时间线字段：

```json
{
  "published": false,
  "zone_id": 1,
  "zone_name": "青石峡片区",
  "status": "warning",                 // normal | warning（当前区域状态）
  "active_warning_since": "2026-10-01T00:10:00Z",
  "trigger_count": 2,                  // K
  "warning_station_count": 2,          // 当前预警成员站数
  "warning_station_ids": [1, 2],       // 当前预警成员站 ID（升序）
  "events": [
    {
      "type": "warning",               // warning | cleared
      "at": "2026-10-01T00:10:00Z",
      "warning_station_ids": [1, 2],   // 事件发生时预警的站 ID
      "warning_station_count": 2
    }
  ],
  "members": [
    {"station_id": 1, "station_name": "甲站", "warning": true,  "warning_before_effective": false},
    {"station_id": 2, "station_name": "乙站", "warning": true,  "warning_before_effective": false}
  ]
}
```

`POST /publish` 的响应、`GET /publications/{id}` 与 `GET /public/current` 在原有字段之外**新增** `zones` / `zone_timelines`（公开负载中均带 `published: true`）；原有字段与其他接口的请求响应保持不变。

## API 一览

### 监测站与设备| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/stations` | 登记监测站（上升阈值必须严格大于解除阈值，否则 400） |
| GET | `/stations` / `/stations/{id}` | 查询监测站 |
| POST | `/stations/{id}/devices` | 登记设备（设备号全局唯一，重复 409） |
| GET | `/stations/{id}/devices` | 查询站内设备 |
| POST | `/stations/{id}/threshold-versions` | 登记阈值版本（同站重复生效时间 409，非法阈值/无时区 400，未知站 404） |
| POST | `/stations/{id}/threshold-versions/preview` | **预演**阈值调整对待发布预警的影响（不落库；校验/冲突规则同上，返回本站及所属责任区现状/预演） |
| GET | `/stations/{id}/threshold-versions` | 查询阈值版本历史（含登记基准 baseline） |

### 读数补传（幂等）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/readings` | 补传读数：`{device_no, seq, observed_at, water_level}` |
| GET | `/stations/{id}/readings?include_voided=false` | 查询站内读数 |
| GET | `/readings/{id}` | 查询单条读数 |

- `observed_at` 必须带时区（如 `2026-10-01T08:00:00+08:00` 或 `...Z`），否则 400；
- 水位须为有限数值且在合法范围内，否则 400；未知设备号 404；
- **同设备号 + 同序号 + 同内容**重试 → 200 返回原结果（`deduplicated: true`）；
- **同设备号 + 同序号 + 异内容** → 409 冲突，不写入。

### 审核（作废 / 更正 / 历史）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/readings/{id}/void` | 作废读数，必填 `reason`，可带 `auditor` |
| POST | `/readings/{id}/correct` | 更正观测时间和/或水位，必填 `reason` |
| GET | `/readings/{id}/revisions` | 查询原值与历次修订 |

作废/更正后响应中直接附带重算后的待发布裁定状态。

### 裁定与发布

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/stations/{id}/timeline` | 待发布裁定时间线（实时重算） |
| POST | `/zones` | 登记防汛责任区（未知站 404，重复成员/无时区/K 越界 400） |
| GET | `/zones` / `/zones/{id}` | 查询责任区列表 / 配置 |
| GET | `/zones/{id}/timeline` | 待发布跨站联动结果（实时重算） |
| GET | `/public/zones/{id}/timeline` | 责任区最近一次发布冻结结果（无发布 404） |
| POST | `/publish` | 发布：冻结全部监测站与责任区当前时间线 |
| GET | `/publications` / `/publications/{id}` | 查询历史发布版本（含 `zone_timelines`） |
| GET | `/public/current` | 最新发布的公开结果（含 `zone_timelines`） |
| GET | `/public/stations/{id}/timeline` | 某站最新公开时间线 |

## 调用示例

完整端到端示例脚本（登记 → 补传 → 幂等重试 → 冲突 → 预警 → 发布 → 解除 → 更正 → 重发布 → 作废 → 阈值版本 → **责任区登记/同刻合并/初始状态/发布冻结/公开无结果** → **阈值调整预演/不落库/同刻冲突/登记一致性**）：

```bash
./examples/demo.sh            # 默认打 http://127.0.0.1:8000
BASE=http://127.0.0.1:8000 ./examples/demo.sh
```

curl 速览：

```bash
# 登记监测站与设备
curl -X POST http://127.0.0.1:8000/stations \
  -H 'content-type: application/json' \
  -d '{"name":"青石峡站","rise_threshold":10.0,"clear_threshold":8.0}'
curl -X POST http://127.0.0.1:8000/stations/1/devices \
  -H 'content-type: application/json' -d '{"device_no":"DEV-001"}'

# 设备补传读数（带时区观测时间）
curl -X POST http://127.0.0.1:8000/readings \
  -H 'content-type: application/json' \
  -d '{"device_no":"DEV-001","seq":1,"observed_at":"2026-10-01T08:00:00+08:00","water_level":12.0}'

# 查看待发布裁定时间线
curl http://127.0.0.1:8000/stations/1/timeline

# 发布并查看公开结果
curl -X POST http://127.0.0.1:8000/publish
curl http://127.0.0.1:8000/public/current

# 审核更正（带原因）并查看修订历史
curl -X POST http://127.0.0.1:8000/readings/1/correct \
  -H 'content-type: application/json' \
  -d '{"water_level":9.0,"reason":"比测后修正","auditor":"王审"}'
curl http://127.0.0.1:8000/readings/1/revisions

# 防汛方案调整：登记阈值版本（带时区生效时间 + 原因），查询版本历史
curl -X POST http://127.0.0.1:8000/stations/1/threshold-versions \
  -H 'content-type: application/json' \
  -d '{"effective_at":"2026-10-02T08:00:00+08:00","rise_threshold":9.0,"clear_threshold":7.0,"reason":"入汛提级"}'
curl http://127.0.0.1:8000/stations/1/threshold-versions

# 登记前预演：同一入参，不落库返回本站及所属责任区的现状(current)/预演(preview)
curl -X POST http://127.0.0.1:8000/stations/1/threshold-versions/preview \
  -H 'content-type: application/json' \
  -d '{"effective_at":"2026-10-02T08:00:00+08:00","rise_threshold":9.0,"clear_threshold":7.0,"reason":"入汛提级预演"}'

# 登记防汛责任区（非空名称、带时区生效时间、不重复成员站、触发站数 K=2）
curl -X POST http://127.0.0.1:8000/zones \
  -H 'content-type: application/json' \
  -d '{"name":"青石峡片区","effective_at":"2026-10-02T08:00:00+08:00","station_ids":[1,2],"trigger_count":2}'

# 查看待发布跨站联动结果（状态 / 事件时间线 / 事件发生时预警的站 ID）
curl http://127.0.0.1:8000/zones/1/timeline

# 发布后查询冻结的公开结果（责任区尚无发布时返回 404）
curl -X POST http://127.0.0.1:8000/publish
curl http://127.0.0.1:8000/public/zones/1/timeline
```

## 并发与一致性设计

- 所有写操作（补传、作废、更正、阈值版本登记、责任区登记、发布）在 **`BEGIN IMMEDIATE` 事务**中执行，并由进程内锁串行化；SQLite 开启 **WAL + busy_timeout**，并发请求不会丢失已提交记录；
- `readings(device_id, seq)` **唯一约束**是幂等的最终防线：并发下同序号写只成功一次，其余按内容比对返回原结果或 409；
- `threshold_versions(station_id, effective_at)` **唯一约束**保证同站生效时刻不重复（生效时间统一归一化为 UTC 存储，不同时区表示的同一时刻同样冲突）；
- 责任区配置（`zones` / `zone_members`）只登记、不修改，区域联动结果在读取与发布时按成员站当前待发布时间线实时重算；发布在单事务内为全部监测站与责任区生成快照，公开结果读取的是冻结快照，与后续写入（含补传、审核修订、阈值调整）互不影响。
- **阈值预演为纯只读操作**：在一次 `db.read()`（同一连接 + 进程内锁）内完成现状与预演的全部单站/责任区裁定，候选版本仅以内存中的虚拟行（临时 id `-1`）参与计算，不开启写事务、不产生任何数据库写入；因此预演期间拿到的现状/预演取自同一数据视图，且绝不会影响待发布查询与已冻结发布。

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

覆盖：阈值/时间/水位/设备校验、幂等与冲突、预警与解除规则（含断段、乱序重排、跨段解除）、作废与更正重算、修订历史、**阈值版本（基准回退、同刻采用新版本、跨版本分别判定、调整不产生事件、快照冻结）**、**阈值版本预演（同一校验/同刻 409 冲突、不落库不改待发布与冻结结果、本站与所属责任区现状/预演、其他成员站按待发布联动、同刻合并、与正式登记状态/事件类型/时刻一致）**、**防汛责任区（登记校验、初始状态计入、同刻合并、K 达到/跌破、事件站 ID、补传/修订/阈值调整重算、发布冻结与公开无结果、replay_zone 纯函数边界）**、发布冻结、并发补传/发布不丢记录。

## 项目结构

```
app/
  adjudication.py   # 裁定核心（纯函数：单站分段预警/解除 + 责任区跨站联动重放 replay_zone）
  config.py         # 环境变量配置
  db.py             # SQLite 连接、事务与建表
  schemas.py        # 请求模型与校验
  main.py           # FastAPI 路由与应用工厂
tests/
  conftest.py       # 导入期重定向默认数据库，避免污染工作区
  test_api.py       # 端到端测试（86 例）
examples/demo.sh    # 调用示例脚本
Dockerfile / docker-compose.yml
```

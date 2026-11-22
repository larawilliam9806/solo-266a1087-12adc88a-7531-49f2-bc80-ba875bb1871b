# 公交 GTFS 时刻表发布系统

FastAPI + SQLite + 本地文件的公交时刻表发布与查询网页。数据工程师一次性上传同一版本的
5 个 CSV（stops / routes / trips / stop_times / calendar），系统整版校验通过后才发布；
公众按「线路 + 运营日」查询有序停站时刻。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `gtfs-data` 中，容器重启数据不丢。

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

### 运行测试

```bash
pip install pytest httpx
pytest tests/ -q
```

## 网页功能

- **时刻查询**：选择运营日和线路，展示当日各班次的停站序列与到发时刻；
  跨午夜时刻显示为 `24:20:00` 并标注「+1天」。
- **车库**：按车号和**北京时间自然日**查询当前正式版的计划占用，列出落在
  当天的占用时段、对应班次与原运营日，并在页面标出来源正式版号。跨午夜的
  运行会拆分到实际落到的自然日（例如 23:30→次日 00:50 的夜班，前一天显示
  23:30–24:00、当天显示 00:00–00:50）。
- **发布新版本**：一次上传 5 个 CSV + 版本名称。通过则立即生效；
  失败则列出全部错误行（文件 / 行号 / 字段 / 原因），当前正式版不受影响。
- **版本记录**：查看所有版本（含失败版本）及其错误明细。

也可以用 `sample_data/valid/`（可通过）、`sample_data/invalid/`（含典型错误）
和 `sample_data/vehicle_conflict/`（同一车号计划占用冲突）三组样例数据在网页上直接试。

## 校验规则

任一规则不满足，整版拒绝发布：

1. **结构**：必备列齐全；字段类型合法（非负整数序号、YYYYMMDD 日期、0/1 星期标志、
   HH:MM:SS 时刻）。
2. **表内唯一**：`stop_id` / `route_id` / `service_id` / `trip_id` 各自唯一；
   `stop_times` 内 `(trip_id, stop_sequence)` 唯一。
3. **跨表引用**：`trips.route_id → routes`、`trips.service_id → calendar`、
   `stop_times.trip_id → trips`、`stop_times.stop_id → stops`；
   每个 trip 至少有一条 stop_times。
4. **时刻递增**：同一 trip 按 `stop_sequence` 排序后，每行 `departure ≥ arrival`，
   且下一站到站时刻严格大于上一站发车时刻。
5. **跨午夜**：时刻允许 `HH ≥ 24`（如 `25:10:00`），内部统一换算为
  「自运营日 00:00 起的秒数」比较与存储；`24:10` 之后出现 `23:59` 视为倒退报错。
6. **车辆计划占用**：`trips.csv` 可选增加 `vehicle_id` 列填写固定车号；
   **缺列或留空仍按原规则处理**，不参与占用检查。同一车号的占用按
   「首站发车 → 末站到站」计算（注意：首站到站时刻不计），**允许首尾相接**
   （前一班到站 == 后一班发车不算冲突），**单站班次不计**。
   - 按**北京时间**比较绝对时刻，因此能发现跨午夜的重叠；同一班次在
     不同运营日的两次运行也会互相比较（如 26 小时班次日日运行会自撞）。
   - 日历不逐日展开：每个运营日只看 `start_date/end_date` 与星期标志，
     星期图案 7 天重复，用常数次比较定位最早的重叠日，多年日历同样快。
   - 发现冲突即**拒绝整版**，并指出**一处**冲突：车号、两个班次号、
     各自运营日与实际重叠时段（北京时间）。

## 发布与并发语义

- **整版原子发布**：所有校验在内存中完成；通过后在**单个 SQLite 事务**内写入
  整版数据并拨动 `meta.current_version_id` 指针。查询方要么看到旧完整版本、
  要么看到新完整版本，绝不出现半新半旧。
- **失败保旧**：校验失败的版本只记录 `versions(status='failed')` 和错误明细，
  不写入任何时刻数据、不触碰指针。
- **版本不可变**：已发布版本的数据永不修改或删除；请求开始时读一次指针，
  之后该请求内所有查询都固定读取同一版本，换版期间查询结果始终完整一致。
- SQLite 开启 **WAL**，发布写入不阻塞并发查询。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/versions` | multipart 上传 `label` + 5 个 CSV；成功 201，失败 422 并返回错误行 |
| GET | `/api/versions` | 版本列表（含当前正式版指针） |
| GET | `/api/versions/current` | 当前正式版 |
| GET | `/api/versions/{id}` | 版本详情与错误明细 |
| GET | `/api/routes` | 当前版本的线路列表 |
| GET | `/api/routes/{route_id}/schedule?date=YYYY-MM-DD` | 该运营日的班次与有序停站时刻 |
| GET | `/api/vehicles` | 当前正式版出现过的车号 |
| GET | `/api/vehicles/{vehicle_id}/occupancy?date=YYYY-MM-DD` | 该车号在北京时间自然日的计划占用 |

### 命令行示例

```bash
# 发布
curl -F "label=2026冬季运行图" \
     -F "stops=@sample_data/valid/stops.csv" \
     -F "routes=@sample_data/valid/routes.csv" \
     -F "trips=@sample_data/valid/trips.csv" \
     -F "stop_times=@sample_data/valid/stop_times.csv" \
     -F "calendar=@sample_data/valid/calendar.csv" \
     http://localhost:8000/api/versions

# 查询夜班线路（含 24:00 之后的跨午夜班次）
curl "http://localhost:8000/api/routes/R2/schedule?date=2026-11-23"

# 查某车在北京时间 2026-11-23 当天的计划占用（含跨午夜拆分与原运营日）
curl "http://localhost:8000/api/vehicles/BUS-01/occupancy?date=2026-11-23"
```

## 目录结构

```
app/
  main.py        FastAPI 端点（发布 / 查询 / 车库 / 版本）
  gtfs.py        CSV 解析与整版校验（纯标准库，含车辆占用冲突）
  db.py          SQLite schema、原子发布、版本指针
  static/index.html  网页（时刻查询 / 车库 / 发布 / 版本记录）
tests/           pytest 单元与集成测试
sample_data/     可通过、含错误、含车辆冲突的样例 CSV
Dockerfile / docker-compose.yml
```

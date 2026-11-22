"""GTFS CSV 解析与整版校验。

纯标准库实现，不依赖 FastAPI，便于单元测试。

校验规则：
1. 结构：必备列齐全、字段类型合法（整数/日期/时刻/0-1 标志）。
2. 表内唯一：stops.stop_id、routes.route_id、calendar.service_id、
   trips.trip_id 唯一；stop_times 内 (trip_id, stop_sequence) 唯一。
3. 跨表引用：trips.route_id -> routes、trips.service_id -> calendar、
   stop_times.trip_id -> trips、stop_times.stop_id -> stops。
4. 时刻递增：同一 trip 按 stop_sequence 排序后，
   每行 departure >= arrival，且下一站到站时刻 > 上一站发车时刻（严格递增）。
5. 跨午夜：时刻允许 HH >= 24（如 25:10:00），统一换算为
   “自运营日 00:00 起的秒数”存储与比较。
6. 车辆占用：trips.csv 可选 vehicle_id 列。同一车号的计划占用按
   “首站发车 -> 末站到站”计算，允许首尾相接，单站班次不计；
   按北京时间换算绝对时刻后检查重叠（含跨午夜，以及同一班次在不同
   运营日的两次运行）。日历只按“起止日期 + 星期标志”做常数时间判定，
   不逐日展开多年日历。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

MAX_ERRORS = 1000  # 单版本最多记录的错误条数，防止超大错误响应

DAY_SECS = 86400

TIME_RE = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")
DATE_RE = re.compile(r"^\d{8}$")

FILES = ("stops", "routes", "trips", "stop_times", "calendar")

REQUIRED_COLUMNS = {
    "stops": ["stop_id", "stop_name"],
    "routes": ["route_id"],
    "trips": ["route_id", "service_id", "trip_id"],
    "stop_times": ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
    "calendar": [
        "service_id", "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday", "start_date", "end_date",
    ],
}

WEEKDAY_COLUMNS = ["monday", "tuesday", "wednesday", "thursday",
                   "friday", "saturday", "sunday"]


@dataclass
class VError:
    file: str
    row: int | None      # CSV 文件行号（表头为第 1 行）
    field: str | None
    message: str

    def as_dict(self) -> dict:
        return {"file": self.file, "row": self.row,
                "field": self.field, "message": self.message}


def parse_gtfs_time(value: str) -> int | None:
    """解析 GTFS 时刻（允许 HH>=24）为自运营日 00:00 起的秒数。"""
    m = TIME_RE.match(value.strip())
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return h * 3600 + mi * 60 + s


def format_gtfs_time(secs: int) -> str:
    """秒数格式化为 GTFS 时刻字符串，小时可 >= 24。"""
    h, rem = divmod(secs, 3600)
    mi, s = divmod(rem, 60)
    return f"{h:02d}:{mi:02d}:{s:02d}"


def parse_service_date(value: str) -> date | None:
    """解析 YYYYMMDD 服务日期。"""
    value = value.strip()
    if not DATE_RE.match(value):
        return None
    try:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None


def abs_secs(day: date, secs_of_day: int) -> int:
    """运营日 + 自运营日 00:00 起的秒数 -> 绝对秒数（以北京时间自然日计）。"""
    return day.toordinal() * DAY_SECS + secs_of_day


def fmt_beijing(secs: int) -> str:
    """绝对秒数（abs_secs 口径）-> 北京时间 'YYYY-MM-DD HH:MM:SS'。"""
    day_no, rem = divmod(secs, DAY_SECS)
    return (datetime.combine(date.fromordinal(day_no), datetime.min.time())
            + timedelta(seconds=rem)).strftime("%Y-%m-%d %H:%M:%S")


class _ErrorBag:
    def __init__(self) -> None:
        self.errors: list[VError] = []
        self.truncated = False

    def add(self, file: str, row: int | None, field: str | None, message: str) -> None:
        if len(self.errors) >= MAX_ERRORS:
            self.truncated = True
            return
        self.errors.append(VError(file, row, field, message))

    def __bool__(self) -> bool:
        return bool(self.errors)


def _read_csv(name: str, content: bytes, bag: _ErrorBag) -> list[dict] | None:
    """读取 CSV 为 dict 行列表，每行附带 _row（文件行号，表头=1）。"""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        bag.add(name, None, None, "文件不是有效的 UTF-8 编码")
        return None
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        bag.add(name, None, None, "文件为空或缺少表头")
        return None
    headers = [h.strip() if h else "" for h in reader.fieldnames]
    missing = [c for c in REQUIRED_COLUMNS[name] if c not in headers]
    if missing:
        bag.add(name, 1, None, f"缺少必备列: {', '.join(missing)}")
        return None
    rows: list[dict] = []
    for lineno, raw in enumerate(reader, start=2):
        if None in raw:  # 列数多于表头
            bag.add(name, lineno, None, "行列数多于表头列数")
            continue
        row = {k.strip(): (v.strip() if v is not None else "")
               for k, v in raw.items() if k is not None}
        row["_row"] = lineno
        rows.append(row)
    if not rows:
        bag.add(name, None, None, "文件没有数据行")
        return None
    return rows


def _check_unique(rows: list[dict], name: str, key: str, bag: _ErrorBag) -> set[str]:
    seen: dict[str, int] = {}
    ids: set[str] = set()
    for r in rows:
        v = r[key]
        if not v:
            bag.add(name, r["_row"], key, f"{key} 不能为空")
            continue
        if v in seen:
            bag.add(name, r["_row"], key,
                    f"{key} '{v}' 重复（首次出现于第 {seen[v]} 行）")
            continue
        seen[v] = r["_row"]
        ids.add(v)
    return ids


def validate_version(files: dict[str, bytes]) -> tuple[dict[str, list[tuple]] | None,
                                                     list[dict], bool]:
    """校验一个完整版本的 5 个 CSV。

    返回 (tables, errors, truncated)：
    - tables: 校验通过时，各表待入库的行（tuple 列表）；失败时为 None。
    - errors: VError.as_dict() 列表。
    - truncated: 错误是否因达到上限被截断。
    """
    bag = _ErrorBag()
    raw: dict[str, list[dict] | None] = {}
    for name in FILES:
        content = files.get(name)
        if content is None:
            bag.add(name, None, None, "缺少上传文件")
            raw[name] = None
        else:
            raw[name] = _read_csv(name, content, bag)

    # ---------- stops ----------
    stop_ids: set[str] = set()
    stops_out: list[tuple] = []
    if raw["stops"]:
        stop_ids = _check_unique(raw["stops"], "stops", "stop_id", bag)
        for r in raw["stops"]:
            lat = lon = None
            if r.get("stop_lat"):
                try:
                    lat = float(r["stop_lat"])
                except ValueError:
                    bag.add("stops", r["_row"], "stop_lat", "stop_lat 不是数字")
            if r.get("stop_lon"):
                try:
                    lon = float(r["stop_lon"])
                except ValueError:
                    bag.add("stops", r["_row"], "stop_lon", "stop_lon 不是数字")
            if r["stop_id"] in stop_ids:
                stops_out.append((r["stop_id"], r.get("stop_name", ""), lat, lon))

    # ---------- routes ----------
    route_ids: set[str] = set()
    routes_out: list[tuple] = []
    if raw["routes"]:
        route_ids = _check_unique(raw["routes"], "routes", "route_id", bag)
        for r in raw["routes"]:
            rtype = r.get("route_type", "")
            rtype_val = None
            if rtype:
                try:
                    rtype_val = int(rtype)
                except ValueError:
                    bag.add("routes", r["_row"], "route_type", "route_type 不是整数")
            if r["route_id"] in route_ids:
                routes_out.append((r["route_id"], r.get("route_short_name", ""),
                                   r.get("route_long_name", ""), rtype_val))

    # ---------- calendar ----------
    service_ids: set[str] = set()
    calendar_out: list[tuple] = []
    if raw["calendar"]:
        service_ids = _check_unique(raw["calendar"], "calendar", "service_id", bag)
        for r in raw["calendar"]:
            flags: list[int] = []
            ok = True
            for col in WEEKDAY_COLUMNS:
                v = r[col]
                if v not in ("0", "1"):
                    bag.add("calendar", r["_row"], col, f"{col} 必须为 0 或 1")
                    ok = False
                else:
                    flags.append(int(v))
            start = parse_service_date(r["start_date"])
            end = parse_service_date(r["end_date"])
            if start is None:
                bag.add("calendar", r["_row"], "start_date",
                        f"start_date '{r['start_date']}' 不是合法的 YYYYMMDD 日期")
                ok = False
            if end is None:
                bag.add("calendar", r["_row"], "end_date",
                        f"end_date '{r['end_date']}' 不是合法的 YYYYMMDD 日期")
                ok = False
            if start and end and start > end:
                bag.add("calendar", r["_row"], "start_date", "start_date 晚于 end_date")
                ok = False
            if ok and r["service_id"] in service_ids:
                calendar_out.append((r["service_id"], *flags,
                                     start.isoformat(), end.isoformat()))

    # ---------- trips ----------
    trip_ids: set[str] = set()
    trips_out: list[tuple] = []
    trip_route: dict[str, str] = {}
    trip_service: dict[str, str] = {}
    trip_vehicle: dict[str, str] = {}   # 仅记录显式填写车号的 trip
    if raw["trips"]:
        trip_ids = _check_unique(raw["trips"], "trips", "trip_id", bag)
        for r in raw["trips"]:
            ok = True
            if r["route_id"] and r["route_id"] not in route_ids:
                bag.add("trips", r["_row"], "route_id",
                        f"route_id '{r['route_id']}' 在 routes 中不存在")
                ok = False
            if not r["route_id"]:
                bag.add("trips", r["_row"], "route_id", "route_id 不能为空")
                ok = False
            if r["service_id"] and r["service_id"] not in service_ids:
                bag.add("trips", r["_row"], "service_id",
                        f"service_id '{r['service_id']}' 在 calendar 中不存在")
                ok = False
            if not r["service_id"]:
                bag.add("trips", r["_row"], "service_id", "service_id 不能为空")
                ok = False
            direction = r.get("direction_id", "")
            direction_val = None
            if direction:
                if direction in ("0", "1"):
                    direction_val = int(direction)
                else:
                    bag.add("trips", r["_row"], "direction_id",
                            "direction_id 必须为 0 或 1")
                    ok = False
            vehicle_id = r.get("vehicle_id", "")
            if ok and r["trip_id"] in trip_ids:
                trips_out.append((r["route_id"], r["service_id"], r["trip_id"],
                                  r.get("trip_headsign", ""), direction_val,
                                  vehicle_id))
                trip_route[r["trip_id"]] = r["route_id"]
                trip_service[r["trip_id"]] = r["service_id"]
                if vehicle_id:
                    trip_vehicle[r["trip_id"]] = vehicle_id

    # ---------- stop_times ----------
    stop_times_out: list[tuple] = []
    if raw["stop_times"]:
        # 先做单行类型校验与引用校验
        per_trip: dict[str, list[dict]] = {}
        for r in raw["stop_times"]:
            ok = True
            if r["trip_id"] and r["trip_id"] not in trip_ids:
                bag.add("stop_times", r["_row"], "trip_id",
                        f"trip_id '{r['trip_id']}' 在 trips 中不存在")
                ok = False
            if not r["trip_id"]:
                bag.add("stop_times", r["_row"], "trip_id", "trip_id 不能为空")
                ok = False
            if r["stop_id"] and r["stop_id"] not in stop_ids:
                bag.add("stop_times", r["_row"], "stop_id",
                        f"stop_id '{r['stop_id']}' 在 stops 中不存在")
                ok = False
            if not r["stop_id"]:
                bag.add("stop_times", r["_row"], "stop_id", "stop_id 不能为空")
                ok = False
            try:
                seq = int(r["stop_sequence"])
                if seq < 0:
                    raise ValueError
            except ValueError:
                bag.add("stop_times", r["_row"], "stop_sequence",
                        f"stop_sequence '{r['stop_sequence']}' 不是非负整数")
                ok = False
                seq = -1
            arr = parse_gtfs_time(r["arrival_time"])
            if arr is None:
                bag.add("stop_times", r["_row"], "arrival_time",
                        f"arrival_time '{r['arrival_time']}' 不是合法的 HH:MM:SS 时刻")
                ok = False
            dep = parse_gtfs_time(r["departure_time"])
            if dep is None:
                bag.add("stop_times", r["_row"], "departure_time",
                        f"departure_time '{r['departure_time']}' 不是合法的 HH:MM:SS 时刻")
                ok = False
            if ok and dep < arr:
                bag.add("stop_times", r["_row"], "departure_time",
                        f"发车时刻 {r['departure_time']} 早于到站时刻 {r['arrival_time']}")
                ok = False
            if ok:
                rec = {"_row": r["_row"], "trip_id": r["trip_id"], "seq": seq,
                       "stop_id": r["stop_id"], "arr": arr, "dep": dep}
                per_trip.setdefault(r["trip_id"], []).append(rec)

        # 每个 trip：stop_sequence 唯一 + 时刻严格递增
        for trip_id, recs in per_trip.items():
            by_seq: dict[int, dict] = {}
            unique_recs: list[dict] = []
            for rec in sorted(recs, key=lambda x: (x["seq"], x["_row"])):
                prev = by_seq.get(rec["seq"])
                if prev is not None:
                    bag.add("stop_times", rec["_row"], "stop_sequence",
                            f"trip '{trip_id}' 的 stop_sequence {rec['seq']} "
                            f"重复（首次出现于第 {prev['_row']} 行）")
                    continue
                by_seq[rec["seq"]] = rec
                unique_recs.append(rec)
            for prev, cur in zip(unique_recs, unique_recs[1:]):
                if cur["arr"] <= prev["dep"]:
                    bag.add(
                        "stop_times", cur["_row"], "arrival_time",
                        f"trip '{trip_id}' 时刻未递增：第 {cur['seq']} 站到站 "
                        f"{format_gtfs_time(cur['arr'])} 不晚于第 {prev['seq']} 站发车 "
                        f"{format_gtfs_time(prev['dep'])}")
                stop_times_out.append((trip_id, cur["seq"], cur["stop_id"],
                                       cur["arr"], cur["dep"]))
            if unique_recs:
                first = unique_recs[0]
                stop_times_out.append((trip_id, first["seq"], first["stop_id"],
                                       first["arr"], first["dep"]))

        # 每个有效 trip 必须至少有一条 stop_times
        if raw["trips"]:
            for r in raw["trips"]:
                tid = r["trip_id"]
                if tid in trip_ids and tid not in per_trip:
                    bag.add("trips", r["_row"], "trip_id",
                            f"trip '{tid}' 没有任何 stop_times 记录")

    # ---------- 车辆计划占用冲突 ----------
    # 只有 trips.csv 提供了 vehicle_id 列且填了车号的班次才参与。
    if not bag and trip_vehicle and raw["stop_times"] and raw["calendar"]:
        _check_vehicle_conflicts(calendar_out, trip_vehicle, trip_service,
                                 stop_times_out, bag)

    if bag:
        return None, [e.as_dict() for e in bag.errors], bag.truncated

    tables = {
        "stops": stops_out,
        "routes": routes_out,
        "calendar": calendar_out,
        "trips": trips_out,
        "stop_times": stop_times_out,
    }
    return tables, [], False


def _check_vehicle_conflicts(
    calendar_out: list[tuple],
    trip_vehicle: dict[str, str],
    trip_service: dict[str, str],
    stop_times_out: list[tuple],
    bag: _ErrorBag,
) -> None:
    """检查同一车号的计划占用是否重叠。

    占用区间 = 首站发车时刻 -> 末站到站时刻（GTFS 秒数，可跨午夜）。
    单站班次不计。比较前把「运营日 + 日内秒数」换算成绝对秒数，
    从而正确覆盖跨午夜、以及同一班次在不同运营日的两次运行。

    日历处理：直接对日历判定，不逐日展开全期。同一车号的两个班次
    若运行窗口相交于某一运营日，则各自取「最早的重叠有效运营日」——
    星期图案每 7 天重复一次，因此只需在前 7 天里定位。
    """
    # trip_id -> (首站发车秒, 末站到站秒)
    window: dict[str, tuple[int, int]] = {}
    grouped: dict[str, list[tuple[int, int, int]]] = {}
    for trip_id, seq, _stop, arr, dep in stop_times_out:
        grouped.setdefault(trip_id, []).append((seq, arr, dep))
    for trip_id, recs in grouped.items():
        if len(recs) < 2:          # 单站班次：不计计划占用
            continue
        recs.sort()
        window[trip_id] = (recs[0][2], recs[-1][1])

    cal: dict[str, tuple[date, date, tuple[bool, ...]]] = {}
    for svc, *rest in calendar_out:
        cal[svc] = (date.fromisoformat(rest[7]), date.fromisoformat(rest[8]),
                    tuple(bool(x) for x in rest[:7]))

    by_vehicle: dict[str, list[tuple[str, int, int, str]]] = {}
    for trip_id, vehicle_id in trip_vehicle.items():
        win = window.get(trip_id)
        svc = trip_service.get(trip_id)
        if win is None or svc not in cal:
            continue
        by_vehicle.setdefault(vehicle_id, []).append(
            (trip_id, win[0], win[1], svc))

    for vehicle_id, runs in sorted(by_vehicle.items()):
        runs.sort(key=lambda r: (r[0], r[3]))
        for i in range(len(runs)):
            trip_a, start_a, end_a, svc_a = runs[i]
            # j 从 i 开始：同一班次在不同运营日的两次运行也要比较。
            for j in range(i, len(runs)):
                trip_b, start_b, end_b, svc_b = runs[j]
                same_run = (trip_a == trip_b and svc_a == svc_b)
                hit = _first_overlap(cal[svc_a], start_a, end_a,
                                     cal[svc_b], start_b, end_b,
                                     allow_same_day=not same_run)
                if hit is not None:
                    d_a, d_b, lo, hi = hit
                    bag.add("trips", None, "vehicle_id", (
                        f"车辆 '{vehicle_id}' 计划占用冲突：班次 '{trip_a}'"
                        f"（运营日 {d_a.isoformat()}）与班次 '{trip_b}'"
                        f"（运营日 {d_b.isoformat()}）重叠，实际重叠时段 "
                        f"{fmt_beijing(lo)} 至 {fmt_beijing(hi)}（北京时间）"))
                    return  # 指出一处冲突即可，整版拒绝


def _first_overlap(cal_a, start_a, end_a, cal_b, start_b, end_b,
                   allow_same_day: bool = True):
    """两个班次各自按日历反复运行时，是否存在一次占用重叠。

    cal_x = (start_date, end_date, weekday_flags)；
    返回 (a 的运营日, b 的运营日, 重叠开始绝对秒, 重叠结束绝对秒) 或 None。
    首尾相接（恰好等于）不算冲突。全部为常数次比较，与日历跨度无关。
    """
    a_start, a_end, flags_a = cal_a
    b_start, b_end, flags_b = cal_b

    def intersect_on(d_a: date, d_b: date):
        lo = max(abs_secs(d_a, start_a), abs_secs(d_b, start_b))
        hi = min(abs_secs(d_a, end_a), abs_secs(d_b, end_b))
        return (lo, hi) if lo < hi else None

    # delta = d_b - d_a。窗口跨度决定需要考察哪些天差；普通班次
    # 短于 24 小时，只可能是 -1/0/+1。同一班次在不同运营日的两次运行
    # 要比较，但同一天的同一次运行不能和自己比（delta=0 跳过）。
    span_days = max((end_a - start_a), (end_b - start_b)) // DAY_SECS
    for delta in range(-span_days - 1, span_days + 2):
        if delta == 0 and not allow_same_day:
            continue
        # 求使 d_a、d_b = d_a + delta 均有效的最早 d_a。
        lo = max(a_start, b_start - timedelta(days=delta))
        hi = min(a_end, b_end - timedelta(days=delta))
        if lo > hi:
            continue
        # 星期图案 7 天重复：在 [lo, hi] 前 7 天里找最早的有效 d_a。
        d_a = _first_valid_day(flags_a, flags_b, delta, lo, hi)
        if d_a is None:
            continue
        d_b = d_a + timedelta(days=delta)
        found = intersect_on(d_a, d_b)
        if found:
            return d_a, d_b, found[0], found[1]
    return None


def _first_valid_day(flags_a, flags_b, delta: int, lo: date, hi: date):
    """在 [lo, hi] 内找最早的 d_a，使 a(d_a) 与 b(d_a+delta) 都为有效服务日。"""
    for k in range(7):
        d_a = lo + timedelta(days=k)
        if d_a > hi:
            return None
        d_b = d_a + timedelta(days=delta)
        if flags_a[d_a.weekday()] and flags_b[d_b.weekday()]:
            return d_a
    # 7 天内必有候选；若窗口 < 7 天且没命中，则确实没有有效日。
    return None

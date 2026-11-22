"""校验逻辑单元测试（纯标准库路径）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.gtfs import format_gtfs_time, parse_gtfs_time, validate_version
from conftest import valid_files


def err_fields(errors):
    return {(e["file"], e["field"]) for e in errors}


# ---------- 时刻解析 ----------

def test_parse_time_normal():
    assert parse_gtfs_time("08:05:09") == 8 * 3600 + 5 * 60 + 9
    assert parse_gtfs_time("00:00:00") == 0


def test_parse_time_after_midnight():
    assert parse_gtfs_time("24:00:00") == 86400
    assert parse_gtfs_time("25:10:00") == 86400 + 3600 + 600


def test_parse_time_invalid():
    assert parse_gtfs_time("8:5:0") is None
    assert parse_gtfs_time("12:60:00") is None
    assert parse_gtfs_time("abc") is None
    assert parse_gtfs_time("") is None


def test_format_time_roundtrip():
    assert format_gtfs_time(0) == "00:00:00"
    assert format_gtfs_time(90061) == "25:01:01"


# ---------- 整版校验 ----------

def test_valid_version_passes():
    tables, errors, truncated = validate_version(valid_files())
    assert errors == []
    assert not truncated
    assert set(tables) == {"stops", "routes", "trips", "stop_times", "calendar"}
    assert len(tables["stop_times"]) == 11


def test_missing_column():
    tables, errors, _ = validate_version(valid_files(
        stops="stop_id,stop_lat\nS1,31.2\n"))
    assert tables is None
    assert any(e["file"] == "stops" and "缺少必备列" in e["message"] for e in errors)


def test_cross_reference_trip_route():
    tables, errors, _ = validate_version(valid_files(
        trips="route_id,service_id,trip_id\nR9,WKD,T1\n"))
    assert tables is None
    assert ("trips", "route_id") in err_fields(errors)


def test_cross_reference_trip_service():
    _, errors, _ = validate_version(valid_files(
        trips="route_id,service_id,trip_id\nR1,NOSVC,T1\n"))
    assert ("trips", "service_id") in err_fields(errors)


def test_cross_reference_stop_times_trip_and_stop():
    _, errors, _ = validate_version(valid_files(
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T9,08:00:00,08:00:00,SX,1\n"))
    fields = err_fields(errors)
    assert ("stop_times", "trip_id") in fields
    assert ("stop_times", "stop_id") in fields


def test_duplicate_stop_sequence():
    _, errors, _ = validate_version(valid_files(
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\n"
                   "T1,08:10:00,08:12:00,S2,1\n"))
    assert ("stop_times", "stop_sequence") in err_fields(errors)


def test_times_must_increase():
    _, errors, _ = validate_version(valid_files(
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\n"
                   "T1,07:50:00,07:55:00,S2,2\n"))
    assert ("stop_times", "arrival_time") in err_fields(errors)


def test_equal_times_not_increasing():
    # 相邻站时刻相同也算不递增
    _, errors, _ = validate_version(valid_files(
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\n"
                   "T1,08:00:00,08:05:00,S2,2\n"))
    assert ("stop_times", "arrival_time") in err_fields(errors)


def test_departure_before_arrival():
    _, errors, _ = validate_version(valid_files(
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:10:00,08:05:00,S1,1\n"))
    assert ("stop_times", "departure_time") in err_fields(errors)


def test_after_midnight_increasing_ok():
    # 23:55 -> 24:20 跨午夜递增合法
    tables, errors, _ = validate_version(valid_files(
        trips="route_id,service_id,trip_id\nR2,ALL,N1\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "N1,23:55:00,23:58:00,S1,1\n"
                   "N1,24:20:00,24:22:00,S2,2\n"
                   "N1,25:01:00,25:01:00,S3,3\n"))
    assert errors == []
    secs = [r[3] for r in sorted(tables["stop_times"], key=lambda r: r[1])]
    assert secs == sorted(secs)
    assert secs[-1] == 25 * 3600 + 60


def test_after_midnight_decreasing_rejected():
    # 24:10 之后接 23:59：数值上倒退，必须报错（防止把次日时刻写成当日）
    _, errors, _ = validate_version(valid_files(
        trips="route_id,service_id,trip_id\nR2,ALL,N1\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "N1,24:10:00,24:10:00,S1,1\n"
                   "N1,23:59:00,23:59:00,S2,2\n"))
    assert ("stop_times", "arrival_time") in err_fields(errors)


def test_duplicate_ids():
    _, errors, _ = validate_version(valid_files(
        stops="stop_id,stop_name\nS1,甲\nS1,乙\n"))
    assert ("stops", "stop_id") in err_fields(errors)


def test_calendar_bad_flag_and_date():
    _, errors, _ = validate_version(valid_files(
        calendar="service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                 "WKD,1,1,1,1,1,0,2,20261301,20261231\n"))
    fields = err_fields(errors)
    assert ("calendar", "sunday") in fields
    assert ("calendar", "start_date") in fields


def test_trip_without_stop_times():
    _, errors, _ = validate_version(valid_files(
        trips="route_id,service_id,trip_id\nR1,WKD,T1\nR1,WKD,T2\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\n"
                   "T1,08:10:00,08:10:00,S2,2\n"))
    assert ("trips", "trip_id") in err_fields(errors)


def test_error_rows_are_csv_line_numbers():
    _, errors, _ = validate_version(valid_files(
        trips="route_id,service_id,trip_id\n"
              "R1,WKD,T1\n"      # 第 2 行
              "R9,WKD,T2\n"))    # 第 3 行
    row3 = [e for e in errors if e["file"] == "trips" and e["row"] == 3]
    assert row3 and row3[0]["field"] == "route_id"


# ---------- 车辆计划占用 ----------

_STOPS = "stop_id,stop_name\nS1,甲\nS2,乙\nS3,丙\n"
_ROUTES = "route_id\nR1\n"
_CAL_ALL = ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
            "sunday,start_date,end_date\nALL,1,1,1,1,1,1,1,20260101,20261231\n")


def _vehicle_version(trips_csv, stop_times_csv, calendar_csv=_CAL_ALL):
    from app.gtfs import validate_version
    return validate_version({
        "stops": _STOPS.encode(), "routes": _ROUTES.encode(),
        "trips": trips_csv.encode(), "stop_times": stop_times_csv.encode(),
        "calendar": calendar_csv.encode()})


def _two_runs(t1_start, t1_end, t2_start="09:00:00", t2_end="10:00:00"):
    """T1 占用 t1_start->t1_end，T2 占用 t2_start->t2_end（按调度规则递增）。"""
    return (f"trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            f"T1,{t1_start},{t1_start},S1,1\nT1,{t1_end},{t1_end},S2,2\n"
            f"T2,{t2_start},{t2_start},S2,1\nT2,{t2_end},{t2_end},S3,2\n")


def test_vehicle_touching_runs_allowed():
    # 前车 09:00 到站、后车 09:00 发车：首尾相接允许
    tables, errors, _ = _vehicle_version(
        "route_id,service_id,trip_id,vehicle_id\nR1,ALL,T1,V1\nR1,ALL,T2,V1\n",
        _two_runs("08:00:00", "09:00:00", "09:00:00", "10:00:00"))
    assert errors == []


def test_vehicle_overlap_rejected_with_details():
    _, errors, _ = _vehicle_version(
        "route_id,service_id,trip_id,vehicle_id\nR1,ALL,T1,V1\nR1,ALL,T2,V1\n",
        _two_runs("08:00:00", "09:00:00", "08:59:00", "10:00:00"))
    assert len(errors) == 1
    e = errors[0]
    assert e["file"] == "trips" and e["field"] == "vehicle_id"
    # 车号、两个班次号、运营日、实际重叠时段
    assert "V1" in e["message"]
    assert "T1" in e["message"] and "T2" in e["message"]
    assert "2026-01-01" in e["message"]
    assert "2026-01-01 08:59:00" in e["message"]
    assert "2026-01-01 09:00:00" in e["message"]


def test_vehicle_only_first_conflict_reported():
    trips = ("route_id,service_id,trip_id,vehicle_id\n"
             "R1,ALL,T1,V1\nR1,ALL,T2,V1\nR1,ALL,T3,V1\n")
    st = ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
          "T1,08:00:00,08:00:00,S1,1\nT1,09:00:00,09:00:00,S2,2\n"
          "T2,08:30:00,08:30:00,S1,1\nT2,09:30:00,09:30:00,S2,2\n"
          "T3,08:45:00,08:45:00,S1,1\nT3,09:45:00,09:45:00,S2,2\n")
    _, errors, _ = _vehicle_version(trips, st)
    assert sum(1 for e in errors if e["field"] == "vehicle_id") == 1


def test_vehicle_missing_column_or_blank_is_ignored():
    st = _two_runs("08:00:00", "09:00:00", "08:59:00", "10:00:00")
    assert _vehicle_version(
        "route_id,service_id,trip_id\nR1,ALL,T1\nR1,ALL,T2\n", st)[1] == []
    assert _vehicle_version(
        "route_id,service_id,trip_id,vehicle_id\nR1,ALL,T1,\nR1,ALL,T2,\n",
        st)[1] == []


def test_vehicle_different_vehicles_no_conflict():
    _, errors, _ = _vehicle_version(
        "route_id,service_id,trip_id,vehicle_id\nR1,ALL,T1,V1\nR1,ALL,T2,V2\n",
        _two_runs("08:00:00", "09:00:00", "08:59:00", "10:00:00"))
    assert errors == []


def test_vehicle_single_stop_trip_not_counted():
    # 两个单站班次同一车号同一时刻：不计计划占用
    _, errors, _ = _vehicle_version(
        "route_id,service_id,trip_id,vehicle_id\nR1,ALL,T1,V1\nR1,ALL,T2,V1\n",
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,08:00:00,08:00:00,S1,1\nT2,08:00:00,08:00:00,S1,1\n")
    assert errors == []


def test_vehicle_overlap_across_midnight():
    # T1 运营日 d 23:00 -> 次日 01:00；T2 运营日 d+1 00:30 -> 02:30
    _, errors, _ = _vehicle_version(
        "route_id,service_id,trip_id,vehicle_id\nR1,ALL,T1,V1\nR1,ALL,T2,V1\n",
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,23:00:00,23:00:00,S1,1\nT1,25:00:00,25:00:00,S2,2\n"
        "T2,00:30:00,00:30:00,S1,1\nT2,01:30:00,01:30:00,S2,2\n")
    assert len(errors) == 1
    assert "2026-01-02 00:30:00" in errors[0]["message"]
    assert "2026-01-02 01:00:00" in errors[0]["message"]
    assert "2026-01-02" in errors[0]["message"] and "2026-01-01" in errors[0]["message"]


def test_vehicle_same_trip_two_service_days_compared():
    # 26 小时班次日日运行，会与自己的前一次运行重叠
    _, errors, _ = _vehicle_version(
        "route_id,service_id,trip_id,vehicle_id\nR1,ALL,T1,V1\n",
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,00:00:00,00:00:00,S1,1\nT1,26:00:00,26:00:00,S2,2\n")
    assert len(errors) == 1
    assert "T1" in errors[0]["message"]
    assert "2026-01-02" in errors[0]["message"] and "2026-01-01" in errors[0]["message"]


def test_vehicle_same_trip_two_service_days_ok_when_short():
    _, errors, _ = _vehicle_version(
        "route_id,service_id,trip_id,vehicle_id\nR1,ALL,T1,V1\n",
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,23:00:00,23:00:00,S1,1\nT1,25:00:00,25:00:00,S2,2\n")
    assert errors == []


def test_vehicle_calendars_never_active_same_day():
    # 工作日 vs 周末：同一车号、时刻重叠，但永不落在同一运营日
    trips = ("route_id,service_id,trip_id,vehicle_id\n"
             "R1,WKD,T1,V1\nR1,END,T2,V1\n")
    cal = ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
           "sunday,start_date,end_date\n"
           "WKD,1,1,1,1,1,0,0,20260101,20261231\n"
           "END,0,0,0,0,0,1,1,20260101,20261231\n")
    _, errors, _ = _vehicle_version(trips, _two_runs("08:00:00", "08:30:00"), cal)
    assert errors == []


def test_vehicle_calendars_disjoint_ranges():
    cal = ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
           "sunday,start_date,end_date\n"
           "ALL,1,1,1,1,1,1,1,20260101,20260201\n"
           "ALL,1,1,1,1,1,1,1,20260101,20260201\n")
    # 用两个服务：一个 1 月、一个 3 月
    cal = ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
           "sunday,start_date,end_date\n"
           "A,1,1,1,1,1,1,1,20260101,20260201\n"
           "B,1,1,1,1,1,1,1,20260301,20260401\n")
    _, errors, _ = _vehicle_version(
        "route_id,service_id,trip_id,vehicle_id\nR1,A,T1,V1\nR1,B,T2,V1\n",
        _two_runs("08:00:00", "09:00:00", "08:30:00", "09:30:00"), cal)
    assert errors == []


def test_vehicle_multiyear_calendar_is_constant_time():
    import time
    cal = ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
           "sunday,start_date,end_date\n"
           "A,1,1,1,1,1,1,1,20000101,20991231\n"
           "B,1,1,1,1,1,1,1,20000101,20991231\n")
    t0 = time.perf_counter()
    _, errors, _ = _vehicle_version(
        "route_id,service_id,trip_id,vehicle_id\nR1,A,T1,V1\nR1,B,T2,V1\n",
        _two_runs("08:00:00", "09:00:00", "08:30:00", "09:30:00"), cal)
    elapsed = time.perf_counter() - t0
    assert len(errors) == 1
    assert elapsed < 0.2      # 百年日历也不逐日展开

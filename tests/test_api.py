"""API 集成测试：发布、失败保留正式版、按线路+运营日查询、跨午夜班次。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from conftest import upload, valid_files


def test_publish_and_query(client):
    r = upload(client, valid_files(), label="2026冬季")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "published"
    assert body["counts"]["stop_times"] == 11

    cur = client.get("/api/versions/current").json()
    assert cur["label"] == "2026冬季"

    routes = client.get("/api/routes").json()
    assert {r["route_id"] for r in routes["routes"]} == {"R1", "R2"}

    # 周一（2026-11-23 是周一）：WKD 与 ALL 都有效
    sch = client.get("/api/routes/R1/schedule?date=2026-11-23").json()
    assert sch["trip_count"] == 2
    t1 = sch["trips"][0]
    assert t1["trip_id"] == "T1"
    seqs = [s["stop_sequence"] for s in t1["stop_times"]]
    assert seqs == sorted(seqs)
    assert [s["stop_id"] for s in t1["stop_times"]] == ["S1", "S2", "S3", "S4"]

    # 周六（2026-11-21）：R1 只有 WKD 服务，不运营 → 无班次
    sch_sat = client.get("/api/routes/R1/schedule?date=2026-11-21").json()
    assert sch_sat["trip_count"] == 0
    # 但 R2（ALL）周六运营
    sch_sat2 = client.get("/api/routes/R2/schedule?date=2026-11-21").json()
    assert sch_sat2["trip_count"] == 1


def test_after_midnight_schedule(client):
    upload(client, valid_files())
    sch = client.get("/api/routes/R2/schedule?date=2026-11-23").json()
    n1 = sch["trips"][0]
    times = [(s["arrival"], s["arrival_day_offset"], s["arrival_clock"])
             for s in n1["stop_times"]]
    # 跨午夜：24:20 / 24:50 属于同一运营日，day_offset=1，钟点为次日凌晨
    assert times[2] == ("24:20:00", 1, "00:20:00")
    assert times[3] == ("24:50:00", 1, "00:50:00")
    # 时刻按 stop_sequence 递增排列
    arrs = [s["arrival_secs"] for s in n1["stop_times"]]
    assert arrs == sorted(arrs)


def test_failed_version_keeps_current(client):
    assert upload(client, valid_files(), label="good").status_code == 201
    good_id = client.get("/api/versions/current").json()["id"]

    bad = valid_files(stop_times=(
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,08:00:00,08:00:00,S1,1\n"
        "T1,08:10:00,08:12:00,S2,1\n"      # stop_sequence 重复
        "T1,07:00:00,07:05:00,SX,3\n"      # 未知站点 + 时刻倒退
        "T9,09:00:00,09:00:00,S1,1\n"))    # 未知 trip
    r = upload(client, bad, label="bad")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error_count"] >= 3
    assert all({"file", "row", "field", "message"} <= set(e) for e in detail["errors"])

    # 当前正式版不变，数据仍是旧版
    assert client.get("/api/versions/current").json()["id"] == good_id
    routes = client.get("/api/routes").json()
    assert routes["version_id"] == good_id
    assert {r["route_id"] for r in routes["routes"]} == {"R1", "R2"}

    # 失败版本有记录，可查询错误明细
    versions = client.get("/api/versions").json()
    failed = [v for v in versions["versions"] if v["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["error_count"] == detail["error_count"]
    errs = client.get(f"/api/versions/{failed[0]['id']}").json()["errors"]
    assert len(errs) == detail["error_count"]


def test_version_switch_atomic(client):
    assert upload(client, valid_files(), label="v1").status_code == 201
    v1 = client.get("/api/versions/current").json()["id"]

    # 第二版：线路 R1 改名、删掉 R2
    files2 = valid_files(
        routes="route_id,route_short_name,route_long_name,route_type\n"
               "R1,1路快线,中央车站-机场,3\n",
        trips="route_id,service_id,trip_id\nR1,WKD,T1\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\n"
                   "T1,09:00:00,09:00:00,S4,2\n")
    assert upload(client, files2, label="v2").status_code == 201
    v2 = client.get("/api/versions/current").json()["id"]
    assert v2 != v1

    routes = client.get("/api/routes").json()
    assert routes["version_id"] == v2
    assert [r["route_short_name"] for r in routes["routes"]] == ["1路快线"]
    # 旧版本数据完整保留（不可变）
    assert client.get(f"/api/versions/{v1}").json()["status"] == "published"
    # 新版本中 R2 已不存在
    assert client.get("/api/routes/R2/schedule?date=2026-11-23").status_code == 404


def test_no_version_yet(client):
    assert client.get("/api/routes").status_code == 404
    assert client.get("/api/versions/current").status_code == 404


def test_bad_date_param(client):
    upload(client, valid_files())
    assert client.get("/api/routes/R1/schedule?date=2026-13-40").status_code == 422


def test_missing_file(client):
    r = client.post("/api/versions", data={"label": "x"}, files={
        "stops": ("stops.csv", "stop_id,stop_name\nS1,甲\n".encode(), "text/csv"),
        "routes": ("routes.csv", b"route_id\nR1\n", "text/csv"),
        "trips": ("trips.csv", b"route_id,service_id,trip_id\n", "text/csv"),
        "stop_times": ("stop_times.csv", b"trip_id\n", "text/csv"),
        # 缺 calendar
    })
    assert r.status_code == 422


# ---------- 车号 / 车库 ----------

TRIPS_VEHICLE = """\
route_id,service_id,trip_id,trip_headsign,direction_id,vehicle_id
R1,WKD,T1,机场方向,0,BUS-01
R1,WKD,T2,中央车站方向,1,BUS-02
R2,ALL,N1,夜班环线,0,BUS-01
"""


def test_publish_with_vehicle_id_and_schedule(client):
    r = upload(client, valid_files(trips=TRIPS_VEHICLE), label="带车号")
    assert r.status_code == 201, r.text
    sch = client.get("/api/routes/R1/schedule?date=2026-11-23").json()
    veh = {t["trip_id"]: t["vehicle_id"] for t in sch["trips"]}
    assert veh == {"T1": "BUS-01", "T2": "BUS-02"}

    vehicles = client.get("/api/vehicles").json()
    assert vehicles["vehicles"] == ["BUS-01", "BUS-02"]


def test_vehicle_occupancy_by_beijing_day(client):
    upload(client, valid_files(trips=TRIPS_VEHICLE))
    # 2026-11-23（周一）：BUS-01 跑 T1（08:00-09:00）与夜班 N1（23:30 -> 次日 00:50）
    body = client.get("/api/vehicles/BUS-01/occupancy?date=2026-11-23").json()
    assert body["version_id"] == client.get("/api/versions/current").json()["id"]
    # 三段：前一运营日夜班的后半段、白天 T1、当晚夜班的前半段
    assert body["occurrence_count"] == 3
    prev_n1, t1, n1 = body["occurrences"]
    assert prev_n1["trip_id"] == "N1" and prev_n1["service_date"] == "2026-11-22"
    assert prev_n1["overlap_start"] == "2026-11-23 00:00:00"
    assert prev_n1["overlap_end"] == "2026-11-23 00:50:00"
    assert t1["trip_id"] == "T1" and t1["service_date"] == "2026-11-23"
    assert t1["overlap_start"] == "2026-11-23 08:00:00"
    assert t1["overlap_end"] == "2026-11-23 09:00:00"
    assert n1["trip_id"] == "N1" and n1["service_date"] == "2026-11-23"
    assert n1["overlap_start"] == "2026-11-23 23:30:00"
    assert n1["overlap_end"] == "2026-11-24 00:00:00"
    assert n1["crosses_midnight"] is True

    # 次日凌晨：11-23 运营日那次夜班的后半段仍算 11-24 当天
    nxt = client.get("/api/vehicles/BUS-01/occupancy?date=2026-11-24").json()
    spill = [o for o in nxt["occurrences"]
             if o["service_date"] == "2026-11-23" and o["trip_id"] == "N1"]
    assert len(spill) == 1
    assert spill[0]["overlap_start"] == "2026-11-24 00:00:00"
    assert spill[0]["overlap_end"] == "2026-11-24 00:50:00"
    assert spill[0]["start"] == "2026-11-23 23:30:00"
    assert spill[0]["end"] == "2026-11-24 00:50:00"


def test_vehicle_occupancy_weekend_only(client):
    upload(client, valid_files(trips=TRIPS_VEHICLE))
    # 周六 2026-11-21：T1/T2 的服务 WKD 不运营，只有 N1（ALL）
    body = client.get("/api/vehicles/BUS-01/occupancy?date=2026-11-21").json()
    # 只有夜班：11-20（周五）的后半段 + 11-21（周六）的前半段
    assert [o["trip_id"] for o in body["occurrences"]] == ["N1", "N1"]
    assert [o["service_date"] for o in body["occurrences"]] ==         ["2026-11-20", "2026-11-21"]
    assert body["occurrences"][1]["overlap_start"] == "2026-11-21 23:30:00"
    assert body["occurrences"][1]["overlap_end"] == "2026-11-22 00:00:00"
    # BUS-02 周六没有任何计划占用
    empty = client.get("/api/vehicles/BUS-02/occupancy?date=2026-11-21").json()
    assert empty["occurrence_count"] == 0


def test_vehicle_occupancy_reports_source_version(client):
    upload(client, valid_files(trips=TRIPS_VEHICLE), label="第一版")
    v1 = client.get("/api/versions/current").json()["id"]
    body = client.get("/api/vehicles/BUS-01/occupancy?date=2026-11-23").json()
    assert body["version_id"] == v1
    assert body["version_label"] == "第一版"

    # 发布第二版后，车库查询跟随指针指向新版本
    upload(client, valid_files(trips=TRIPS_VEHICLE), label="第二版")
    v2 = client.get("/api/versions/current").json()["id"]
    body2 = client.get("/api/vehicles/BUS-01/occupancy?date=2026-11-23").json()
    assert body2["version_id"] == v2 and body2["version_label"] == "第二版"


def test_vehicle_conflict_keeps_current(client):
    assert upload(client, valid_files(), label="good").status_code == 201
    good_id = client.get("/api/versions/current").json()["id"]

    trips = ("route_id,service_id,trip_id,vehicle_id\n"
             "R1,WKD,T1,V1\nR1,WKD,T2,V1\n")
    stop_times = ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                  "T1,08:00:00,08:00:00,S1,1\nT1,09:00:00,09:00:00,S2,2\n"
                  "T2,08:30:00,08:30:00,S1,1\nT2,09:30:00,09:30:00,S2,2\n")
    r = upload(client, valid_files(trips=trips, stop_times=stop_times),
               label="撞车")
    assert r.status_code == 422
    detail = r.json()["detail"]
    conflict = [e for e in detail["errors"] if e["field"] == "vehicle_id"]
    assert len(conflict) == 1
    assert "V1" in conflict[0]["message"]
    # 当前正式版不变，车库仍按旧版回答
    assert client.get("/api/versions/current").json()["id"] == good_id
    assert client.get("/api/vehicles").json()["version_id"] == good_id
    assert client.get("/api/vehicles").json()["vehicles"] == []


def test_vehicle_unknown_or_bad_date(client):
    upload(client, valid_files(trips=TRIPS_VEHICLE))
    assert client.get("/api/vehicles/NOPE/occupancy?date=2026-11-23").status_code == 404
    assert client.get("/api/vehicles/BUS-01/occupancy?date=2026-13-40").status_code == 422

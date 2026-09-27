import json
import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo
from transfer_guarantee import judge_guarantee, trip_stop_times


class TransferGuaranteeMathTest(unittest.TestCase):
    def test_trip_stop_times_both_directions(self):
        rows = [
            {"stop_id": 1, "travel_minutes_from_previous": 0},
            {"stop_id": 2, "travel_minutes_from_previous": 5},
            {"stop_id": 3, "travel_minutes_from_previous": 7},
        ]
        self.assertEqual(trip_stop_times(rows, 100, 0), {1: 100, 2: 105, 3: 112})
        self.assertEqual(trip_stop_times(rows, 100, 1), {1: 112, 2: 107, 3: 100})

    def test_judge_guarantee_ok_pending_and_missed(self):
        ok = judge_guarantee(arrival_minute=1400, walk_minutes=4, min_buffer_minutes=30, departures=[1455, 1525])
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["shortfall_minutes"], 0)
        tight = judge_guarantee(arrival_minute=1455, walk_minutes=4, min_buffer_minutes=70, departures=[1455, 1525])
        self.assertEqual(tight["status"], "pending_adjustment")
        self.assertEqual(tight["slack_minutes"], 66)
        self.assertEqual(tight["shortfall_minutes"], 4)
        missed = judge_guarantee(arrival_minute=1530, walk_minutes=4, min_buffer_minutes=10, departures=[1455, 1525])
        self.assertEqual(missed["status"], "pending_adjustment")
        self.assertIsNone(missed["latest_catchable_departure"])
        self.assertEqual(missed["shortfall_minutes"], 19)
        self.assertEqual(judge_guarantee(arrival_minute=None, walk_minutes=4, min_buffer_minutes=10, departures=[1525])["status"], "no_arrival")
        self.assertEqual(judge_guarantee(arrival_minute=100, walk_minutes=4, min_buffer_minutes=10, departures=[])["status"], "no_service")


class TransitFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_route_version_review_publish_and_snapshot_isolation(self):
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"])["minutes"], 23)
        disruption = self.db.create_disruption("planner-01", {"code": "D-001", "name": "会展站跳站", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        v1 = disruption["draft_version_id"]
        self.db.add_change(v1, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        with self.assertRaises(DomainError):
            self.db.transition(v1, "planner-01", "planner", "publish")
        self.db.transition(v1, "planner-01", "planner", "submit")
        self.db.transition(v1, "reviewer-01", "reviewer", "approve")
        published = self.db.transition(v1, "reviewer-01", "reviewer", "publish")
        self.assertEqual(published["status"], "published")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)

        v2 = self.db.create_version_copy(disruption["id"], v1, "planner-02", "planner")["id"]
        self.db.add_change(v2, "planner-02", {"kind": "detour", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"], "travel_minutes": 18}, "planner")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v2)["minutes"], 18)
        # Publishing v2 as a draft snapshot does not alter the old published v1.
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)
        self.db.transition(v2, "planner-02", "planner", "submit")
        self.db.transition(v2, "reviewer-02", "reviewer", "approve")
        self.db.transition(v2, "reviewer-02", "reviewer", "publish")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v2)["minutes"], 18)

    def test_cross_midnight_times_and_bad_data_isolation(self):
        times = self.db.trip_times(self.db.list_trips()[0]["id"])
        self.assertEqual(times[0]["clock"], "23:50")
        self.assertEqual(times[-1]["service_minute"], 1461)
        self.assertEqual(times[-1]["clock"], "00:21")
        self.assertEqual(times[-1]["day_offset"], 1)

        fresh = Database(Path(self.tmp.name) / "bad.db")
        result = fresh.import_base("planner-01", {
            "lines": [{"code": "B1", "name": "错误线路"}],
            "stops": [{"code": "B-S1", "name": "站点一", "latitude": 31, "longitude": 121}],
            "line_stops": [
                {"line_code": "B1", "stop_code": "B-S1", "sequence": 0, "travel_minutes_from_previous": 0},
                {"line_code": "B1", "stop_code": "NO-SUCH", "sequence": 1, "travel_minutes_from_previous": 5},
            ],
            "trips": [],
        }, "planner")
        self.assertFalse(result["accepted"])
        self.assertTrue(fresh.list_import_errors())
        self.assertEqual(fresh.list_lines(), [])

    def test_accessibility_and_conflict_validation(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-002", "name": "站点无障碍设施故障", "starts_at": "2026-09-24T00:00:00+08:00", "ends_at": "2026-09-25T00:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        self.db.add_change(version, "planner-01", {"kind": "accessibility_change", "stop_id": self.stops["S4"], "accessible": False}, "planner")
        normal = self.db.route(self.stops["S1"], self.stops["S5"], version, require_accessible=False)
        accessible = self.db.route(self.stops["S1"], self.stops["S5"], version, require_accessible=True)
        self.assertEqual(normal["minutes"], 23)
        self.assertEqual(accessible["minutes"], 31)
        with self.assertRaises(DomainError):
            self.db.add_change(version, "viewer", {"kind": "stop_closure", "stop_id": self.stops["S2"]}, "viewer")

    def test_transfer_guarantee_route_judgment(self):
        lines = {row["code"]: row["id"] for row in self.db.list_lines()}
        disruption = self.db.create_disruption("planner-01", {"code": "D-101", "name": "末班车改道接驳保障", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        guarantee = self.db.add_transfer_guarantee(version, "planner-01", {"transfer_stop_id": self.stops["S4"], "feeder_line_id": lines["L3"], "walk_minutes": 4, "min_buffer_minutes": 30}, "planner")
        self.assertEqual(guarantee["transfer_stop_id"], self.stops["S4"])

        # 23:50 出发走 L2 预计 00:07(1447) 到达 S4，步行后留乘 29 分钟 < 30，待调整差 1 分钟。
        result = self.db.route(self.stops["S1"], self.stops["S4"], version, at_minute=1430)
        judgments = result["transfer_guarantees"]
        self.assertEqual(len(judgments), 1)
        judgment = judgments[0]["judgment"]
        self.assertEqual(judgment["status"], "pending_adjustment")
        self.assertEqual(judgment["arrival_minute"], 1447)
        self.assertEqual(judgment["latest_catchable_departure"], 1480)
        self.assertEqual(judgment["slack_minutes"], 29)
        self.assertEqual(judgment["shortfall_minutes"], 1)
        self.assertEqual(judgment["status_label"], "待调整")

        # 提前出发留乘充足则正常；基线查询不带保障判断。
        early = self.db.route(self.stops["S1"], self.stops["S4"], version, at_minute=1400)
        self.assertEqual(early["transfer_guarantees"][0]["judgment"]["status"], "ok")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S4"])["transfer_guarantees"], [])

    def test_transfer_guarantee_snapshot_freeze_and_recompute(self):
        lines = {row["code"]: row["id"] for row in self.db.list_lines()}
        disruption = self.db.create_disruption("planner-01", {"code": "D-102", "name": "末班接驳保障发布", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        v1 = disruption["draft_version_id"]
        self.db.add_transfer_guarantee(v1, "planner-01", {"transfer_stop_id": self.stops["S4"], "feeder_line_id": lines["L3"], "walk_minutes": 4, "min_buffer_minutes": 30}, "planner")
        self.db.transition(v1, "planner-01", "planner", "submit")
        self.db.transition(v1, "reviewer-01", "reviewer", "approve")
        published = self.db.transition(v1, "reviewer-01", "reviewer", "publish")
        snapshot = json.loads(published["snapshot"])
        # 版本级判断以换乘站末班到达(L1 于 1455 到达 S4)为预计到达。
        self.assertEqual(snapshot["transfer_guarantees"][0]["judgment"]["status"], "pending_adjustment")
        self.assertEqual(snapshot["transfer_guarantees"][0]["judgment"]["shortfall_minutes"], 9)

        # 基础时刻变化：接驳班次整体延后 40 分钟。
        with self.db.connect() as conn:
            conn.execute("UPDATE trips SET departure_minute=departure_minute+40 WHERE line_id=?", (lines["L3"],))
        # 新版本复制保障并按新时刻重算：留乘 61 分钟，正常。
        v2 = self.db.create_version_copy(disruption["id"], v1, "planner-02", "planner")["id"]
        live = self.db.get_version(v2)["transfer_guarantees"][0]["judgment"]
        self.assertEqual(live["status"], "ok")
        self.assertEqual(live["slack_minutes"], 61)
        # 旧发布版本仍显示当时的结论。
        frozen = self.db.get_version(v1)["transfer_guarantees"][0]["judgment"]
        self.assertEqual(frozen["status"], "pending_adjustment")
        self.assertEqual(frozen["shortfall_minutes"], 9)

    def test_transfer_guarantee_validation(self):
        lines = {row["code"]: row["id"] for row in self.db.list_lines()}
        disruption = self.db.create_disruption("planner-01", {"code": "D-103", "name": "保障登记校验", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        with self.assertRaises(DomainError):
            self.db.add_transfer_guarantee(version, "planner-01", {"transfer_stop_id": self.stops["S2"], "feeder_line_id": lines["L3"], "walk_minutes": 4, "min_buffer_minutes": 10}, "planner")
        with self.assertRaises(DomainError):
            self.db.add_transfer_guarantee(version, "planner-01", {"transfer_stop_id": self.stops["S4"], "feeder_line_id": lines["L3"], "walk_minutes": -1, "min_buffer_minutes": 10}, "planner")
        with self.assertRaises(DomainError):
            self.db.add_transfer_guarantee(version, "viewer", {"transfer_stop_id": self.stops["S4"], "feeder_line_id": lines["L3"], "walk_minutes": 4, "min_buffer_minutes": 10}, "viewer")
        self.db.transition(version, "planner-01", "planner", "submit")
        with self.assertRaises(DomainError):
            self.db.add_transfer_guarantee(version, "planner-01", {"transfer_stop_id": self.stops["S4"], "feeder_line_id": lines["L3"], "walk_minutes": 4, "min_buffer_minutes": 10}, "planner")


if __name__ == "__main__":
    unittest.main()

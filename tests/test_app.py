import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, adjusted_cumulative, judge_transfer, seed_demo


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


class TransferGuaranteeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}
        self.lines = {row["code"]: row["id"] for row in self.db.list_lines()}

    def tearDown(self):
        self.tmp.cleanup()

    def _disruption(self, code="D-101"):
        return self.db.create_disruption("planner-01", {"code": code, "name": "末班车临时改道", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")

    def _guarantee(self, version_id, walk=2, minimum=5):
        return self.db.add_guarantee(version_id, "planner-01", {"transfer_stop_id": self.stops["S4"], "feeder_line_id": self.lines["L2"], "walk_minutes": walk, "min_retained_minutes": minimum}, "planner")

    def _publish(self, version_id):
        self.db.transition(version_id, "planner-01", "planner", "submit")
        self.db.transition(version_id, "reviewer-01", "reviewer", "approve")
        self.db.transition(version_id, "reviewer-01", "reviewer", "publish")

    def test_registration_requires_draft_and_valid_references(self):
        version = self._disruption()["draft_version_id"]
        guarantee = self._guarantee(version, walk=2, minimum=5)
        self.assertEqual(guarantee["transfer_stop_id"], self.stops["S4"])
        with self.assertRaises(DomainError):  # L2 不经停 S5
            self.db.add_guarantee(version, "planner-01", {"transfer_stop_id": self.stops["S5"], "feeder_line_id": self.lines["L2"], "walk_minutes": 2, "min_retained_minutes": 5}, "planner")
        with self.assertRaises(DomainError):  # 步行分钟不能为负
            self._guarantee(version, walk=-1)
        with self.assertRaises(DomainError):  # 换乘站不存在
            self.db.add_guarantee(version, "planner-01", {"transfer_stop_id": 9999, "feeder_line_id": self.lines["L2"], "walk_minutes": 2, "min_retained_minutes": 5}, "planner")
        with self.assertRaises(DomainError):  # 角色无权
            self.db.add_guarantee(version, "viewer", {"transfer_stop_id": self.stops["S4"], "feeder_line_id": self.lines["L2"], "walk_minutes": 2, "min_retained_minutes": 5}, "viewer")
        self.db.transition(version, "planner-01", "planner", "submit")
        with self.assertRaises(DomainError):  # 只有草稿可以登记
            self._guarantee(version)

    def test_route_marks_pending_adjustment_with_shortfall(self):
        version = self._disruption()["draft_version_id"]
        self._guarantee(version, walk=2, minimum=10)
        self._guarantee(version, walk=2, minimum=5)
        result = self.db.route(self.stops["S1"], self.stops["S5"], version, at_minute=1430)
        self.assertEqual(result["minutes"], 23)
        evaluations = {item["min_retained_minutes"]: item["judgment"] for item in result["transfer_guarantees"]}
        pending = evaluations[10]
        self.assertEqual(pending["status"], "pending_adjustment")
        self.assertEqual(pending["reason"], "insufficient_retained")
        self.assertEqual(pending["arrival_minute"], 1447)
        self.assertEqual(pending["ready_minute"], 1449)
        self.assertEqual(pending["latest_catchable_departure"], 1457)
        self.assertEqual(pending["retained_minutes"], 8)
        self.assertEqual(pending["shortfall_minutes"], 2)
        okay = evaluations[5]
        self.assertEqual(okay["status"], "ok")
        self.assertEqual(okay["shortfall_minutes"], 0)
        baseline = self.db.route(self.stops["S1"], self.stops["S5"])
        self.assertNotIn("transfer_guarantees", baseline)

    def test_route_marks_missed_last_feeder(self):
        version = self._disruption()["draft_version_id"]
        self._guarantee(version, walk=2, minimum=5)
        result = self.db.route(self.stops["S1"], self.stops["S5"], version, at_minute=1500)
        [judgment] = [item["judgment"] for item in result["transfer_guarantees"]]
        self.assertEqual(judgment["status"], "pending_adjustment")
        self.assertEqual(judgment["reason"], "no_catchable_trip")
        self.assertEqual(judgment["last_departure"], 1457)
        self.assertEqual(judgment["retained_minutes"], -62)
        self.assertEqual(judgment["shortfall_minutes"], 67)

    def test_publish_freezes_judgment_and_base_changes_only_recompute_new_versions(self):
        disruption = self._disruption()
        v1 = disruption["draft_version_id"]
        self._guarantee(v1, walk=2, minimum=5)
        self._publish(v1)
        published = self.db.get_version(v1)
        [frozen] = published["transfer_guarantees"]
        self.assertEqual(frozen["judgment"]["status"], "pending_adjustment")
        self.assertEqual(frozen["judgment"]["arrival_minute"], 1455)
        self.assertEqual(frozen["judgment"]["shortfall_minutes"], 5)
        self.assertEqual(published["snapshot"]["transfer_guarantees"][0]["judgment"]["shortfall_minutes"], 5)
        # 基础时刻变化：直接写入一条更晚的接驳班次，模拟时刻表更新。
        with self.db.connect() as conn:
            conn.execute("INSERT INTO trips(line_id,service_code,direction,departure_minute) VALUES(?,?,0,1500)", (self.lines["L2"], "daily"))
        [still_frozen] = self.db.get_version(v1)["transfer_guarantees"]
        self.assertEqual(still_frozen["judgment"]["status"], "pending_adjustment")
        self.assertEqual(still_frozen["judgment"]["shortfall_minutes"], 5)
        v2 = self.db.create_version_copy(disruption["id"], v1, "planner-02", "planner")["id"]
        [recomputed] = self.db.get_version(v2)["transfer_guarantees"]
        self.assertEqual(recomputed["judgment"]["status"], "ok")
        self.assertEqual(recomputed["judgment"]["latest_catchable_departure"], 1517)
        self.assertEqual(recomputed["judgment"]["shortfall_minutes"], 0)

    def test_detour_delays_last_arrival_in_snapshot(self):
        version = self._disruption()["draft_version_id"]
        self.db.add_change(version, "planner-01", {"kind": "detour", "from_stop_id": self.stops["S2"], "to_stop_id": self.stops["S4"], "travel_minutes": 20}, "planner")
        self._guarantee(version, walk=2, minimum=5)
        self._publish(version)
        [judgment] = [item["judgment"] for item in self.db.get_version(version)["transfer_guarantees"]]
        self.assertEqual(judgment["arrival_minute"], 1460)
        self.assertEqual(judgment["reason"], "no_catchable_trip")
        self.assertEqual(judgment["shortfall_minutes"], 10)


class GuaranteeMathTest(unittest.TestCase):
    def test_adjusted_cumulative_rules(self):
        rows = [
            {"stop_id": 1, "sequence": 0, "travel_minutes_from_previous": 0},
            {"stop_id": 2, "sequence": 1, "travel_minutes_from_previous": 10},
            {"stop_id": 3, "sequence": 2, "travel_minutes_from_previous": 5},
            {"stop_id": 4, "sequence": 3, "travel_minutes_from_previous": 10},
        ]
        self.assertEqual(dict(adjusted_cumulative(rows, [])), {1: 0, 2: 10, 3: 15, 4: 25})
        closed = adjusted_cumulative(rows, [{"kind": "stop_closure", "stop_id": 3}])
        self.assertEqual(dict(closed), {1: 0, 2: 10, 4: 25})
        detoured = adjusted_cumulative(rows, [{"kind": "detour", "from_stop_id": 2, "to_stop_id": 4, "travel_minutes": 20}])
        self.assertEqual(dict(detoured), {1: 0, 2: 10, 3: 15, 4: 30})
        reversed_cum = adjusted_cumulative(rows, [], reverse=True)
        self.assertEqual(dict(reversed_cum), {4: 0, 3: 10, 2: 15, 1: 25})

    def test_judge_transfer_boundaries(self):
        departures = [100, 200]
        judgment = judge_transfer(90, 5, 105, departures)
        self.assertEqual(judgment["status"], "ok")
        self.assertEqual(judgment["retained_minutes"], 105)
        judgment = judge_transfer(90, 5, 106, departures)
        self.assertEqual(judgment["status"], "pending_adjustment")
        self.assertEqual(judgment["reason"], "insufficient_retained")
        self.assertEqual(judgment["shortfall_minutes"], 1)
        missed = judge_transfer(300, 0, 10, departures)
        self.assertEqual(missed["reason"], "no_catchable_trip")
        self.assertEqual(missed["retained_minutes"], -100)
        self.assertEqual(missed["shortfall_minutes"], 110)
        self.assertEqual(judge_transfer(100, 0, 8, [])["reason"], "no_feeder_service")
        self.assertEqual(judge_transfer(None, 0, 8, departures)["reason"], "no_incoming_service")


if __name__ == "__main__":
    unittest.main()

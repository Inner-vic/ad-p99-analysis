import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import state_performance


def line(second, text):
    return f"+0800 2026-09-11-10_32_{second:02d}_000 {text}\n"


def detailed_line(offset_ms, text, pid=101):
    stamp = datetime(2026, 9, 11, 10, 32, 0) + timedelta(milliseconds=offset_ms)
    return f"+0800 {stamp:%Y-%m-%d-%H_%M_%S}_{stamp.microsecond // 1000:03d} {pid} 202 I infer_expert_pcm {text}\n"


class StatePerformanceTests(unittest.TestCase):
    def write_log(self, directory, name, lines):
        (Path(directory) / name).write_text("".join(lines), encoding="utf-8")

    def test_extracts_cpu_frame_rate_and_uses_exact_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.write_log(temporary, "AVM_Service-b", [
                line(3, "perception_state=4 infer_expert_pc_cpu=38.0% sys_cpu=50.0%"),
                line(4, "FPSlast10frames: 8.0 Hz"),
                line(5, "perception_state=1 infer_expert_pc_cpu=19.0% sys_cpu=40.0% FPSlast10frames: 7.0 Hz"),
            ])
            self.write_log(temporary, "AVM_Service-a", [
                line(1, "perception_state=0 infer_expert_pc_cpu=83.8% sys_cpu=61.0%"),
                line(2, "FPSlast10frames: 5.0 Hz"),
                line(6, "perception_state=5 infer_expert_pc_cpu=57.0% sys_cpu=44.0%"),
                line(7, "FPSlast10frames: 9.0 Hz"),
                line(8, "perception_state=2 infer_expert_pc_cpu=190.0% sys_cpu=80.0% FPSlast10frames: 3.0 Hz"),
                line(9, "perception_state=0 infer_expert_pc_cpu=100.0%"),
                "perception_state=0 infer_expert_pc_cpu=100.0% sys_cpu=50.0%\n",
            ])
            result = state_performance.extract(temporary, "2026-09-11 10:32:01", "2026-09-11 10:32:08")

        self.assertEqual(result["status"], "partial")
        self.assertAlmostEqual(result["metrics"]["slow_driver"]["pcm_cpu_percent"]["mean"], 83.8 / 19, places=4)
        self.assertEqual(result["metrics"]["slow_driver"]["system_cpu_percent"]["mean"], 61.0)
        self.assertEqual(result["metrics"]["slow_driver"]["pcm_frame_rate_hz"], {"mean": None, "count": 0})
        self.assertEqual(result["metrics"]["fast_driver"]["pcm_frame_rate_hz"], {"mean": None, "count": 0})
        self.assertEqual(result["metrics"]["parking"]["pcm_frame_rate_hz"], {"mean": None, "count": 0})
        self.assertEqual(result["metrics"]["CNOA"]["pcm_frame_rate_hz"], {"mean": None, "count": 0})
        self.assertEqual(result["coverage"]["complete_cpu_samples_ignored_state"], 0)
        self.assertEqual(result["coverage"]["records_without_timestamp"], 1)

    def test_does_not_assign_stale_or_unknown_state_and_keeps_missing_as_null(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.write_log(temporary, "AVM_Service-a", [
                line(1, "perception_state=0 infer_expert_pc_cpu=19.0% sys_cpu=20.0%"),
                line(4, "FPSlast10frames: 5.0 Hz"),
                line(5, "perception_state=2 infer_expert_pc_cpu=19.0% sys_cpu=20.0% FPSlast10frames: 6.0 Hz"),
                line(6, "FPSlast10frames: not-a-number Hz"),
            ])
            self.write_log(temporary, "netbw-AVM_Service-ignore", [line(2, "perception_state=4 infer_expert_pc_cpu=190.0% sys_cpu=99.0%")])
            result = state_performance.extract(temporary, "2026-09-11 10:32:00", "2026-09-11 10:32:10")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["metrics"]["slow_driver"]["pcm_cpu_percent"], {"mean": 1.0, "count": 1})
        self.assertEqual(result["metrics"]["slow_driver"]["frame_rate_hz"], {"mean": None, "count": 0})
        self.assertEqual(result["metrics"]["fast_driver"]["pcm_cpu_percent"], {"mean": None, "count": 0})
        self.assertEqual(result["coverage"]["fps_samples_unattributed"], 2)
        self.assertEqual(result["coverage"]["fps_samples_ignored_state"], 0)
        self.assertEqual(result["coverage"]["fps_samples_invalid"], 1)

    def test_extracts_pcm_p99_and_validated_ten_frame_fps_window(self):
        lines = []
        nodes = ("lidar", "img", "classifier", "transformer", "dit")
        for index, post_frame in enumerate(range(11, 21)):
            start = index * 100
            pre_frame = post_frame - 1
            ctx = post_frame % 8
            lines.append(detailed_line(
                start,
                f"UroadPreNode::run() frame={pre_frame} | perception_state=0 | "
                "infer_expert_pc_cpu=19.0% | sys_cpu=60.0%",
            ))
            lines.append(detailed_line(start + 10, f"UroadPreNode::run() timing(ms) frame={pre_frame} | TOTAL=10.0"))
            for node_index, node in enumerate(nodes):
                lines.append(detailed_line(
                    start + 20 + node_index,
                    f"[UnifiedInferenceNode_low_{node}] runWithContext ctx={ctx} infer=1 ms",
                ))
            if post_frame == 20:
                lines.append(detailed_line(start + 45, "UroadPostNode FPSlast10frames: 9.75 Hz"))
            lines.append(detailed_line(start + 50, f"UroadPostNode::run() timing(ms) frame={post_frame} | TOTAL=5.0"))

        with tempfile.TemporaryDirectory() as temporary:
            self.write_log(temporary, "AVM_Service-pcm", lines)
            result = state_performance.extract(
                temporary, "2026-09-11 10:32:00", "2026-09-11 10:32:02"
            )

        slow = result["metrics"]["slow_driver"]
        self.assertEqual(slow["pcm_p99_infer_delay_ms"], {"p99": 50.0, "count": 10})
        self.assertEqual(slow["pcm_frame_rate_hz"], {"mean": 9.75, "count": 1})
        self.assertEqual(result["ctx_validation"]["inference_sequence"], 10)
        self.assertEqual(result["fps_diagnostics"]["missing_frames"], 0)
        self.assertEqual(result["time_window"]["semantics"], "[start, end)")

    def test_cli_is_json_and_rejects_invalid_time_range(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.write_log(temporary, "AVM_Service-a", [])
            self.assertEqual(state_performance.main([
                "--log-dir", temporary, "--start", "2026-09-11 10:32:01", "--end", "2026-09-11 10:32:01",
            ]), 2)


if __name__ == "__main__":
    unittest.main()

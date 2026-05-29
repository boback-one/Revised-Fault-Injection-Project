"""Tests for fi_host — run with: pytest packages/host/tests/ -v"""
import json
import pytest
from fi_host.core import GlitchRecord, GlitchResult, GlitchParams, SweepParams, SessionStats
from fi_host.analysis import build_heatmap, find_fault_clusters, generate_report


# ─────────────────────────── FIXTURES ───────────────────────────────────────

def make_record(delay=1000, width=100, result=0, rail=0, mv=1200, clk=5000):
    return GlitchRecord(
        trigger_delay_ns=delay,
        glitch_width_ns=width,
        target_rail=rail,
        result=GlitchResult(result),
        adc_min_mv=mv,
        clock_edges=clk,
        response_byte="0xAA",
    )


def make_sweep_records(n_delay=5, n_width=5):
    records = []
    delays = [1000 * i for i in range(1, n_delay + 1)]
    widths = [100  * i for i in range(1, n_width + 1)]
    for di, d in enumerate(delays):
        for wi, w in enumerate(widths):
            result = GlitchResult.FAULT if (di == 2 and wi == 2) else GlitchResult.OK
            records.append(make_record(delay=d, width=w, result=int(result)))
    return records


# ─────────────────────────── MODEL TESTS ────────────────────────────────────

class TestGlitchRecord:
    def test_from_json_line_short(self):
        line = '{"d":1000,"w":200,"r":0,"res":1,"mv":950,"clk":4321,"byte":"0xBB"}'
        rec  = GlitchRecord.from_json_line(line)
        assert rec is not None
        assert rec.trigger_delay_ns == 1000
        assert rec.glitch_width_ns  == 200
        assert rec.result           == GlitchResult.FAULT
        assert rec.is_fault
        assert not rec.is_crash

    def test_from_json_line_full(self):
        line = json.dumps({
            "trigger_delay_ns": 5000,
            "glitch_width_ns":  500,
            "target_rail":      2,
            "result":           2,
            "adc_min_mv":       800,
            "clock_edges":      9999,
            "response_byte":    "0x00",
        })
        rec = GlitchRecord.from_json_line(line)
        assert rec is not None
        assert rec.is_crash
        assert rec.rail_name == "VCC_PLL"

    def test_from_json_line_invalid(self):
        assert GlitchRecord.from_json_line("not json") is None
        assert GlitchRecord.from_json_line('{"foo":"bar"}') is None

    def test_result_label(self):
        assert make_record(result=0).result_label == "OK"
        assert make_record(result=1).result_label == "FAULT"
        assert make_record(result=2).result_label == "CRASH"
        assert make_record(result=3).result_label == "TIMEOUT"


class TestParams:
    def test_glitch_cmd(self):
        p   = GlitchParams(trigger_delay_ns=1500, glitch_width_ns=200, target_rail=3)
        cmd = p.to_firmware_cmd()
        assert "GLITCH" in cmd
        assert "1500" in cmd
        assert "200" in cmd
        assert "3" in cmd

    def test_sweep_total(self):
        p = SweepParams(
            delay_start_ns=1000, delay_end_ns=5000, delay_step_ns=1000,
            width_start_ns=100,  width_end_ns=500,  width_step_ns=100,
        )
        assert p.total_combinations == 5 * 5  # 5 delays × 5 widths

    def test_sweep_cmd(self):
        p   = SweepParams(delay_start_ns=100, delay_end_ns=1000, delay_step_ns=100,
                          width_start_ns=10,  width_end_ns=100,  width_step_ns=10)
        cmd = p.to_firmware_cmd()
        assert cmd.startswith("SWEEP")


class TestSessionStats:
    def test_accumulation(self):
        stats = SessionStats()
        stats.record(make_record(result=0))
        stats.record(make_record(result=1))
        stats.record(make_record(result=2))
        stats.record(make_record(result=1))
        assert stats.total   == 4
        assert stats.ok      == 1
        assert stats.fault   == 2
        assert stats.crash   == 1
        assert round(stats.fault_rate, 1) == 50.0


# ─────────────────────────── ANALYSIS TESTS ─────────────────────────────────

class TestHeatmap:
    def test_basic_shape(self):
        records = make_sweep_records(4, 4)
        hm      = build_heatmap(records)
        assert len(hm["delays"]) == 4
        assert len(hm["widths"])  == 4
        assert len(hm["grid"])   == 4
        assert len(hm["grid"][0])== 4

    def test_fault_coords(self):
        records = make_sweep_records(5, 5)
        hm      = build_heatmap(records)
        assert len(hm["fault_coords"]) >= 1
        for fc in hm["fault_coords"]:
            assert "d" in fc and "w" in fc

    def test_empty(self):
        hm = build_heatmap([])
        assert hm["delays"] == []
        assert hm["grid"]   == []


class TestClustering:
    def test_finds_cluster(self):
        records = []
        for d in range(5000, 8000, 200):
            for w in range(100, 300, 20):
                records.append(make_record(delay=d, width=w, result=1))
        clusters = find_fault_clusters(records, eps_delay=500, eps_width=100, min_samples=2)
        assert len(clusters) >= 1
        assert clusters[0]["size"] > 1

    def test_no_faults(self):
        records = [make_record(result=0) for _ in range(20)]
        assert find_fault_clusters(records) == []


class TestReport:
    def test_structure(self):
        records = make_sweep_records(6, 6)
        report  = generate_report(records)
        assert "summary"   in report
        assert "clusters"  in report
        assert "heatmap"   in report
        assert "voltage"   in report
        assert "top_faults" in report
        assert report["summary"]["total"] == 36

    def test_counts_consistent(self):
        records = make_sweep_records(4, 4)
        report  = generate_report(records)
        s       = report["summary"]
        assert s["ok"] + s["fault"] + s["crash"] + s["timeout"] == s["total"]

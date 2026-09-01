#!/usr/bin/env python3
"""Unit tests for crps-collector parsers and metric emission.

Run:  python3 -m unittest test_crps_collector  (from this dir)

The canned replies below are real captures from a Comino CRPS controller
(4x Great Wall CRPS2000D2), including the char-by-char VT100 echo the
controller prepends — so the tests exercise the ANSI stripping too.
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "crps_collector", os.path.join(_HERE, "crps-collector.py")
)
cc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cc)

# Real `show crps` reply (4 modules, all Normal). The leading garble is
# the controller echoing "show crps" one char at a time with cursor moves.
SHOW_CRPS = (
    "\x1b[Ks\x1b[109D\x1b[6C\x1b[1C\x1b[Kh\x1b[109D\x1b[6C\x1b[2C\x1b[Kr\r\n\r\n"
    "| PMBus    |  CRPS 1 |  CRPS 2 |  CRPS 3 |  CRPS 4 |\r\n"
    "|--------------------------------------------------|\r\n"
    "| Temp1:   |   36.44 |   30.12 |   30.44 |   35.44 |\r\n"
    "| Temp2:   |   43.75 |   43.62 |   40.00 |   43.56 |\r\n"
    "| Fan:     |   11904 |   11808 |    9888 |   11664 |\r\n"
    "| Vin:     |  221.00 |  220.25 |  232.00 |  232.50 |\r\n"
    "| Vout:    |   12.19 |   12.19 |   12.19 |   12.21 |\r\n"
    "| Iin:     |    0.52 |    0.52 |    0.50 |    0.54 |\r\n"
    "| Iout:    |    7.00 |    6.88 |    6.97 |    8.02 |\r\n"
    "| Pin:     |  105.00 |  104.00 |  102.00 |  115.00 |\r\n"
    "| Pout:    |   85.25 |   82.62 |   84.88 |   97.88 |\r\n"
    "| VinType: |    0x01 |    0x01 |    0x01 |    0x01 |\r\n"
    "| Status:  |  0x0000 |  0x0000 |  0x0000 |  0x0000 |\r\n"
    "| SmartOn: |    0x00 |    0x00 |    0x00 |    0x00 |\r\n"
    "crps> "
)

# Real `show alarms` reply, all clear.
SHOW_ALARMS = (
    "show alarms\r\n\r\n"
    "Alarms: 00000000 (0x00)\r\n\r\n"
    "| Code | Bit | Name                       | State |\r\n"
    "|-------------------------------------------------|\r\n"
    "|   4  |  0  | ALARM_CRPS_NORESERV        |   0   |\r\n"
    "|   5  |  1  | ALARM_CRPS_LOWNUM_PS_OFF   |   0   |\r\n"
    "|   7  |  3  | ALARM_VOLTAGE_12V          |   0   |\r\n\r\n"
    "Faults: 00000000 (0x00)\r\n\r\n"
    "| Code | Bit | Name                       | State |\r\n"
    "|-------------------------------------------------|\r\n"
    "|  12  |  0  | FAULT_CRPS_LOWNUM          |   0   |\r\n"
    "|  18  |  6  | FAULT_OVERHEAT             |   0   |\r\n\r\n"
    "crps> "
)

# A fault-active variant: ALARM_CRPS_NORESERV set, aggregate words nonzero.
SHOW_ALARMS_FAULT = (
    "Alarms: 00000001 (0x01)\r\n"
    "|   4  |  0  | ALARM_CRPS_NORESERV        |   1   |\r\n"
    "|   5  |  1  | ALARM_CRPS_LOWNUM_PS_OFF   |   0   |\r\n"
    "Faults: 00000004 (0x04)\r\n"
    "|  12  |  0  | FAULT_CRPS_LOWNUM          |   0   |\r\n"
    "|  14  |  2  | FAULT_COOLING              |   1   |\r\n"
    "crps> "
)

# Real `show params` reply (JSON), workQty=2 reserveQty=2 crEnable=false.
SHOW_PARAMS = (
    "\x1b[Ks\x1b[109D\x1b[6C\x1b[1Cp\r\n"
    '{"temp":{"limit_1":80,"limit_2":120},'
    '"crps":{"workQty":2,"reserveQty":2,"crEnable":false,'
    '"crState":[false,false,false,false],"crType":[0,0,0,0]},'
    '"combineMode":false}\r\n'
    "crps> "
)

# What the RM cooling controller answers `show crps` with — not CRPS.
COOLING_REPLY = "show crps\x1b[?3l\r\nERROR: unknown command 'show'\r\nUse 'help'\r\n\r\n> "


class ParseShowCrps(unittest.TestCase):
    def test_parses_all_modules(self):
        psus = cc.parse_show_crps(SHOW_CRPS)
        self.assertIsNotNone(psus)
        self.assertEqual(sorted(psus.keys()), ["1", "2", "3", "4"])
        self.assertEqual(psus["1"]["Vin"], "221.00")
        self.assertEqual(psus["4"]["Pout"], "97.88")
        self.assertEqual(psus["1"]["Status"], "0x0000")

    def test_cooling_controller_rejected(self):
        self.assertIsNone(cc.parse_show_crps(COOLING_REPLY))

    def test_garbage_rejected(self):
        self.assertIsNone(cc.parse_show_crps("not a table\r\ncrps> "))


class ParseShowAlarms(unittest.TestCase):
    def test_all_clear_is_normal(self):
        state, flags = cc.parse_show_alarms(SHOW_ALARMS)
        self.assertEqual(state, "normal")
        self.assertEqual(flags["ALARM_CRPS_NORESERV"], 0)
        self.assertEqual(flags["FAULT_OVERHEAT"], 0)
        self.assertIn("ALARM_VOLTAGE_12V", flags)

    def test_fault_outranks_alarm(self):
        state, flags = cc.parse_show_alarms(SHOW_ALARMS_FAULT)
        self.assertEqual(state, "fault")
        self.assertEqual(flags["ALARM_CRPS_NORESERV"], 1)
        self.assertEqual(flags["FAULT_COOLING"], 1)

    def test_alarm_only(self):
        text = "Alarms: 00000001 (0x01)\r\nFaults: 00000000 (0x00)\r\ncrps> "
        state, _ = cc.parse_show_alarms(text)
        self.assertEqual(state, "alarm")

    def test_no_section_returns_none(self):
        self.assertIsNone(cc.parse_show_alarms("nothing here\r\ncrps> "))


class ParseShowParams(unittest.TestCase):
    def test_redundancy_config(self):
        p = cc.parse_show_params(SHOW_PARAMS)
        self.assertEqual(p["crps"]["workQty"], 2)
        self.assertEqual(p["crps"]["reserveQty"], 2)
        self.assertFalse(p["crps"]["crEnable"])

    def test_no_json_returns_none(self):
        self.assertIsNone(cc.parse_show_params("no json\r\ncrps> "))


class SelectCrpsDevice(unittest.TestCase):
    def test_skips_cooling_and_picks_crps(self):
        # reader returns None for the cooling controller (parse fails),
        # the canned triple for the CRPS one.
        replies = {
            "/dev/ttyACM0": None,
            "/dev/ttyACM1": (SHOW_CRPS, SHOW_ALARMS, SHOW_PARAMS),
        }
        sel = cc.select_crps_device(
            ["/dev/ttyACM0", "/dev/ttyACM1"], lambda d: replies[d]
        )
        self.assertIsNotNone(sel)
        self.assertEqual(sel[0], "/dev/ttyACM1")

    def test_none_when_no_match(self):
        self.assertIsNone(
            cc.select_crps_device(["/dev/ttyACM0"], lambda d: None)
        )


class BuildMetrics(unittest.TestCase):
    def setUp(self):
        self.m = cc.build_metrics(SHOW_CRPS, SHOW_ALARMS, SHOW_PARAMS)

    def test_module_readings(self):
        self.assertEqual([m["module"] for m in self.m["modules"]],
                         ["1", "2", "3", "4"])
        m1 = self.m["modules"][0]
        self.assertEqual(m1["vin"], 221.00)
        self.assertEqual(m1["vout"], 12.19)
        self.assertEqual(m1["iout"], 7.00)
        self.assertEqual(m1["pin"], 105.00)
        self.assertEqual(m1["pout"], 85.25)
        self.assertEqual(m1["temp1"], 36.44)
        self.assertEqual(m1["temp2"], 43.75)
        self.assertEqual(m1["fan"], 11904)

    def test_present_and_ok(self):
        for m in self.m["modules"]:
            self.assertTrue(m["present"])
            self.assertTrue(m["ok"])

    def test_state_and_config(self):
        self.assertEqual(self.m["state"], "normal")
        self.assertEqual(self.m["work_qty"], 2)
        self.assertEqual(self.m["reserve_qty"], 2)
        self.assertFalse(self.m["redundancy_enabled"])

    def test_status_nonzero_marks_not_ok(self):
        crps = SHOW_CRPS.replace("| Status:  |  0x0000 |", "| Status:  |  0x0802 |", 1)
        m = cc.build_metrics(crps, SHOW_ALARMS, SHOW_PARAMS)
        self.assertFalse(m["modules"][0]["ok"])
        self.assertTrue(m["modules"][1]["ok"])


class WriteProm(unittest.TestCase):
    def test_emitted_series(self):
        import tempfile
        m = cc.build_metrics(SHOW_CRPS, SHOW_ALARMS, SHOW_PARAMS)
        with tempfile.TemporaryDirectory() as d:
            orig = cc.OUT
            cc.OUT = os.path.join(d, "crps.prom")
            try:
                cc.write_prom(m)
                with open(cc.OUT) as fh:
                    body = fh.read()
            finally:
                cc.OUT = orig
        self.assertIn('node_crps_psu_vin_volts{module="1"} 221.0', body)
        self.assertIn('node_crps_psu_temp_celsius{module="1",probe="2"} 43.75', body)
        self.assertIn('node_crps_psu_present{module="3"} 1', body)
        self.assertIn('node_crps_psu_ok{module="2"} 1', body)
        self.assertIn('node_crps_state{state="normal"} 1', body)
        self.assertIn('node_crps_state{state="fault"} 0', body)
        self.assertIn('node_crps_alarm{flag="ALARM_CRPS_NORESERV"} 0', body)
        self.assertIn("node_crps_work_qty 2", body)
        self.assertIn("node_crps_reserve_qty 2", body)
        self.assertIn("node_crps_redundancy_enabled 0", body)

    def test_empty_when_no_controller(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            orig = cc.OUT
            cc.OUT = os.path.join(d, "crps.prom")
            try:
                cc.write_prom(None)
                with open(cc.OUT) as fh:
                    body = fh.read()
            finally:
                cc.OUT = orig
        # headers present, no module series
        self.assertIn("# TYPE node_crps_psu_vin_volts gauge", body)
        self.assertNotIn('node_crps_psu_vin_volts{', body)


# Real `show log` record lines (full 106-field entries) captured from nv4.
LOG_NORMAL = (
    "13;0;Normal;28816;4.98;0.03;0.72;0.22;0.22;3.30;24.94;24.94;24.69;24.62;"
    "24.62;24.69;25.38;26.50;25.75;0;2;1;6384;25.12;26.41;232.00;0.00;0.20;0.00;"
    "0.00;10.00;0.00;0.00;1;23;0;2112;0;0;0;0;0;2;6208;25.69;27.09;231.00;0.00;"
    "0.20;0.00;0.00;10.00;0.00;0.00;1;23;0;2112;0;0;0;0;0;3;840;25.88;26.50;"
    "231.00;0.00;0.20;0.00;0.00;10.00;0.00;0.00;1;23;0;2112;0;0;0;0;0;4;0;26.56;"
    "27.97;229.50;0.00;0.23;0.00;0.00;67.00;0.00;0.00;1;23;0;2112;0;0;0;0;0"
)
# Fault entry: 12V/output rails collapsed (header field 5VSB=0.19, 12V=0.04).
LOG_FAULT = (
    "13;0;Fault;28803;0.19;0.01;0.54;0.04;0.22;3.30;24.94;24.94;24.69;24.62;"
    "24.62;24.69;25.38;26.50;25.75;0;0;1;6384;25.12;26.41;232.00;0.00;0.20;0.00;"
    "0.00;10.00;0.00;0.00;1;23;0;2112;0;0;0;0;0;2;6208;25.69;27.09;231.00;0.00;"
    "0.20;0.00;0.00;10.00;0.00;0.00;1;23;0;2112;0;0;0;0;0;3;840;25.88;26.50;"
    "231.00;0.00;0.20;0.00;0.00;10.00;0.00;0.00;1;23;0;2112;0;0;0;0;0;4;0;26.56;"
    "27.97;229.50;0.00;0.23;0.00;0.00;67.00;0.00;0.00;1;23;0;2112;0;0;0;0;0"
)
LOG_ALARM = "6;0;Alarm;29756;4.98;3.28;4.99;12.14;-11.7;3.30;1;2;3"

# Real entry where PSU4 alone lost its AC input (Vin=0.00, Vout=0.00) while
# PSU1/2/3 held ~233V / 12.19V — the single-PSU dropout we want to attribute.
LOG_PSU4_FAULT = (
    "13;0;Fault;240381;4.98;3.28;4.99;12.18;-11.73;3.30;24.44;24.44;24.12;"
    "23.81;23.62;23.44;23.50;28.00;24.25;0;0;"
    "1;11904;36.94;43.75;233.50;12.19;0.59;8.86;108.12;125.00;0.00;0.00;1;23;0;0;0;0;0;0;0;"
    "2;11728;30.09;43.56;233.25;12.19;0.62;9.31;113.62;132.00;0.00;0.00;1;23;0;0;0;0;0;0;0;"
    "3;9648;30.53;39.75;234.25;12.19;0.59;8.88;106.50;126.00;0.00;0.00;1;23;0;0;0;0;0;0;0;"
    "4;6416;35.69;42.38;0.00;0.00;0.00;0.00;0.00;0.00;0.00;0.00;0;23;0;10312;0;0;56;0;0"
)


def log_reply(page, total, data):
    """Wrap a record line as a full `show log` reply (with echo + prompt)."""
    return f"show log\r\npage {page}/{total}\r\n{data}\r\ncrps> "


class ParseLogPage(unittest.TestCase):
    def test_parses_page_and_data(self):
        res = cc.parse_log_page(log_reply(67, 275, LOG_NORMAL))
        self.assertIsNotNone(res)
        page, total, data = res
        self.assertEqual((page, total), (67, 275))
        self.assertTrue(data.startswith("13;0;Normal;28816"))

    def test_no_page_returns_none(self):
        self.assertIsNone(cc.parse_log_page("show log 1\r\ncrps> "))

    def test_empty_returns_none(self):
        self.assertIsNone(cc.parse_log_page(""))


class ParseLogEntry(unittest.TestCase):
    def test_state_and_ts(self):
        self.assertEqual(cc.parse_log_entry(LOG_NORMAL), {"state": "normal", "ts": 28816})
        self.assertEqual(cc.parse_log_entry(LOG_FAULT), {"state": "fault", "ts": 28803})
        self.assertEqual(cc.parse_log_entry(LOG_ALARM), {"state": "alarm", "ts": 29756})

    def test_no_state_token(self):
        self.assertIsNone(cc.parse_log_entry("1;2;3;4;5;6"))

    def test_key_is_stable_and_distinct(self):
        self.assertEqual(cc.entry_key(LOG_FAULT), cc.entry_key(LOG_FAULT))
        self.assertNotEqual(cc.entry_key(LOG_FAULT), cc.entry_key(LOG_NORMAL))


class HarvestEntries(unittest.TestCase):
    def test_counts_by_state(self):
        st = cc.new_state()
        n = cc.harvest_entries(st, [LOG_NORMAL, LOG_FAULT, LOG_ALARM], count=True)
        self.assertEqual(n, 3)
        self.assertEqual(st["counts"], {"normal": 1, "alarm": 1, "fault": 1})

    def test_dedup_across_calls(self):
        st = cc.new_state()
        cc.harvest_entries(st, [LOG_FAULT], count=True)
        # the same entry re-read on the next ring lap must not double-count
        added = cc.harvest_entries(st, [LOG_FAULT, LOG_NORMAL], count=True)
        self.assertEqual(added, 1)
        self.assertEqual(st["counts"], {"normal": 1, "alarm": 0, "fault": 1})

    def test_seed_marks_seen_without_counting(self):
        st = cc.new_state()
        # baseline seed: backlog is recorded as seen but NOT counted
        cc.harvest_entries(st, [LOG_FAULT, LOG_NORMAL], count=False)
        self.assertEqual(st["counts"], {"normal": 0, "alarm": 0, "fault": 0})
        # a later real re-read of the same entries stays at zero (deduped)
        cc.harvest_entries(st, [LOG_FAULT, LOG_NORMAL], count=True)
        self.assertEqual(st["counts"], {"normal": 0, "alarm": 0, "fault": 0})
        # only a genuinely new entry counts
        cc.harvest_entries(st, [LOG_ALARM], count=True)
        self.assertEqual(st["counts"]["alarm"], 1)

    def test_seen_set_bounded(self):
        st = cc.new_state()
        lines = [f"13;0;Fault;{i};x;y;z;w;v;u" for i in range(cc.MAX_SEEN + 50)]
        cc.harvest_entries(st, lines, count=True)
        self.assertLessEqual(len(st["seen"]), cc.MAX_SEEN)


class ModuleEvents(unittest.TestCase):
    def test_parse_modules(self):
        mods = cc.parse_log_modules(LOG_PSU4_FAULT)
        self.assertEqual(sorted(mods.keys()), ["1", "2", "3", "4"])
        self.assertEqual(mods["1"]["vin"], 233.50)
        self.assertEqual(mods["1"]["vout"], 12.19)
        self.assertEqual(mods["4"]["vin"], 0.0)
        self.assertEqual(mods["4"]["vout"], 0.0)

    def test_asymmetric_psu4_drop_attributed(self):
        ev = cc.module_events(LOG_PSU4_FAULT)
        # PSU4 lost both input and output while 1/2/3 are healthy
        self.assertEqual(set(ev.keys()), {"4"})
        self.assertEqual(ev["4"], {"input_loss", "output_off"})

    def test_bank_wide_off_is_not_attributed(self):
        # LOG_NORMAL has all four modules with Vout=0 — a whole-bank-off
        # snapshot, not a single-PSU fault, so nothing is flagged.
        self.assertEqual(cc.module_events(LOG_NORMAL), {})

    def test_harvest_tallies_per_module(self):
        st = cc.new_state()
        cc.harvest_entries(st, [LOG_PSU4_FAULT, LOG_NORMAL], count=True)
        self.assertEqual(st["module_events"], {"4": {"input_loss": 1, "output_off": 1}})

    def test_seed_does_not_tally_modules(self):
        st = cc.new_state()
        cc.harvest_entries(st, [LOG_PSU4_FAULT], count=False)
        self.assertEqual(st["module_events"], {})

    def test_dedup_no_double_count(self):
        st = cc.new_state()
        cc.harvest_entries(st, [LOG_PSU4_FAULT], count=True)
        cc.harvest_entries(st, [LOG_PSU4_FAULT], count=True)  # re-read same entry
        self.assertEqual(st["module_events"]["4"]["input_loss"], 1)


class WritePromLog(unittest.TestCase):
    def test_emits_log_counters(self):
        import tempfile
        m = cc.build_metrics(SHOW_CRPS, SHOW_ALARMS, SHOW_PARAMS)
        m["log"] = {
            "counts": {"normal": 5, "alarm": 2, "fault": 3},
            "module_events": {"4": {"input_loss": 7, "output_off": 3}},
            "pages_total": 120, "ring_pages": 275, "seeded": True,
        }
        with tempfile.TemporaryDirectory() as d:
            orig = cc.OUT
            cc.OUT = os.path.join(d, "crps.prom")
            try:
                cc.write_prom(m)
                with open(cc.OUT) as fh:
                    body = fh.read()
            finally:
                cc.OUT = orig
        self.assertIn("# TYPE node_crps_log_event_total counter", body)
        self.assertIn('node_crps_log_event_total{state="fault"} 3', body)
        self.assertIn('node_crps_log_event_total{state="normal"} 5', body)
        self.assertIn("node_crps_log_harvest_pages_total 120", body)
        self.assertIn("node_crps_log_ring_pages 275", body)
        self.assertIn("node_crps_log_seeded 1", body)
        self.assertIn('node_crps_psu_event_total{module="4",kind="input_loss"} 7', body)
        self.assertIn('node_crps_psu_event_total{module="4",kind="output_off"} 3', body)
        # present-but-eventless modules emit a 0 baseline (no "No data")
        self.assertIn('node_crps_psu_event_total{module="1",kind="input_loss"} 0', body)


if __name__ == "__main__":
    unittest.main()

class NormalizedStateTests(unittest.TestCase):
    def test_fills_missing_count_states(self):
        state = cc._normalized_state({"counts": {"normal": 2}, "pages_total": 5})
        self.assertEqual(state["counts"], {"normal": 2, "alarm": 0, "fault": 0})
        self.assertEqual(state["pages_total"], 5)

    def test_non_dict_gets_defaults(self):
        self.assertEqual(cc._normalized_state("bad"), cc.new_state())

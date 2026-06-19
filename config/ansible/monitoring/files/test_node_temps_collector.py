#!/usr/bin/env python3
"""Unit tests for node-temps-collector Comino device selection.

Run:  python3 -m unittest test_node_temps_collector  (from this dir)
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "node_temps_collector", os.path.join(_HERE, "node-temps-collector.py")
)
ntc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ntc)

# A real :get_data reply captured from a Comino RM controller (24 fields).
VALID_LINE = (
    "115210374;15181165;0;0;12,11;32,7;20,2;19,2;21,2;23,5;24,2;79;"
    "3321;2133;3216;2128;3287;2222;4016;4050;9,0;0;1;3422"
)
# What the CRPS power-supply controller (the other STM32 ACM device)
# returns for :get_data — an error string, not semicolon-delimited data.
CRPS_LINE = "command: ':get_data' Not found."


class ParseCominoData(unittest.TestCase):
    def test_valid_line_parses_to_field_map(self):
        data = ntc.parse_comino_data(VALID_LINE)
        self.assertIsNotNone(data)
        self.assertEqual(len(data), len(ntc.COMINO_FIELDS))
        self.assertEqual(data["T3"], "21,2")   # air inlet
        self.assertEqual(data["CH7"], "4016")  # pump 1

    def test_crps_error_line_rejected(self):
        self.assertIsNone(ntc.parse_comino_data(CRPS_LINE))

    def test_empty_line_rejected(self):
        self.assertIsNone(ntc.parse_comino_data(""))

    def test_wrong_field_count_rejected(self):
        self.assertIsNone(ntc.parse_comino_data("1;2;3"))


class SelectCominoDevice(unittest.TestCase):
    def test_skips_crps_and_picks_comino(self):
        # ttyACM0 is the CRPS controller, ttyACM1 is the Comino RM —
        # the real ordering observed on a node with both controllers.
        replies = {
            "/dev/ttyACM0": (CRPS_LINE, {}),
            "/dev/ttyACM1": (VALID_LINE, {"CH7": "pump 1"}),
        }
        sel = ntc.select_comino_device(
            ["/dev/ttyACM0", "/dev/ttyACM1"], lambda d: replies[d]
        )
        self.assertIsNotNone(sel)
        dev, data, labels = sel
        self.assertEqual(dev, "/dev/ttyACM1")
        self.assertEqual(data["T3"], "21,2")
        self.assertEqual(labels, {"CH7": "pump 1"})

    def test_picks_first_when_comino_is_acm0(self):
        # Single-controller node: Comino on ttyACM0.
        sel = ntc.select_comino_device(
            ["/dev/ttyACM0"], lambda d: (VALID_LINE, {})
        )
        self.assertIsNotNone(sel)
        self.assertEqual(sel[0], "/dev/ttyACM0")

    def test_returns_none_when_no_device_matches(self):
        sel = ntc.select_comino_device(
            ["/dev/ttyACM0"], lambda d: (CRPS_LINE, {})
        )
        self.assertIsNone(sel)

    def test_skips_unreadable_devices(self):
        # reader returns None on I/O failure (e.g. persistent EBUSY).
        replies = {"/dev/ttyACM0": None, "/dev/ttyACM1": (VALID_LINE, {})}
        sel = ntc.select_comino_device(
            ["/dev/ttyACM0", "/dev/ttyACM1"], lambda d: replies[d]
        )
        self.assertIsNotNone(sel)
        self.assertEqual(sel[0], "/dev/ttyACM1")

    def test_empty_candidate_list(self):
        self.assertIsNone(ntc.select_comino_device([], lambda d: None))


if __name__ == "__main__":
    unittest.main()

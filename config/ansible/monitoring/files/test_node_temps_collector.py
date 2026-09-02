#!/usr/bin/env python3
"""Unit tests for node-temps-collector Comino device selection.

Run:  python3 -m unittest test_node_temps_collector  (from this dir)
"""
import importlib.util
import os
import subprocess
import unittest
from unittest import mock

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


class CominoQueryTests(unittest.TestCase):
    class Port:
        def __init__(self, chunks):
            self.chunks = list(chunks)
            self.reads = 0

        def reset_input_buffer(self):
            pass

        def write(self, _data):
            pass

        def flush(self):
            pass

        def read(self, _size):
            self.reads += 1
            return self.chunks.pop(0)

    def query(self, chunks, times=(0, .1, .2, .3, .9)):
        port = self.Port(chunks)
        with mock.patch.object(ntc.time, 'monotonic', side_effect=times):
            got = ntc._comino_query(port, ':get_data')
        return got, port

    def test_reads_single_terminated_chunk(self):
        got, port = self.query([b'response\r\n'])
        self.assertEqual(got, 'response')
        self.assertEqual(port.reads, 1)

    def test_assembles_chunks_until_termination(self):
        got, port = self.query([b'resp', b'onse\r\n'])
        self.assertEqual(got, 'response')
        self.assertEqual(port.reads, 2)

    def test_stops_on_empty_chunk_after_data(self):
        got, port = self.query([b'resp', b'', b'unread'])
        self.assertEqual(got, 'resp')
        self.assertEqual(port.reads, 2)

    def test_timeout_returns_partial_data(self):
        got, port = self.query([b'response'], times=(0, .1, .6, .9))
        self.assertEqual(got, 'response')
        self.assertEqual(port.reads, 1)


class CollectIpmiTests(unittest.TestCase):
    @staticmethod
    def result(stdout='', returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout=stdout,
                                          stderr='')

    def test_parses_readings_and_roles(self):
        stdout = (
            'INLET_AIR_TEMP | 0Bh | ok | 55.1 | 20 degrees C\n'
            'SLOT1_GPU_TEMP | 18h | ok | 11.1 | 35 degrees C\n'
            'CPU_TEMP | 20h | nc | 30.1 | 45 degrees C\n'
        )
        with mock.patch.object(ntc.subprocess, 'run',
                               return_value=self.result(stdout)):
            self.assertEqual(
                ntc.collect_ipmi(),
                [('INLET_AIR_TEMP', 'ambient', 20.0),
                 ('SLOT1_GPU_TEMP', 'gpu', 35.0),
                 ('CPU_TEMP', 'cpu', 45.0)])

    def test_skips_unreadable_and_nonnumeric(self):
        stdout = (
            'SLOT2_GPU_TEMP | 19h | ns | 11.1 | No Reading\n'
            'OTHER | 21h | ok | 12.1 | maybe\n'
            'SHORT | 22h | ok\n'
        )
        with mock.patch.object(ntc.subprocess, 'run',
                               return_value=self.result(stdout)):
            self.assertEqual(ntc.collect_ipmi(), [])

    def test_nonzero_returncode_is_empty(self):
        with mock.patch.object(ntc.subprocess, 'run',
                               return_value=self.result('x', returncode=1)):
            self.assertEqual(ntc.collect_ipmi(), [])

    def test_missing_or_slow_ipmitool_is_empty(self):
        for exc in (FileNotFoundError('missing'),
                    subprocess.TimeoutExpired('ipmitool', 15)):
            with mock.patch.object(ntc.subprocess, 'run', side_effect=exc):
                self.assertEqual(ntc.collect_ipmi(), [])


class CollectNvidiaTests(unittest.TestCase):
    @staticmethod
    def result(stdout='', returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout=stdout,
                                          stderr='')

    GPU_ROW = ('0, 00000000:01:00.0, Test GPU, 52, 0, 0, 100, 100, 10.5,N/A,N/A\n')

    def test_merges_throttle_flags(self):
        throttle = self.result(
            '0, 00000000:01:00.0, Not Active, Active\n'
        )
        with mock.patch.object(ntc.subprocess, 'run',
                               side_effect=[self.result(self.GPU_ROW),
                                            throttle]):
            gpus = ntc.collect_nvidia()
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["temp_c"], 52.0)
        self.assertFalse(gpus[0]["hw_slow"])
        self.assertTrue(gpus[0]["sw_slow"])

    def test_unsupported_throttle_query_degrades_to_false(self):
        # Older drivers reject the clocks_throttle_reasons fields; temps
        # must still be collected with throttle flags defaulting to off.
        with mock.patch.object(ntc.subprocess, 'run',
                               side_effect=[self.result(self.GPU_ROW),
                                            self.result(returncode=1)]):
            gpus = ntc.collect_nvidia()
        self.assertEqual(len(gpus), 1)
        self.assertFalse(gpus[0]["hw_slow"])
        self.assertFalse(gpus[0]["sw_slow"])

    def test_throttle_parser_rejects_malformed_rows(self):
        stdout = (
            '0, 00000000:01:00.0, Not Active, Active\n'
            '1, 00000000:02:00.0, Not Active\n'
            'garbage\n'
        )
        with mock.patch.object(ntc.subprocess, 'run',
                               return_value=self.result(stdout)):
            self.assertEqual(ntc._collect_throttle(),
                             {"00000000:01:00.0": (False, True)})

    def test_emit_gpu_metrics_includes_throttle(self):
        gpu = {"index": "0", "pci_bus_id": "00000000:01:00.0",
               "name": "Test GPU", "temp_c": 52.0, "util_gpu": 0.0,
               "util_mem": 0.0, "mem_used_mib": 100.0, "mem_total_mib": 100.0,
               "power_w": 10.5, "hw_slow": False, "sw_slow": True}
        lines = []
        ntc._emit_gpu_metrics(lines, [gpu])
        body = "\n".join(lines)
        self.assertIn(
            'node_gpu_thermal_slowdown{index="0",'
            'pci_bus_id="00000000:01:00.0",name="Test GPU",reason="hw"} 0',
            body)
        self.assertIn(
            'node_gpu_thermal_slowdown{index="0",'
            'pci_bus_id="00000000:01:00.0",name="Test GPU",reason="sw"} 1',
            body)
    def test_parses_gpu_health_fields(self):
        stdout = '0,0000:01:00.0,NVIDIA,45,10,20,1024,2048,300,7,2\n'
        with mock.patch.object(ntc.subprocess, 'run',
                               return_value=self.result(stdout)):
            self.assertEqual(ntc.collect_nvidia(), [{
                'index': '0', 'pci_bus_id': '0000:01:00.0',
                'name': 'NVIDIA', 'temp_c': 45.0,
                'util_gpu': 10.0, 'util_mem': 20.0,
                'mem_used_mib': 1024.0, 'mem_total_mib': 2048.0,
                'power_w': 300.0,
                'ecc_corrected': 7.0, 'ecc_uncorrected': 2.0,
                'hw_slow': False, 'sw_slow': False,
            }])

    def test_preserves_gpu_when_ecc_is_unavailable(self):
        stdout = '0,0000:01:00.0,NVIDIA,45,10,20,1024,2048,300,N/A,N/A\n'
        with mock.patch.object(ntc.subprocess, 'run',
                               return_value=self.result(stdout)):
            gpu = ntc.collect_nvidia()[0]
        self.assertIsNone(gpu['ecc_corrected'])
        self.assertIsNone(gpu['ecc_uncorrected'])


class CollectFlrEventsTests(unittest.TestCase):
    @staticmethod
    def result(stdout='', returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout=stdout,
                                          stderr='')

    def test_counts_matching_journal_lines(self):
        stdout = (
            'pcieport: Function Level Reset for 0000:01:00.0\n'
            'unrelated\n'
            'FLR completed\n'
        )
        with mock.patch.object(ntc.subprocess, 'run',
                               return_value=self.result(stdout)):
            self.assertEqual(ntc.collect_flr_events(), (2, 1))

    def test_failure_reports_probe_not_ok(self):
        with mock.patch.object(ntc.subprocess, 'run',
                               return_value=self.result('bad', returncode=2)):
            self.assertEqual(ntc.collect_flr_events(), (0, 0))

    def test_no_matches_reports_probe_ok(self):
        with mock.patch.object(ntc.subprocess, 'run',
                               return_value=self.result('', returncode=1)):
            self.assertEqual(ntc.collect_flr_events(), (0, 1))


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


class CominoChannelLabelTests(unittest.TestCase):
    def test_fan_mode(self):
        label, fan_n, _pump_n = ntc._comino_channel_label('1', 'FAN', 'CH1', 0, 0)
        self.assertEqual(label, 'fan 1')
        self.assertEqual(fan_n, 1)

    def test_pump_mode(self):
        label, _fan_n, pump_n = ntc._comino_channel_label('1', 'PUMP', 'CH1', 0, 0)
        self.assertEqual(label, 'pump 1')
        self.assertEqual(pump_n, 1)

    def test_off_skipped(self):
        label, _, _ = ntc._comino_channel_label('1', 'OFF', 'CH1', 0, 0)
        self.assertIsNone(label)

    def test_not_installed_skipped(self):
        label, _, _ = ntc._comino_channel_label('0', 'FAN', 'CH1', 0, 0)
        self.assertIsNone(label)

    def test_unknown_mode(self):
        label, _, _ = ntc._comino_channel_label('1', 'XYZ', 'CH1', 0, 0)
        self.assertIn('xyz', label)

    def test_counters_increment_independently(self):
        _, fan_n, _ = ntc._comino_channel_label('1', 'FAN', 'CH1', 0, 0)
        _, _, pump_n = ntc._comino_channel_label('1', 'PUMP', 'CH2', 1, 0)
        self.assertEqual(fan_n, 1)
        self.assertEqual(pump_n, 1)

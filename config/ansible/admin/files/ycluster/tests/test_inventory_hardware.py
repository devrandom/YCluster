"""Tests for the decomposed Linux hardware-fact collectors.

_run is mocked with canned command output, so these exercise the parsing
and filtering logic without requiring real hardware.
"""
import unittest
from unittest import mock

from ycluster.utils import inventory


def run_result(stdout="", returncode=0):
    r = mock.Mock()
    r.stdout = stdout
    r.returncode = returncode
    return r


class CollectCPUTest(unittest.TestCase):
    def test_model_with_cores_threads(self):
        out = "Model name:      AMD EPYC (R) (TM)\nCPU(s): 64\nSocket(s): 2\nCore(s) per socket: 16\n"
        with mock.patch.object(inventory, "_run", return_value=run_result(out)):
            facts = inventory._init_hardware_facts()
            inventory._collect_cpu(facts)
        self.assertEqual(facts["cpu"], "AMD EPYC, 32c/64t")

    def test_empty_lscpu_leaves_cpu_none(self):
        with mock.patch.object(inventory, "_run", return_value=run_result("")):
            facts = inventory._init_hardware_facts()
            inventory._collect_cpu(facts)
        self.assertIsNone(facts["cpu"])


class CollectRAMTest(unittest.TestCase):
    def test_dimm_sum_gb(self):
        out = "\n".join([
            "Size: 32 GB",
            "Size: No Module Installed",
            "Size: 32 GB",
            "Size: 16 GB",
        ])
        with mock.patch.object(inventory, "_run", return_value=run_result(out)):
            facts = inventory._init_hardware_facts()
            inventory._collect_ram(facts)
        # (32+32+16) GB in MB summed then //1024
        self.assertEqual(facts["ram_gb"], 80)

    def test_no_dimms_leaves_none(self):
        with mock.patch.object(inventory, "_run", return_value=run_result("Size: No Module Installed")):
            facts = inventory._init_hardware_facts()
            inventory._collect_ram(facts)
        self.assertIsNone(facts["ram_gb"])


class CollectDMITest(unittest.TestCase):
    def test_serial_product_bios(self):
        sys_out = "Serial Number: ABC123\nProduct Name: ProLiant DL360\n"
        bios_out = "Version: v2.1\n"
        with mock.patch.object(inventory, "_run", side_effect=[run_result(sys_out), run_result(bios_out)]):
            facts = inventory._init_hardware_facts()
            inventory._collect_dmi(facts)
        self.assertEqual(facts["serial"], "ABC123")
        self.assertEqual(facts["product"], "ProLiant DL360")
        self.assertEqual(facts["bios_version"], "v2.1")


class CollectDisksTest(unittest.TestCase):
    def test_filters_and_types(self):
        import json
        data = {
            "blockdevices": [
                {"name": "sda", "rota": "1", "model": "WDC"},
                {"name": "nvme0n1", "rota": "0", "model": "Samsung"},
                {"name": "loop0", "rota": "0"},
                {"name": "sr0", "rota": "0"},
                {"name": "sdb", "rota": "0", "model": "Virtual HDisk"},
            ]
        }
        with mock.patch.object(inventory, "_run", return_value=run_result(json.dumps(data))):
            facts = inventory._init_hardware_facts()
            inventory._collect_disks(facts)
        names = [d["name"] for d in facts["disks"]]
        self.assertEqual(names, ["sda", "nvme0n1"])
        self.assertEqual(facts["disks"][0]["type"], "hdd")
        self.assertEqual(facts["disks"][1]["type"], "nvme")


class CollectNICsTest(unittest.TestCase):
    def test_physical_nics_with_speed(self):
        import json
        links = [
            {"ifname": "lo", "link_type": "loopback"},
            {"ifname": "eth0", "link_type": "ether", "address": "aa:bb"},
            {"ifname": "wg0", "link_type": "ether"},
            {"ifname": "eth1", "link_type": "ether"},
        ]
        ethtool_speed = "Speed: 25000Mb/s\n"
        ethtool_driver = "driver: mlx5_core\n"
        with mock.patch.object(inventory, "_run", side_effect=[
            run_result(json.dumps(links)),
            run_result(ethtool_speed), run_result(ethtool_driver),   # eth0
            run_result(ethtool_speed), run_result(ethtool_driver),   # eth1
        ]):
            facts = inventory._init_hardware_facts()
            inventory._collect_nics(facts)
        names = [n["name"] for n in facts["nics"]]
        self.assertEqual(names, ["eth0", "eth1"])
        self.assertEqual(facts["nics"][0]["speed"], "25000Mb/s")
        self.assertEqual(facts["nics"][0]["driver"], "mlx5_core")

    def test_unknown_speed_not_recorded(self):
        import json
        links = [{"ifname": "eth0", "link_type": "ether", "address": "aa:bb"}]
        with mock.patch.object(inventory, "_run", side_effect=[
            run_result(json.dumps(links)),
            run_result("Speed: Unknown!\n"),
            run_result("driver: tg3\n"),
        ]):
            facts = inventory._init_hardware_facts()
            inventory._collect_nics(facts)
        self.assertEqual(facts["nics"][0]["speed"], None)
        self.assertEqual(facts["nics"][0]["driver"], "tg3")


class CollectGPUsTest(unittest.TestCase):
    def test_lspci_skips_bmc_display(self):
        lspci = '\n'.join([
            '00:01.0 "VGA compatible controller" "ASPEED" "ASPEED GFX"',
            '0000:41:00.0 "3D controller" "NVIDIA" "GA102"',
        ])
        with mock.patch.object(inventory, "_run", return_value=run_result("", returncode=0)):
            # nvidia-smi missing -> exception path; lspci returns entries
            def fake_run(cmd, **kw):
                if cmd[0] == "lspci":
                    return run_result(lspci)
                raise FileNotFoundError(cmd[0])
            with mock.patch.object(inventory, "_run", side_effect=fake_run):
                facts = inventory._init_hardware_facts()
                inventory._collect_gpus(facts)
        models = [g["model"] for g in facts["gpus"]]
        self.assertEqual(models, ["GA102"])

    def test_nvidia_smi_replaces_lspci_entries(self):
        lspci = '0000:41:00.0 "3D controller" "NVIDIA" "GA102"'
        smi_out = "NVIDIA A100, 40960, 550.54\n"
        def fake_run(cmd, **kw):
            if cmd[0] == "lspci":
                return run_result(lspci)
            return run_result(smi_out, returncode=0)
        with mock.patch.object(inventory, "_run", side_effect=fake_run):
            facts = inventory._init_hardware_facts()
            inventory._collect_gpus(facts)
        self.assertEqual(len(facts["gpus"]), 1)
        self.assertEqual(facts["gpus"][0]["model"], "NVIDIA A100")
        self.assertEqual(facts["gpus"][0]["vram_mb"], 40960)


class CollectCPUDarwinTest(unittest.TestCase):
    def test_brand_with_cores(self):
        def fake_run(cmd, **kw):
            arg = cmd[-1]
            return run_result({
                "machdep.cpu.brand_string": "Intel Xeon",
                "hw.physicalcpu": "8",
                "hw.logicalcpu": "16",
            }.get(arg, ""))
        with mock.patch.object(inventory, "_run", side_effect=fake_run):
            facts = inventory._init_hardware_facts()
            inventory._collect_cpu_darwin(facts)
        self.assertEqual(facts["cpu"], "Intel Xeon, 8c/16t")

    def test_apple_silicon_fallback_uses_product(self):
        def fake_run(cmd, **kw):
            arg = cmd[-1]
            return run_result({
                "machdep.cpu.brand_string": "",   # Intel brand absent on AS
                "hw.physicalcpu": "10",
                "hw.logicalcpu": "10",
            }.get(arg, ""))
        with mock.patch.object(inventory, "_run", side_effect=fake_run):
            facts = inventory._init_hardware_facts()
            facts["product"] = "Mac mini"
            inventory._collect_cpu_darwin(facts)
        self.assertEqual(facts["cpu"], "Mac mini, 10c/10t")


class CollectDisksDarwinTest(unittest.TestCase):
    def test_partitions_dedup_and_types(self):
        import json
        data = {"SPStorageDataType": [
            {"bsd_name": "disk0s0", "com.apple.diskmanagement.sizeondisk": 512 * 1024 ** 3,
             "physical_interconnect": "PCI-Express", "device_model": "APPLE SSD"},
            {"bsd_name": "disk0s1", "com.apple.diskmanagement.sizeondisk": 512 * 1024 ** 3,
             "physical_interconnect": "PCI-Express"},
            {"bsd_name": "disk1s0", "com.apple.diskmanagement.sizeondisk": 0,
             "physical_interconnect": "SATA"},
        ]}
        with mock.patch.object(inventory, "_run", return_value=run_result(json.dumps(data))):
            facts = inventory._init_hardware_facts()
            inventory._collect_disks_darwin(facts)
        # bsd_name normalization ("disk0s0"->"disk") dedups partitions
        # of the same physical disk. NB: the medium check looks for the
        # substring "pcie"/"nvme"/"flash"; "PCI-Express" matches none,
        # so classification falls through to "hdd" (pre-existing quirk,
        # preserved here).
        self.assertEqual(len(facts["disks"]), 1)
        self.assertEqual(facts["disks"][0]["name"], "disk")
        self.assertEqual(facts["disks"][0]["type"], "hdd")
        self.assertEqual(facts["disks"][0]["size"], "512G")


class CollectNICsDarwinTest(unittest.TestCase):
    def test_filters_wireless_and_bridge(self):
        import json
        data = {"SPNetworkDataType": [
            {"interface": "en0", "spnetwork_hardware_address": "aa:bb",
             "spnetwork_interface_type": "Ethernet"},
            {"interface": "en1", "spnetwork_interface_type": "IEEE80211"},
            {"interface": "bridge0", "spnetwork_interface_type": "Bridge"},
        ]}
        with mock.patch.object(inventory, "_run", return_value=run_result(json.dumps(data))):
            facts = inventory._init_hardware_facts()
            inventory._collect_nics_darwin(facts)
        self.assertEqual([n["name"] for n in facts["nics"]], ["en0"])
        self.assertEqual(facts["nics"][0]["mac"], "aa:bb")


if __name__ == "__main__":
    unittest.main()

class CollectBMCMacTest(unittest.TestCase):
    def test_returns_first_channel_mac(self):
        with mock.patch.object(inventory, "_load_ipmi_modules"), \
             mock.patch.object(inventory, "_run", side_effect=[
                 run_result("MAC Address  : aa:bb"),
                 run_result("MAC Address  : cc:dd"),
             ]):
            self.assertEqual(inventory._collect_bmc_mac_linux(), "aa:bb")

    def test_failure_returns_none(self):
        with mock.patch.object(inventory, "_load_ipmi_modules"), \
             mock.patch.object(inventory, "_run", side_effect=RuntimeError("boom")):
            self.assertIsNone(inventory._collect_bmc_mac_linux())

class DarwinGpuEntryTest(unittest.TestCase):
    def test_normalizes_apple_gpu(self):
        entry = inventory._gpu_darwin_entry({'sppci_model': 'Apple M4', 'sppci_vram': '16 GB'})
        self.assertEqual(entry, {'vendor': 'Apple', 'model': 'Apple M4', 'vram': '16 GB'})

    def test_requires_name(self):
        self.assertIsNone(inventory._gpu_darwin_entry({}))

class HardwareOverviewTest(unittest.TestCase):
    def test_applies_macos_fields(self):
        facts = inventory._init_hardware_facts()
        inventory._apply_hardware_overview(facts, {
            'machine_name': 'Mac mini',
            'serial_number': 'serial',
            'platform_UUID': 'uuid',
            'SMC_version_system': 'smc',
            'physical_memory': '64 GB',
        })
        self.assertEqual(facts['product'], 'Mac mini')
        self.assertEqual(facts['serial'], 'serial')
        self.assertEqual(facts['bios_version'], 'smc')
        self.assertEqual(facts['ram_gb'], 64)

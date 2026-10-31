"""Tests for VM data block-image sizing and device switching."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ycluster.utils import vm_data_block as vdb
from ycluster.utils import vm_manager as vm


def _proc(cmd, returncode=0, stdout=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, "")


def _happy_run(cmd, *args, **kwargs):
    if cmd[0] == "du":
        return _proc(cmd, stdout="1 /mnt/vm\n")
    if cmd[0] == "losetup" and "--find" in cmd:
        return _proc(cmd, stdout="/dev/loop0\n")
    return _proc(cmd)


class VMDataBlockTests(unittest.TestCase):
    def setUp(self):
        # Default: NAS reachable, no legacy data (fresh-VM shape). Tests
        # exercising legacy shares override _legacy_share_has_data.
        for target, value in (
                ("_nas_reachable", True),
                ("_legacy_share_has_data", False)):
            patcher = mock.patch.object(vdb, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Fresh-image creation is mocked so orchestration tests don't touch
        # the filesystem; its internals are covered by create/mkfs mocks.
        patcher = mock.patch.object(vdb, "create_data_image")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_migration_size_uses_allocated_with_200gib_floor(self):
        self.assertEqual(vdb.migration_size_mib(0),
                         vdb.VM_DATA_MIN_BYTES // (1024 * 1024))
        self.assertEqual(vdb.migration_size_mib(100 * 1024), 200 * 1024)
        # 150 GiB allocated * 1.5 = 225 GiB.
        self.assertEqual(vdb.migration_size_mib(150 * 1024), 225 * 1024)

    def test_disk_size_parsing(self):
        self.assertEqual(vdb.parse_disk_size("200GiB"), 200 * 1024**3)
        self.assertEqual(vdb.parse_disk_size("2TiB"), 2 * 1024**4)
        # Decimal-style aliases must not crash: parse binary-consistent.
        self.assertEqual(vdb.parse_disk_size("200GB"), 200 * 1024**3)
        self.assertEqual(vdb.parse_disk_size("1MB"), 1024**2)
        self.assertEqual(vdb.parse_disk_size("1TB"), 1024**4)
        self.assertEqual(vdb.parse_disk_size("512B"), 512)
        with self.assertRaises(ValueError):
            vdb.parse_disk_size("not-a-size")

    def test_grow_data_image_refuses_to_shrink(self):
        with mock.patch.object(vdb.subprocess, "run") as run:
            run.return_value = _proc(["qemu-img"],
                                     stdout='{"virtual-size": 300}')
            with self.assertRaises(ValueError):
                vdb.grow_data_image("img", 100)
        self.assertEqual(run.call_args.args[0][:2], ["qemu-img", "info"])

    def test_grow_data_image_resizes_only_when_growing(self):
        def run(cmd, *args, **kwargs):
            if cmd[1] == "info":
                return _proc(cmd, stdout='{"virtual-size": 300}')
            return _proc(cmd)

        with mock.patch.object(vdb.subprocess, "run",
                               side_effect=run) as run_mock:
            vdb.grow_data_image("img", 300)
            self.assertNotIn(
                "resize", [c.args[0][1] for c in run_mock.call_args_list])
            vdb.grow_data_image("img", 500)
        self.assertIn(["qemu-img", "resize", "img", "500"],
                      [c.args[0] for c in run_mock.call_args_list])

    def test_grow_data_image_and_fs_order_and_cleanup(self):
        calls = []

        def run(cmd, *args, **kwargs):
            calls.append(cmd)
            if cmd[1] == "info":
                return _proc(cmd, stdout='{"virtual-size": 300}')
            if cmd[0] == "losetup" and "--find" in cmd:
                return _proc(cmd, stdout="/dev/loop7\n")
            return _proc(cmd)

        with mock.patch.object(vdb.subprocess, "run", side_effect=run):
            vdb.grow_data_image_and_fs("img", 500)
        self.assertEqual(calls, [
            ["qemu-img", "info", "--output=json", "img"],
            ["qemu-img", "resize", "img", "500"],
            ["losetup", "--find", "--show", "img"],
            ["e2fsck", "-fp", "/dev/loop7"],
            ["resize2fs", "/dev/loop7"],
            ["losetup", "-d", "/dev/loop7"],
        ])

    def test_grow_data_image_and_fs_detaches_loop_on_failure(self):
        def run(cmd, *args, **kwargs):
            if cmd[1] == "info":
                return _proc(cmd, stdout='{"virtual-size": 300}')
            if cmd[0] == "losetup" and "--find" in cmd:
                return _proc(cmd, stdout="/dev/loop7\n")
            if cmd[0] == "e2fsck":
                raise subprocess.CalledProcessError(1, cmd)
            return _proc(cmd)

        with mock.patch.object(vdb.subprocess, "run",
                               side_effect=run) as run_mock:
            with self.assertRaises(subprocess.CalledProcessError):
                vdb.grow_data_image_and_fs("img", 500)
        self.assertIn(["losetup", "-d", "/dev/loop7"],
                      [c.args[0] for c in run_mock.call_args_list])

    def test_existing_image_attaches_block_device(self):
        with (
            mock.patch.object(vm, "_host_config_value", return_value=None),
            mock.patch.object(vdb.subprocess, "run"),
            mock.patch.object(vm, "_instance_info",
                              return_value={"type": "virtual-machine"}),
            mock.patch.object(vdb.os.path, "exists", return_value=True),
            mock.patch.object(vdb, "LOCK_DIR", tempfile.mkdtemp()),
            mock.patch.object(vdb, "replace_data_device") as replace,
        ):
            vm._ensure_nas_share("vm1")

        replace.assert_called_once_with(
            vm._incus, "vm1", f"{vdb.NAS_NFS_MOUNT}/vm1.img",
            "io.cache=none", "io.bus=virtio-blk", "required=true")

    def test_migration_success_renames_verified_image(self):
        image = f"{vdb.NAS_NFS_MOUNT}/vm1.img"
        tmp = f"{image}.tmp"
        with (
            mock.patch.object(vdb.subprocess, "run",
                              side_effect=_happy_run) as run,
            mock.patch.object(vdb.os.path, "isdir", return_value=True),
            mock.patch.object(vdb.os.path, "exists", return_value=False),
            mock.patch.object(vdb.os, "statvfs") as statvfs,
            mock.patch.object(vdb.tempfile, "mkdtemp",
                              return_value="/mnt/vm1-data") as mkdtemp,
            mock.patch.object(vdb.os, "replace") as replace,
            mock.patch.object(vdb.os, "rmdir") as rmdir,
        ):
            statvfs.return_value = mock.Mock(f_bavail=300, f_frsize=1024**3)
            with (
                mock.patch.object(vdb, "_regular_file_stats",
                                  return_value=(2, 30)),
            ):
                vdb.migrate_vm_data_to_image("vm1", image, is_running=lambda: False)

        commands = [tuple(call.args[0]) for call in run.call_args_list]
        self.assertIn(("qemu-img", "create", "-f", "raw", tmp, "204800M"),
                      commands)
        self.assertIn(("rsync", "-aHAXS", "--info=progress2",
                       f"{vdb.NAS_NFS_MOUNT}/vm1/", "/mnt/vm1-data/"),
                      commands)
        replace.assert_called_once_with(tmp, image)

    def test_migration_aborts_if_vm_starts_before_commit(self):
        image = f"{vdb.NAS_NFS_MOUNT}/vm1.img"
        running = iter([False, True])
        with (
            mock.patch.object(vdb.subprocess, "run",
                              side_effect=_happy_run),
            mock.patch.object(vdb.os.path, "isdir", return_value=True),
            mock.patch.object(vdb.os.path, "exists", return_value=False),
            mock.patch.object(vdb.os, "statvfs",
                              return_value=mock.Mock(f_bavail=300,
                                                     f_frsize=1024**3)),
            mock.patch.object(vdb.tempfile, "mkdtemp",
                              return_value="/mnt/vm1-data"),
            mock.patch.object(vdb.os, "replace") as replace,
            mock.patch.object(vdb.os, "rmdir"),
            mock.patch.object(vdb, "_regular_file_stats",
                              return_value=(2, 30)),
        ):
            with self.assertRaises(RuntimeError):
                vdb.migrate_vm_data_to_image(
                    "vm1", image, is_running=lambda: next(running))

        replace.assert_not_called()

    def test_failed_copy_cleans_up_temporary_image(self):
        image = f"{vdb.NAS_NFS_MOUNT}/vm1.img"
        tmp = f"{image}.tmp"

        def run(cmd, *args, **kwargs):
            if cmd[0] == "du":
                return _proc(cmd, stdout="1 /mnt/vm\n")
            if cmd[0] == "rsync":
                raise subprocess.CalledProcessError(1, cmd)
            if cmd[0] == "losetup" and "--find" in cmd:
                return _proc(cmd, stdout="/dev/loop0\n")
            return _proc(cmd)

        with (
            mock.patch.object(vdb.subprocess, "run", side_effect=run),
            mock.patch.object(vdb.os.path, "isdir", return_value=True),
            mock.patch.object(vdb.os.path, "exists",
                              side_effect=lambda path: path == tmp),
            mock.patch.object(vdb.os, "statvfs") as statvfs,
            mock.patch.object(vdb.tempfile, "mkdtemp",
                              return_value="/mnt/vm1-data"),
            mock.patch.object(vdb.os, "replace") as replace,
            mock.patch.object(vdb.os, "unlink") as unlink,
            mock.patch.object(vdb.os, "rmdir"),
        ):
            statvfs.return_value = mock.Mock(f_bavail=300, f_frsize=1024**3)
            with self.assertRaises(subprocess.CalledProcessError):
                vdb.migrate_vm_data_to_image(
                    "vm1", image, is_running=lambda: False)

        replace.assert_not_called()
        self.assertEqual(unlink.call_args_list,
                         [mock.call(tmp), mock.call(tmp)])

    def test_regular_file_stats_counts_and_sums(self):
        with mock.patch.object(vdb.subprocess, "run") as run:
            run.return_value = _proc(["find"], stdout="10\n5\n")
            self.assertEqual(vdb._regular_file_stats("src"), (2, 15))

    def test_migration_preflight_rejections(self):
        image = f"{vdb.NAS_NFS_MOUNT}/vm1.img"
        with mock.patch.object(vdb.subprocess, "run") as run:
            with self.assertRaises(RuntimeError):
                vdb.migrate_vm_data_to_image(
                    "vm1", image, is_running=lambda: True)
            run.assert_not_called()

        with mock.patch.object(vdb.subprocess, "run",
                               return_value=_proc(["mountpoint"], returncode=1)):
            with self.assertRaises(RuntimeError):
                vdb.migrate_vm_data_to_image(
                    "vm1", image, is_running=lambda: False)

        with (
            mock.patch.object(vdb.subprocess, "run",
                              return_value=_proc(["mountpoint"])),
            mock.patch.object(vdb.os.path, "isdir", return_value=False),
        ):
            with self.assertRaises(RuntimeError):
                vdb.migrate_vm_data_to_image(
                    "vm1", image, is_running=lambda: False)

        with (
            mock.patch.object(vdb.subprocess, "run",
                              return_value=_proc(["mountpoint"])),
            mock.patch.object(vdb.os.path, "isdir", return_value=True),
            mock.patch.object(vdb.os.path, "exists", return_value=True),
        ):
            with self.assertRaises(RuntimeError):
                vdb.migrate_vm_data_to_image(
                    "vm1", image, is_running=lambda: False)

        with (
            mock.patch.object(vdb.subprocess, "run",
                              side_effect=_happy_run),
            mock.patch.object(vdb.os.path, "isdir", return_value=True),
            mock.patch.object(vdb.os.path, "exists", return_value=False),
            mock.patch.object(vdb.os, "statvfs",
                              return_value=mock.Mock(
                                  f_bavail=1, f_frsize=1024**3)),
        ):
            with self.assertRaises(RuntimeError):
                vdb.migrate_vm_data_to_image(
                    "vm1", image, is_running=lambda: False)

    def test_migration_verification_failure_cleans_up(self):
        image = f"{vdb.NAS_NFS_MOUNT}/vm1.img"
        tmp = f"{image}.tmp"
        with (
            mock.patch.object(vdb.subprocess, "run",
                              side_effect=_happy_run) as run,
            mock.patch.object(vdb.os.path, "isdir", return_value=True),
            mock.patch.object(vdb.os.path, "exists",
                              side_effect=[False, False, True]),
            mock.patch.object(vdb.os, "statvfs") as statvfs,
            mock.patch.object(vdb.tempfile, "mkdtemp",
                              return_value="/mnt/vm1-data"),
            mock.patch.object(vdb.os, "replace") as replace,
            mock.patch.object(vdb.os, "unlink") as unlink,
            mock.patch.object(vdb.os, "rmdir"),
        ):
            statvfs.return_value = mock.Mock(f_bavail=300, f_frsize=1024**3)
            with (
                mock.patch.object(vdb, "_regular_file_stats",
                                  side_effect=[(1, 10), (2, 20)]),
            ):
                with self.assertRaises(RuntimeError):
                    vdb.migrate_vm_data_to_image(
                        "vm1", image, is_running=lambda: False)

        replace.assert_not_called()
        unlink.assert_called_once_with(tmp)

    def test_migration_failure_falls_back_to_directory_share(self):
        with (
            mock.patch.object(vm, "_host_config_value", return_value=None),
            mock.patch.object(vdb.subprocess, "run"),
            mock.patch.object(vm, "_instance_info",
                              return_value={"type": "virtual-machine"}),
            mock.patch.object(vm, "_instance_running", return_value=False),
            mock.patch.object(vdb.os.path, "exists", return_value=False),
            mock.patch.object(vdb.os.path, "isdir", return_value=True),
            mock.patch.object(vdb, "_legacy_share_has_data",
                              return_value=True),
            mock.patch.object(vdb, "LOCK_DIR", tempfile.mkdtemp()),
            mock.patch.object(vdb, "migrate_vm_data_to_image",
                              side_effect=RuntimeError("no space")),
            mock.patch.object(vdb, "replace_data_device") as replace,
        ):
            vm._ensure_nas_share("vm1", migrate=True)

        replace.assert_called_once_with(
            vm._incus, "vm1", f"{vdb.NAS_HOST_BASE}/vm1", "required=false",
            f"path={vdb.VM_DATA_GUEST_PATH}")

    def test_migration_failure_leaves_running_vm_untouched(self):
        with (
            mock.patch.object(vm, "_host_config_value", return_value=None),
            mock.patch.object(vdb.subprocess, "run"),
            mock.patch.object(vm, "_instance_info",
                              return_value={"type": "virtual-machine"}),
            mock.patch.object(vm, "_instance_running", return_value=True),
            mock.patch.object(vdb.os.path, "exists", return_value=False),
            mock.patch.object(vdb.os.path, "isdir", return_value=True),
            mock.patch.object(vdb, "_legacy_share_has_data",
                              return_value=True),
            mock.patch.object(vdb, "LOCK_DIR", tempfile.mkdtemp()),
            mock.patch.object(vdb, "migrate_vm_data_to_image",
                              side_effect=RuntimeError("no space")),
            mock.patch.object(vdb, "replace_data_device") as replace,
        ):
            vm._ensure_nas_share("vm1", migrate=True)

        replace.assert_not_called()

    def test_no_migrate_attaches_legacy_share_without_migrating(self):
        with (
            mock.patch.object(vm, "_host_config_value", return_value=None),
            mock.patch.object(vdb.subprocess, "run"),
            mock.patch.object(vm, "_instance_info",
                              return_value={"type": "virtual-machine"}),
            mock.patch.object(vdb.os.path, "exists", return_value=False),
            mock.patch.object(vdb, "_legacy_share_has_data",
                              return_value=True),
            mock.patch.object(vdb, "LOCK_DIR", tempfile.mkdtemp()),
            mock.patch.object(vdb, "migrate_vm_data_to_image") as migrate,
            mock.patch.object(vdb, "replace_data_device") as replace,
        ):
            self.assertFalse(vm._ensure_nas_share("vm1"))

        migrate.assert_not_called()
        replace.assert_called_once_with(
            vm._incus, "vm1", f"{vdb.NAS_HOST_BASE}/vm1", "required=false",
            f"path={vdb.VM_DATA_GUEST_PATH}")

    def test_fresh_image_creation_failure_attaches_nothing(self):
        with (
            mock.patch.object(vm, "_host_config_value", return_value=None),
            mock.patch.object(vdb.subprocess, "run"),
            mock.patch.object(vm, "_instance_info",
                              return_value={"type": "virtual-machine"}),
            mock.patch.object(vm, "_instance_running", return_value=False),
            mock.patch.object(vdb.os.path, "exists", return_value=False),
            mock.patch.object(vdb.os.path, "isdir", return_value=False),
            mock.patch.object(vdb, "LOCK_DIR", tempfile.mkdtemp()),
            mock.patch.object(vdb, "create_data_image",
                              side_effect=RuntimeError("no space")),
            mock.patch.object(vdb, "replace_data_device") as replace,
        ):
            self.assertFalse(vm._ensure_nas_share("vm1", migrate=True))

        replace.assert_not_called()

    def test_committed_image_attach_failure_fails_start(self):
        committed = {"image": False}

        def exists(path):
            return committed["image"]

        with (
            mock.patch.object(vm, "_host_config_value", return_value=None),
            mock.patch.object(vdb.subprocess, "run"),
            mock.patch.object(vm, "_instance_info",
                              return_value={"type": "virtual-machine"}),
            mock.patch.object(vdb.os.path, "exists", side_effect=exists),
            mock.patch.object(vdb, "_legacy_share_has_data",
                              return_value=True),
            mock.patch.object(vdb, "LOCK_DIR", tempfile.mkdtemp()),
            mock.patch.object(vdb, "migrate_vm_data_to_image",
                              side_effect=lambda *a, **k:
                                  committed.__setitem__("image", True)),
            mock.patch.object(vdb, "replace_data_device",
                              side_effect=RuntimeError("attach failed")),
        ):
            with self.assertRaises(RuntimeError):
                vm._ensure_nas_share("vm1", migrate=True)

    def test_resize_refuses_legacy_attached_vm(self):
        image = f"{vdb.NAS_NFS_MOUNT}/vm1.img"
        with (
            mock.patch.object(vdb.os.path, "exists", return_value=True),
            mock.patch.object(vdb.subprocess, "run") as run,
        ):
            run.return_value = _proc(["incus"], returncode=1, stdout="")
            with self.assertRaises(ValueError):
                vdb.assert_resizable(
                    lambda *a, **k: run.return_value, "vm1", image)
            # Image attached: passes.
            run.return_value = _proc(["incus"], stdout=f"{image}\n")
            vdb.assert_resizable(
                lambda *a, **k: run.return_value, "vm1", image)

    def test_replace_data_device_swaps_sources(self):
        calls = []

        def make_incus(current):
            def incus(*args, **kwargs):
                calls.append(args)
                get = args[:4] == ("config", "device", "get", "vm1")
                if get:
                    missing = current is None
                    return _proc(args, 1 if missing else 0,
                                 "" if missing else f"{current}\n")
                return _proc(args)
            return incus

        # Same source: no change.
        vdb.replace_data_device(make_incus("old-src"), "vm1", "old-src")
        self.assertEqual(calls, [("config", "device", "get", "vm1",
                                  "data", "source")])
        # Different source: remove then add.
        calls.clear()
        vdb.replace_data_device(make_incus("old-src"), "vm1", "new-src",
                                "required=true", "path=/data")
        self.assertIn(("config", "device", "remove", "vm1", "data"), calls)
        self.assertIn(("config", "device", "add", "vm1", "data", "disk",
                       "source=new-src", "required=true", "path=/data"),
                      calls)
        # Not attached: add only.
        calls.clear()
        vdb.replace_data_device(make_incus(None), "vm1", "new-src")
        self.assertNotIn(("config", "device", "remove", "vm1", "data"), calls)
        self.assertIn(("config", "device", "add", "vm1", "data", "disk",
                       "source=new-src"), calls)

    def test_guest_mount_only_for_image_device(self):
        image = f"{vdb.NAS_NFS_MOUNT}/vm1.img"
        incus = mock.Mock()
        # Device source != image: no-op.
        incus.return_value = _proc(["incus"], returncode=1, stdout="")
        vdb.ensure_guest_mount(incus, "vm1")
        self.assertEqual(incus.call_count, 1)
        # Device source == image: fstab + mount script runs in the guest.
        incus.reset_mock()
        incus.return_value = _proc(["incus"], stdout=f"{image}\n")
        vdb.ensure_guest_mount(incus, "vm1")
        self.assertEqual(incus.call_args.args[:2], ("exec", "vm1"))
        script = incus.call_args.args[5]
        self.assertIn("set -e", script)
        self.assertIn("mount LABEL=vmdata /data", script)
        # fstab append comes after the mount (mount-first ordering).
        self.assertLess(script.index("mount LABEL=vmdata"),
                        script.index(">> /etc/fstab"))

    def test_disabled_share_attaches_nothing(self):
        with (
            mock.patch.object(vm, "_host_config_value", return_value="false"),
            mock.patch.object(vdb.subprocess, "run") as run,
            mock.patch.object(vdb, "replace_data_device") as replace,
        ):
            vm._ensure_nas_share("vm1")

        run.assert_not_called()
        replace.assert_not_called()

    def test_unreachable_nas_skips_attach(self):
        with (
            mock.patch.object(vm, "_host_config_value", return_value=None),
            mock.patch.object(vdb, "_nas_reachable", return_value=False),
            mock.patch.object(vdb, "replace_data_device") as replace,
            mock.patch.object(vdb, "migrate_vm_data_to_image") as migrate,
        ):
            vm._ensure_nas_share("vm1")

        migrate.assert_not_called()
        replace.assert_not_called()

    def test_container_gets_directory_share(self):
        with (
            mock.patch.object(vm, "_host_config_value", return_value=None),
            mock.patch.object(vdb.subprocess, "run"),
            mock.patch.object(vm, "_instance_info",
                              return_value={"type": "container"}),
            mock.patch.object(vdb, "LOCK_DIR", tempfile.mkdtemp()),
            mock.patch.object(vdb, "replace_data_device") as replace,
        ):
            vm._ensure_nas_share("vm1")

        replace.assert_called_once_with(
            vm._incus, "vm1", f"{vdb.NAS_HOST_BASE}/vm1", "required=false",
            f"path={vdb.VM_DATA_GUEST_PATH}")

    def test_guest_mount_after_boot_retries_transient_failure(self):
        calls = {"mount": 0}
        clock = {"now": 0.0}

        def fake_ensure(incus, name):
            calls["mount"] += 1
            if calls["mount"] < 3:
                raise RuntimeError("agent not ready")
        with (
            mock.patch.object(vdb.time, "sleep") as sleep,
            mock.patch.object(vdb.time, "time",
                              side_effect=lambda: clock["now"]),
            mock.patch.object(vdb, "ensure_guest_mount",
                              side_effect=fake_ensure),
        ):
            # Advance the fake clock on each sleep call.
            def fake_sleep(_):
                clock["now"] += 5
            sleep.side_effect = fake_sleep
            vdb.guest_mount_after_boot(
                None, "vm1",
                wait_agent=lambda name, timeout: None,
                agent_timeout=10)
        self.assertEqual(calls["mount"], 3)

    def test_guest_mount_after_boot_warns_after_deadline(self):
        clock = {"now": 0.0}

        def fake_sleep(_):
            clock["now"] += 5
        with (
            mock.patch.object(vdb.time, "sleep", side_effect=fake_sleep),
            mock.patch.object(vdb.time, "time",
                              side_effect=lambda: clock["now"]),
            mock.patch.object(vdb, "ensure_guest_mount",
                              side_effect=RuntimeError("no agent")),
        ):
            # Must not raise — the warning path is the contract.
            vdb.guest_mount_after_boot(
                None, "vm1",
                wait_agent=lambda name, timeout: None,
                agent_timeout=10)


class LegacyShareProbeTests(unittest.TestCase):
    def test_start_failure_is_treated_as_no_data(self):
        with mock.patch.object(vdb.subprocess, "Popen",
                               side_effect=OSError("gone")):
            self.assertFalse(vdb._legacy_share_has_data("/mnt/vm"))

    def test_timeout_is_treated_as_no_data(self):
        proc = mock.MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired("ls", 10)
        with mock.patch.object(vdb.subprocess, "Popen", return_value=proc):
            self.assertFalse(vdb._legacy_share_has_data("/mnt/vm"))

    def test_nonempty_directory_has_data(self):
        proc = mock.MagicMock(returncode=0)
        proc.communicate.return_value = ("file\n", "")
        with mock.patch.object(vdb.subprocess, "Popen", return_value=proc):
            self.assertTrue(vdb._legacy_share_has_data("/mnt/vm"))

    def test_empty_or_failed_listing_has_no_data(self):
        proc = mock.MagicMock(returncode=0)
        proc.communicate.return_value = ("\n", "")
        with mock.patch.object(vdb.subprocess, "Popen", return_value=proc):
            self.assertFalse(vdb._legacy_share_has_data("/mnt/vm"))
        proc = mock.MagicMock(returncode=2)
        proc.communicate.return_value = ("", "")
        with mock.patch.object(vdb.subprocess, "Popen", return_value=proc):
            self.assertFalse(vdb._legacy_share_has_data("/mnt/vm"))


class CreateDataImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.image = str(Path(self.tmp.name) / "data.img")

    def _statvfs(self, free_bytes):
        stats = mock.Mock()
        stats.f_bavail = free_bytes // 4096
        stats.f_frsize = 4096
        return stats

    def test_refuses_when_nas_is_full(self):
        with mock.patch.object(vdb.os, "statvfs",
                               return_value=self._statvfs(1024)):
            with self.assertRaises(RuntimeError):
                vdb.create_data_image("vm1", self.image)

    def test_creates_then_renames_image(self):
        with mock.patch.object(vdb.os, "statvfs",
                               return_value=self._statvfs(10**12)), \
                mock.patch.object(vdb.subprocess, "run") as run, \
                mock.patch.object(vdb.os, "replace") as replace, \
                mock.patch("builtins.print"):
            vdb.create_data_image("vm1", self.image)
        self.assertEqual(replace.call_args.args[1], self.image)
        self.assertEqual([c.args[0][0] for c in run.call_args_list],
                         ["qemu-img", "mkfs.ext4"])

    def test_removes_stale_tmp_and_tmp_after_failure(self):
        tmp = self.image + ".tmp"
        Path(tmp).write_text("stale")
        with mock.patch.object(vdb.os, "statvfs",
                               return_value=self._statvfs(10**12)), \
                mock.patch.object(vdb.subprocess, "run",
                                  side_effect=subprocess.CalledProcessError(
                                      1, "mkfs.ext4")):
            with self.assertRaises(subprocess.CalledProcessError):
                vdb.create_data_image("vm1", self.image)
        self.assertFalse(Path(tmp).exists())

    def test_cleanup_failure_is_ignored(self):
        tmp = self.image + ".tmp"
        Path(tmp).write_text("stale")
        with mock.patch.object(vdb.os, "statvfs",
                               return_value=self._statvfs(10**12)), \
                mock.patch.object(vdb.subprocess, "run",
                                  side_effect=OSError("boom")), \
                mock.patch.object(vdb.os, "unlink",
                                  side_effect=OSError("nope")):
            with self.assertRaises(OSError):
                vdb.create_data_image("vm1", self.image)


class NasReachableTests(unittest.TestCase):
    def test_probe_start_failure(self):
        with mock.patch.object(vdb.subprocess, "Popen",
                               side_effect=OSError("gone")), \
                mock.patch("builtins.print"):
            self.assertFalse(vdb._nas_reachable())

    def test_probe_timeout(self):
        proc = mock.MagicMock()
        proc.wait.side_effect = subprocess.TimeoutExpired("ls", 10)
        with mock.patch.object(vdb.subprocess, "Popen", return_value=proc), \
                mock.patch("builtins.print"):
            self.assertFalse(vdb._nas_reachable())

    def test_probe_failure_status(self):
        proc = mock.MagicMock()
        proc.wait.return_value = 2
        with mock.patch.object(vdb.subprocess, "Popen", return_value=proc), \
                mock.patch("builtins.print"):
            self.assertFalse(vdb._nas_reachable())

    def test_probe_success(self):
        proc = mock.MagicMock()
        proc.wait.return_value = 0
        with mock.patch.object(vdb.subprocess, "Popen", return_value=proc):
            self.assertTrue(vdb._nas_reachable())


class ResizeAndAttachTests(unittest.TestCase):
    def test_resize_requires_existing_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                vdb.assert_resizable(mock.Mock(), "vm1",
                                     str(Path(tmp) / "absent.img"))

    def test_container_without_nas_reports_failure(self):
        incus = mock.Mock()
        with mock.patch.object(vdb.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(
                                   1, "mkdir")), \
                mock.patch("builtins.print"):
            self.assertFalse(vdb.ensure_nas_share(
                "c1", incus, share_enabled=True,
                instance_info=lambda name: {"type": "container"},
                instance_running=lambda name: False))

    def test_fresh_vm_creates_image_before_attach(self):
        incus = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            image = str(Path(tmp) / "vm1.img")
            with mock.patch.object(vdb, "_nas_reachable", return_value=True), \
                    mock.patch.object(vdb, "data_image_path",
                                      return_value=image), \
                    mock.patch.object(vdb, "data_migration_lock"), \
                    mock.patch.object(vdb, "_legacy_share_has_data",
                                      return_value=False), \
                    mock.patch.object(vdb, "create_data_image") as create, \
                    mock.patch.object(vdb, "replace_data_device"), \
                    mock.patch("builtins.print"):
                self.assertFalse(vdb.ensure_nas_share(
                    "vm1", incus, share_enabled=True,
                    instance_info=lambda name: {"type": "virtual-machine"},
                    instance_running=lambda name: False))
            create.assert_called_once_with("vm1", image)


if __name__ == "__main__":
    unittest.main()

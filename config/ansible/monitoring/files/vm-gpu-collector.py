#!/usr/bin/env python3
# Emit GPU metrics for VFIO-passthrough GPUs by execing nvidia-smi
# inside running Incus VMs. On nvX hosts the GPU(s) are bound to
# vfio-pci on the host, so this is the only way to see utilisation,
# memory, and power — IPMI gives us temperature but nothing else.
#
# Discovery is dynamic: we list running incus VMs and try each one.
# A hung guest can't block collection of the others because exec is
# capped by a per-VM timeout.
#
# Output (atomic write to /var/lib/prometheus/node-exporter/vm_gpu.prom):
#   node_vm_gpu_utilization_percent{vm,index,pci_bus_id,name}
#   node_vm_gpu_memory_utilization_percent{...}
#   node_vm_gpu_memory_used_bytes{...}
#   node_vm_gpu_memory_total_bytes{...}
#   node_vm_gpu_temperature_celsius{...}
#   node_vm_gpu_power_watts{...}
import json
import os
import subprocess
import sys
import tempfile

OUT = "/var/lib/prometheus/node-exporter/vm_gpu.prom"
EXEC_TIMEOUT = 5  # seconds per VM
LIST_TIMEOUT = 5

NVIDIA_SMI_QUERY = (
    "index,pci.bus_id,name,"
    "utilization.gpu,utilization.memory,"
    "memory.used,memory.total,"
    "temperature.gpu,power.draw"
)


def list_running_vms():
    """Return list of incus VM names in RUNNING state."""
    try:
        r = subprocess.run(
            ["incus", "list", "--format=json", "type=virtual-machine"],
            capture_output=True, text=True, timeout=LIST_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"# incus list failed: {e}", file=sys.stderr)
        return []
    if r.returncode != 0:
        print(f"# incus list rc={r.returncode}: {r.stderr.strip()}", file=sys.stderr)
        return []
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        print(f"# incus list JSON parse: {e}", file=sys.stderr)
        return []
    return [v["name"] for v in data if v.get("status") == "Running"]


def query_vm(vm):
    """Return list of GPU dicts from a VM, or [] if none / unreachable."""
    cmd = [
        "incus", "exec", vm, "--",
        "nvidia-smi",
        f"--query-gpu={NVIDIA_SMI_QUERY}",
        "--format=csv,noheader,nounits",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=EXEC_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"# {vm}: nvidia-smi timed out", file=sys.stderr)
        return []
    except FileNotFoundError as e:
        print(f"# incus exec failed: {e}", file=sys.stderr)
        return []
    if r.returncode != 0:
        # Most VMs won't have GPUs or nvidia-smi — that's normal,
        # not an error worth logging.
        return []

    rows = []
    for line in r.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 9:
            continue
        try:
            rows.append({
                "index":       parts[0],
                "pci_bus_id":  parts[1],
                "name":        parts[2],
                "util_gpu":    float(parts[3]),
                "util_mem":    float(parts[4]),
                "mem_used_mib":  float(parts[5]),
                "mem_total_mib": float(parts[6]),
                "temp_c":      float(parts[7]),
                "power_w":     float(parts[8]),
            })
        except ValueError:
            continue
    return rows


def esc(v):
    return v.replace("\\", "\\\\").replace('"', '\\"')


def write_prom(samples):
    """samples: list of (vm_name, gpu_dict)."""
    lines = []

    def emit(metric, help_text, value_key, scale=1.0):
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} gauge")
        for vm, g in samples:
            lbls = (
                f'vm="{esc(vm)}",index="{esc(g["index"])}",'
                f'pci_bus_id="{esc(g["pci_bus_id"])}",name="{esc(g["name"])}"'
            )
            lines.append(f"{metric}{{{lbls}}} {g[value_key] * scale}")

    emit("node_vm_gpu_utilization_percent",
         "GPU compute utilization percent from nvidia-smi inside a VM.",
         "util_gpu")
    emit("node_vm_gpu_memory_utilization_percent",
         "GPU memory-bandwidth utilization percent from nvidia-smi inside a VM.",
         "util_mem")
    emit("node_vm_gpu_memory_used_bytes",
         "GPU memory used in bytes (from nvidia-smi MiB * 2^20).",
         "mem_used_mib", scale=1024 * 1024)
    emit("node_vm_gpu_memory_total_bytes",
         "GPU memory total in bytes.",
         "mem_total_mib", scale=1024 * 1024)
    emit("node_vm_gpu_temperature_celsius",
         "GPU temperature in celsius (from nvidia-smi inside a VM).",
         "temp_c")
    emit("node_vm_gpu_power_watts",
         "GPU power draw in watts.",
         "power_w")

    body = "\n".join(lines) + "\n"
    d = os.path.dirname(OUT)
    fd, tmp = tempfile.mkstemp(prefix=".vm_gpu.", suffix=".prom", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.chmod(tmp, 0o644)
        os.rename(tmp, OUT)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    vms = list_running_vms()
    samples = []
    for vm in vms:
        for gpu in query_vm(vm):
            samples.append((vm, gpu))

    # Always rewrite so node_textfile_mtime_seconds advances. If no VM
    # is up with GPUs, the metric family vanishes — `absent()` or
    # `count() by (node)` queries can detect that explicitly. Stale
    # mtime means the script itself stopped running and is caught by
    # the staleness alert.
    write_prom(samples)
    return 0


if __name__ == "__main__":
    sys.exit(main())

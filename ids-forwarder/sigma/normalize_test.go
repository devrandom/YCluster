package sigma

import "testing"

func TestNormalizeProcessExec(t *testing.T) {
	line := `{"time":"2026-10-28T12:00:00Z","process_exec":{"process":{"pid":4242,"uid":0,"binary":"/bin/sh","arguments":"-c \"whoami\"","pod":{"namespace":"default","name":"web-0","container":{"name":"web","image":{"name":"nginx:1.29"}}}},"parent":{"pid":1,"uid":0,"binary":"/usr/sbin/nginx","arguments":"-g daemon off;"}}}`
	ev, ok, err := NormalizeLine(line)
	if err != nil || !ok {
		t.Fatalf("NormalizeLine = %v, %v", ok, err)
	}
	if ev.EventType != "process_exec" || ev.Image != "/bin/sh" {
		t.Fatalf("unexpected event: %+v", ev)
	}
	if ev.CommandLine != `/bin/sh -c "whoami"` {
		t.Fatalf("CommandLine = %q", ev.CommandLine)
	}
	if ev.ParentImage != "/usr/sbin/nginx" || ev.ParentCommandLine != "/usr/sbin/nginx -g daemon off;" {
		t.Fatalf("parent fields = %q %q", ev.ParentImage, ev.ParentCommandLine)
	}
	if ev.User != "root" || ev.ProcessId != 4242 || ev.ParentProcessId != 1 {
		t.Fatalf("identity fields = %+v", ev)
	}
	if ev.ContainerName != "web" || ev.ContainerImage != "nginx:1.29" {
		t.Fatalf("container fields = %+v", ev)
	}
}

func TestNormalizeTCPConnect(t *testing.T) {
	line := `{"time":"2026-10-28T12:00:01Z","process_kprobe":{"function_name":"tcp_connect","process":{"pid":7,"uid":1000,"binary":"/usr/bin/curl","arguments":"https://example.test/"},"args":[{"sock_arg":{"daddr":"203.0.113.10","dport":443}}]}}`
	ev, ok, err := NormalizeLine(line)
	if err != nil || !ok {
		t.Fatalf("NormalizeLine = %v, %v", ok, err)
	}
	if ev.EventType != "network_connection" || ev.DestinationIp != "203.0.113.10" || ev.DestinationPort != 443 {
		t.Fatalf("unexpected event: %+v", ev)
	}
	if ev.User != "1000" {
		t.Fatalf("User = %q", ev.User)
	}
}

func TestNormalizeCarriesProcessStart(t *testing.T) {
	line := `{"process_exec":{"process":{"pid":9,"uid":1000,"binary":"/bin/bash","start_time":"2026-10-28T12:00:00.123456789Z"}}}`
	ev, ok, err := NormalizeLine(line)
	if err != nil || !ok {
		t.Fatalf("NormalizeLine = %v, %v", ok, err)
	}
	if ev.ProcessId != 9 || ev.StartTime != "2026-10-28T12:00:00.123456789Z" {
		t.Fatalf("process identity = %d %q", ev.ProcessId, ev.StartTime)
	}
}

func TestNormalizeSkipsOtherKprobes(t *testing.T) {
	line := `{"process_kprobe":{"function_name":"security_file_open","process":{"binary":"/bin/cat"}}}`
	_, ok, err := NormalizeLine(line)
	if err != nil || ok {
		t.Fatalf("NormalizeLine = %v, %v; want skipped", ok, err)
	}
}

func TestNormalizeRejectsMalformedJSON(t *testing.T) {
	if _, _, err := NormalizeLine(`{`); err == nil {
		t.Fatal("expected a parse error")
	}
}

package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	mvccpb "go.etcd.io/etcd/api/v3/mvccpb"
	clientv3 "go.etcd.io/etcd/client/v3"
)

func mustBackendURL(s string) *url.URL {
	u, err := url.Parse(s)
	if err != nil {
		panic(err)
	}
	return u
}

// newSupersessionTestSource builds an EtcdSource with ds4.1 routable
// and ds4 superseded onto it, via real watch-event application.
func newSupersessionTestSource(t *testing.T) *EtcdSource {
	t.Helper()
	src := testSource("/models/")
	src.snapshot.Store(&map[string][]*url.URL{})
	src.aclModels.Store(&map[string]ACLModelRule{})
	src.superseded.Store(&map[string]string{})
	src.applyWatchEvents([]*clientv3.Event{
		{
			Type: clientv3.EventTypePut,
			Kv: &mvccpb.KeyValue{Key: []byte("/models/ds4.1"),
				Value: []byte(`{"backends":[{"api_base":"http://backend.example:8000"}]}`)},
		},
		{
			Type: clientv3.EventTypePut,
			Kv: &mvccpb.KeyValue{Key: []byte("/models/ds4"),
				Value: []byte(`{"superseded_by":"ds4.1"}`)},
		},
	})
	return src
}

func supersededTestRouter(t *testing.T) *ModelRouter {
	t.Helper()
	return NewModelRouter(newSupersessionTestSource(t))
}

func TestModelRouterRejectsSupersededModel(t *testing.T) {
	r := supersededTestRouter(t)
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"ds4","messages":[]}`))
	got, err := r.Route(req)

	var se *SupersededError
	if !errors.As(err, &se) {
		t.Fatalf("want SupersededError, got %v", err)
	}
	if se.Model != "ds4" || se.SupersededBy != "ds4.1" {
		t.Errorf("SupersededError = %+v; want ds4 -> ds4.1", se)
	}
	if got != nil {
		t.Errorf("superseded request must not produce a route, got %+v", got)
	}
	if !strings.Contains(err.Error(), "ds4.1") {
		t.Errorf("error should name the replacement, got %q", err.Error())
	}
}

func TestModelRouterSupersededHeaderModel(t *testing.T) {
	r := supersededTestRouter(t)
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"ds4.1","messages":[]}`))
	req.Header.Set("X-Model", "ds4")
	_, err := r.Route(req)
	if !errors.As(err, new(*SupersededError)) {
		t.Fatalf("X-Model header should be checked too, got %v", err)
	}
}

func TestModelRouterReplacementModelStillRoutes(t *testing.T) {
	r := supersededTestRouter(t)
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"ds4.1","messages":[]}`))
	got, err := r.Route(req)
	if err != nil {
		t.Fatalf("replacement model should route normally, got %v", err)
	}
	if got.Model != "ds4.1" || len(got.Candidates) != 1 {
		t.Errorf("unexpected route %+v", got)
	}
}

func TestModelRouterSupersededMultipart(t *testing.T) {
	r := supersededTestRouter(t)
	body := "--BOUNDARY\r\n" +
		"Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n" +
		"\r\n" +
		"binary\r\n" +
		"--BOUNDARY\r\n" +
		"Content-Disposition: form-data; name=\"model\"\r\n" +
		"\r\n" +
		"ds4\r\n" +
		"--BOUNDARY--\r\n"
	req := httptest.NewRequest(http.MethodPost, "/v1/audio/transcriptions", strings.NewReader(body))
	req.Header.Set("Content-Type", "multipart/form-data; boundary=BOUNDARY")
	_, err := r.Route(req)
	if !errors.As(err, new(*SupersededError)) {
		t.Fatalf("multipart model field should be checked, got %v", err)
	}
}

// TestModelRouterEtcdSupersessionAddsAndClears exercises the hot-reload
// transition: a routable model becomes superseded-only (dropped from
// routing and /v1/models), then becomes routable again.
// TestSupersededWithBackendsNeverRoutable covers the migration shape
// where superseded_by is added before the old backends are removed: the
// model must not be listed or routed on either load path.
func TestSupersededWithBackendsNeverRoutable(t *testing.T) {
	value := []byte(`{"backends":[{"api_base":"http://backend.example:8000"}],"superseded_by":"ds4.1"}`)

	// Initial etcd listing.
	src := testSource("/models/")
	m, _, sup := src.initialState([]*mvccpb.KeyValue{{Key: []byte("/models/ds4"), Value: value}})
	if _, ok := m["ds4"]; ok {
		t.Errorf("initial load: superseded model with backends must not be routable")
	}
	if sup["ds4"] != "ds4.1" {
		t.Errorf("initial load: supersession missing, got %v", sup)
	}
	if _, ok := src.Snapshot()["ds4"]; ok {
		t.Errorf("initial load: Snapshot must not contain superseded model")
	}

	// Watch-event path.
	src.snapshot.Store(&map[string][]*url.URL{"ds4": {mustBackendURL("http://backend.example:8000")}})
	src.aclModels.Store(&map[string]ACLModelRule{})
	src.superseded.Store(&map[string]string{})
	src.applyWatchEvents([]*clientv3.Event{{
		Type: clientv3.EventTypePut,
		Kv:   &mvccpb.KeyValue{Key: []byte("/models/ds4"), Value: value},
	}})
	r := NewModelRouter(src)
	if _, ok := src.Snapshot()["ds4"]; ok {
		t.Errorf("watch path: superseded model must be dropped from routing snapshot")
	}
	for _, name := range r.Models() {
		if name == "ds4" {
			t.Errorf("watch path: superseded model must not be listed in Models()")
		}
	}
}

func TestModelRouterEtcdSupersessionAddsAndClears(t *testing.T) {
	src := newSupersessionTestSource(t)
	// Start from a state where ds4 is still routable and not superseded.
	src.applyWatchEvents([]*clientv3.Event{{
		Type: clientv3.EventTypePut,
		Kv: &mvccpb.KeyValue{Key: []byte("/models/ds4"),
			Value: []byte(`{"backends":[{"api_base":"http://backend.example:8000"}]}`)},
	}})
	r := NewModelRouter(src)

	routeReq := func() (*RouteResult, error) {
		return r.Route(httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
			strings.NewReader(`{"model":"ds4","messages":[]}`)))
	}

	got, err := routeReq()
	if err != nil {
		t.Fatalf("pre-supersession model should route, got %v", err)
	}
	if got.Model != "ds4" {
		t.Errorf("Model = %q, want ds4", got.Model)
	}

	// Existing routable model becomes superseded-only via a watch event.
	src.applyWatchEvents([]*clientv3.Event{{
		Type: clientv3.EventTypePut,
		Kv: &mvccpb.KeyValue{Key: []byte("/models/ds4"),
			Value: []byte(`{"superseded_by":"ds4.1"}`)},
	}})
	_, err = routeReq()
	if !errors.As(err, new(*SupersededError)) {
		t.Fatalf("updated model should be superseded, got %v", err)
	}
	if snap := src.Snapshot(); len(snap["ds4"]) != 0 {
		t.Errorf("superseded-only model should be dropped from routing snapshot, got %v", snap)
	}
	if models := r.Models(); len(models) != 1 {
		t.Errorf("only the replacement should be listed, got %v", models)
	}

	// Clearing superseded_by restores routing.
	src.applyWatchEvents([]*clientv3.Event{{
		Type: clientv3.EventTypePut,
		Kv: &mvccpb.KeyValue{Key: []byte("/models/ds4"),
			Value: []byte(`{"backends":[{"api_base":"http://backend.example:8000"}]}`)},
	}})
	got, err = routeReq()
	if err != nil {
		t.Fatalf("cleared supersession should route, got %v", err)
	}
	if got.Model != "ds4" {
		t.Errorf("Model = %q, want ds4", got.Model)
	}
}

func TestHandleRouteErrorSuperseded(t *testing.T) {
	h := NewHandler(NewModelRouter(testSource("/models/")))
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)

	if !h.handleRouteError(rec, req, &SupersededError{Model: "ds4", SupersededBy: "ds4.1"}) {
		t.Fatal("superseded error should be handled")
	}
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}

	var body struct {
		Error struct {
			Message string `json:"message"`
			Type    string `json:"type"`
			Code    string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if body.Error.Code != "model_not_found" {
		t.Errorf("error.code = %q, want model_not_found", body.Error.Code)
	}
	if body.Error.Type != "invalid_request_error" {
		t.Errorf("error.type = %q, want invalid_request_error", body.Error.Type)
	}
	if !strings.Contains(body.Error.Message, "ds4.1") {
		t.Errorf("error.message should name replacement, got %q", body.Error.Message)
	}
}

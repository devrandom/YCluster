# Document Extraction (MinerU)

YCluster exposes a layout-aware document parsing API. Clients reach it
through the same gateway as the LLM and embedding endpoints — no
special configuration.

Behind the scenes the cluster runs **MinerU 3.4.x** (VLM +
layout/OCR/table/formula pipeline) on Apple Silicon via MLX. See
[`docs/operations/document-extraction.md`](../operations/document-extraction.md)
for the deployment side and [`docs/design/mineru.md`](../design/mineru.md)
for the technology choice.

## What's available

- **Layout-aware extraction**: reading-order text blocks, headings,
  tables, equations, code, lists.
- **Input formats**: PDF, DOCX, PPTX, XLSX, images (PNG/JPG/etc.).
- **Output formats**: structured Markdown (with embedded image refs),
  per-page layout JSON, flat content-list JSON.

The cluster registers two model names that both point at the same
backend:

- `document-parse` — the semantic name (use this from client code).
- `mineru-parse` — the implementation-specific alias.

## Quick start

```bash
# Cluster master key from etcd:
#   /cluster/config/inference/master-key
# Or any Open-WebUI api_key:
export OPENAI_API_KEY=sk-<your-token>
export OPENAI_BASE_URL=http://inference.xc/v1   # or https://your-domain.com/v1

curl -s -X POST "$OPENAI_BASE_URL/file_parse" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F "files=@report.pdf" \
  -F "model=document-parse" \
  -F "return_md=true"
```

The response is JSON with `results.<file>.md_content` containing the
extracted markdown:

```json
{
  "task_id": "...",
  "status": "completed",
  "backend": "hybrid-engine",
  "results": {
    "report": {
      "md_content": "## Report Title\n\nSection 1: ...\n\n..."
    }
  }
}
```

Python:

```python
import requests
with open("report.pdf", "rb") as f:
    r = requests.post(
        "http://inference.xc/v1/file_parse",
        headers={"Authorization": f"Bearer {token}"},
        files={"files": ("report.pdf", f, "application/pdf")},
        data={"model": "document-parse", "return_md": "true"},
        timeout=180,
    )
r.raise_for_status()
markdown = r.json()["results"]["report"]["md_content"]
```

## Form fields

| Field | Required | Description |
|---|---|---|
| `files` | yes | One or more binary uploads (multipart). PDF / DOCX / PPTX / XLSX / images. |
| `model` | yes | `document-parse` or `mineru-parse`. |
| `return_md` | no | `true` to include `<name>.md` in the response. |
| `return_middle_json` | no | `true` to include `<name>_middle.json` (intermediate layout with bboxes). |
| `return_content_list_json` | no | `true` to include `<name>_content_list.json` (flat reading-order blocks). |
| `return_model_json` | no | `true` to include `<name>_model.json` (raw model output, debug). |
| `return_original_file` | no | `true` to echo the original file alongside the parse. |
| `response_format_zip` | no | `true` to return the result as a ZIP (otherwise JSON with selected fields inline). |
| `start_page` / `end_page` | no | Limit parsing to a page range (PDF only). |
| `lang_list` | no | Comma-separated language hints (e.g. `en,zh`) for OCR. |
| `formula_enable` | no | `true` to enable formula recognition (default: `true`). |
| `table_enable` | no | `true` to enable table recognition (default: `true`). |

## Output formats

### JSON (default when not `response_format_zip`)

```json
{
  "task_id": "8786bae1-...",
  "status": "completed",
  "backend": "hybrid-engine",
  "file_names": ["report"],
  "results": {
    "report": {
      "md_content": "## Report Title\n\n...",
      "middle_json": "{...}",        // if return_middle_json=true
      "content_list_json": "[...]",  // if return_content_list_json=true
      "model_json": "{...}"          // if return_model_json=true
    }
  }
}
```

### ZIP (`response_format_zip=true`)

A binary ZIP containing the parsed files for each uploaded document:

| File | Content |
|---|---|
| `<name>.md` | Human-readable Markdown (with embedded image references). |
| `<name>_content_list.json` | Flat reading-order list of content blocks. |
| `<name>_content_list_v2.json` | Page-grouped `{type, content: {...}}` schema. |
| `<name>_middle.json` | Intermediate structured layout (blocks/lines/spans with bboxes). |
| `<name>_model.json` | Raw model output (debug). |
| `<name>_layout.pdf` | Layout analysis visualization (debug). |
| `<name>_span.pdf` | Text-span visualization (pipeline backend only). |

```bash
# ZIP output
curl -s -X POST "$OPENAI_BASE_URL/file_parse" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F "files=@report.pdf" \
  -F "model=document-parse" \
  -F "response_format_zip=true" \
  -F "return_md=true" \
  -o report-parse.zip
unzip -l report-parse.zip
```

## Limits worth knowing about

- **Upload size**: 200 MiB max per request (nginx `client_max_body_size`
  + proxy `maxMultipartBodyBytes`). Large textbooks may exceed — bump
  both in lockstep, or chunk client-side.
- **Concurrency**: 1 concurrent request on macOS hosts (MinerU clamps
  to 1 when listening on `0.0.0.0` to mitigate SSRF risk). Expect
  linear queueing under load. NVIDIA nodes are faster but Docker-only
  on Linux.
- **First request after a redeploy or boot**: ~14 s while the
  hybrid-engine warms up. Subsequent requests are ~0.7 s for short
  docs; longer docs scale with page count.
- **In-process task state**: MinerU tracks results in memory only.
  Service restart loses pending tasks (404 on old `task_id`s). For
  long docs (>1000 pages) the recommended pattern is sync `/file_parse`
  with a generous client-side timeout.
- **CJK fonts**: macOS already ships them (no install needed).
- **Cold model download**: ~22 GB on first install; cached at
  `~/mineru-models/` thereafter. `MINERU_MODEL_SOURCE=local` skips
  the network re-check on subsequent boots.

## See also

- [`inference.md`](inference.md) — the parent gateway doc covering
  `/v1/chat/completions`, `/v1/embeddings`, `/v1/audio/...`,
  authentication, and `ycluster inference` CLI.
- [`docs/operations/document-extraction.md`](../operations/document-extraction.md)
  — deployment, opt-in, troubleshooting.
- [`docs/design/mineru.md`](../design/mineru.md) — technology choice,
  MLX-on-Mac rationale, why not Docker.

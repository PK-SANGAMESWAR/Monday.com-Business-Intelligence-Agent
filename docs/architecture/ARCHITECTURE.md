# Architecture

STATUS: Complete. Extracted and expanded from `docs/00_IMPLEMENTATION_PLAN.md` §3 once
every layer below was actually implemented (F01–F09).

## Shape

```text
                    +----------------------------+
   browser  ------->|  Streamlit chat UI (app.py)|
   (hosted link)    +-------------+--------------+
                                  | user turn
                    +-------------v--------------+
                    |  Agent core                |
                    |  bi_agent/agent/           |
                    |  Anthropic tool-use loop   |
                    |  system prompt = the rules |
                    +-------------+--------------+
                       structured tool calls
                    +-------------v--------------+
                    |  Analytics                 |<-- crossboard.py: shared-
                    |  bi_agent/analytics/       |    dimension comparison,
                    |  pandas metrics + query    |    refuses row joins
                    |  spec; returns value +     |
                    |  coverage metadata         |--> briefing.py: leadership
                    +-------------+--------------+    brief assembly
                    +-------------v--------------+
                    |  Normalization & quality   |
                    |  bi_agent/data/            |
                    |  schema map, cleaners,     |
                    |  DataQualityReport         |
                    +-------------+--------------+
                    +-------------v--------------+
                    |  monday.com client          |
                    |  bi_agent/monday/           |
                    |  GraphQL, paging, retry,    |
                    |  TTL cache, read-only gate  |
                    +-------------+--------------+
                                  | HTTPS
                          monday.com API v2
                    (Deals board . Work Orders board)
```

`scripts/seed_monday.py` sits outside this stack: it is the only component that writes to
monday.com, it runs once at setup time (before any of the above can be exercised live),
and it is not importable from the `bi_agent` package — a write path and a read-only agent
package do not share code, so a bug in one cannot become a mutation from the other.

## Layers, bottom to top

**`bi_agent/monday/`** — `client.py` owns the HTTP/GraphQL transport: bearer auth,
timeout, retry with backoff on 429/5xx, and a read-only gate that inspects every outgoing
document and raises before sending anything containing a `mutation` operation.
`queries.py` holds the small, fixed set of GraphQL documents actually sent. `boards.py`
resolves column titles to monday's opaque column IDs, walks `items_page` cursor
pagination, and applies the `BoardRepository`'s TTL cache — one fetch per board per cache
window, not one per question.

**`bi_agent/data/`** — `schema.py` expresses each board as data: title → canonical
snake_case field → type → parser, rather than scattered conditionals. `normalize.py` is
every cleaner CLAUDE.md's messiness list demanded: dropping the two embedded-header junk
rows, distinguishing `0` (billed nothing) from empty (not recorded), date coercion,
label normalization. `quality.py` builds a `DataQualityReport` per board — always-null
fields, junk rows excluded, stage/status conflict count — as measured facts, not
narration. `repository.py` is the only entry point analytics uses: fetch → normalize →
cache.

**`bi_agent/analytics/`** — `spec.py` defines `QuerySpec` (validated filters + group-by +
metric) and `MetricResult`, the value type every metric returns: `{value, unit, n_used,
n_total, excluded, caveats}` — never a bare number. `metrics.py` holds the named metrics
(`pipeline_value`, `revenue_billed`, `collected_amount`, `receivable`,
`stage_distribution`, `sector_breakdown`, …) plus `run_query` for the general spec.
`calendar.py` resolves "this quarter" against the Indian fiscal year (April–March),
anchored to the real clock, and falls back explicitly — never silently — when the
resolved period has no rows. `crossboard.py` is the side-by-side comparison policy:
restricted to `sector` and `owner_code`, the only dimensions genuinely shared under the
same canonical name on both boards; anything else (a `deal_name` or `serial_no` "join")
is refused with a stated reason before it can silently multiply revenue. `briefing.py`
assembles the leadership brief from metrics already proven correct — it performs zero new
arithmetic of its own.

**`bi_agent/agent/`** — `tools.py` is the tool surface exposed to the model
(`describe_data`, `query_deals`, `query_work_orders`, `pipeline_health`,
`revenue_and_collections`, `compare_boards`, `leadership_brief`, `data_quality_report`).
`prompt.py` is the system prompt: the caveat obligation, the clarify-when-ambiguous
policy, the "never invent a number" rule. `loop.py` runs the Anthropic tool-use loop,
carries multi-turn conversation state, and degrades per the typed exception hierarchy in
`bi_agent/errors.py` rather than crashing the chat.

**`app.py`** — Streamlit entrypoint, deliberately flat at the repo root (not under
`src/`) because Streamlit Community Cloud runs `streamlit run app.py` with no install
step. `BoardRepository` is a `st.cache_resource` (one instance, process-wide, with its own
TTL cache); `Agent` lives in `st.session_state` (one per browser tab) so two founders
chatting at once never share conversation history.

## The central decision: the LLM chooses, Python computes

The model never performs arithmetic and never sees a raw row it has to add up. It picks a
tool and arguments; tested Python in `bi_agent/analytics/` returns a `MetricResult`; the
model's only job is to phrase that as an insight, including the caveats Python already
computed. This is why a hallucinated number has no path to production here — there is no
step where the model is asked to calculate anything.

## Fetch-once, compute-locally

Both boards together are on the order of 500 rows — comfortably under a megabyte. Rather
than translating each question into a filtered API call, `BoardRepository` fetches each
board in full, normalizes it once, and caches the result with a TTL (`CACHE_TTL_SECONDS`,
default 300s; manual refresh available in the UI sidebar). This keeps coverage statistics
("165 of 344") true board-wide instead of an artifact of whatever page the API happened to
return, at the cost of the design no longer being appropriate once a board reaches roughly
10k rows — recorded as a known scale boundary, not discovered as a surprise.

## Error handling philosophy

Every exception in `bi_agent/errors.py` maps to a specific degradation, never a crash and
never a silent zero: auth failure surfaces as "cannot authenticate", rate limiting backs
off then serves stale cache and says so, an unavailable board falls back to the other
board and names the one that failed, an invalid tool argument comes back to the model as a
correctable error rather than reaching the user at all.

# Workflow Authoring — Instruction Prompts

These are **natural-language instruction prompts** you can paste to the
`botcircuits-workflow-authoring` skill. They describe what the workflow should
do; the skill turns each into a runnable workflow (steps, conditions, build).
No JSON here — authoring is done separately.

---

## Use case: Batch shipment tracking (file in → web fetch → JSON out)

> Create a workflow named **`shipment_tracking`** that checks the live status of
> **many parcels at once**, reading the tracking numbers from a text file and
> writing a single results file at the end.
>
> Use `http://localhost:4000/v1` as the carrier API host (a local mock — see
> `api/` and the README to start it).
>
> **What it should do, step by step:**
>
> 1. **Start.** Take a `tracking_file` input — the path to a plain-text file
>    that holds one tracking number per line (default it to
>    `tracking-ids.txt`). Read the file, trim whitespace, and ignore blank
>    lines. This produces a list of tracking numbers to process.
>
> 2. **Loop over every tracking number.** For each one, run the fetch +
>    classification below, collect a result record, and continue to the next.
>    Do **not** ask the user anything per item — this is an unattended batch
>    run.
>
> 3. **Fetch live status (web fetch).** For the current tracking number, fetch
>    `http://localhost:4000/v1/track?number={tracking_number}` and read the JSON
>    response. Extract `status`, `last_location`, and `estimated_delivery`.
>
> 4. **Classify the fetch result** into an `outcome` for this item (handle
>    failures first):
>    - Fetch fails, times out, or returns a non-200 / empty response →
>      `outcome = "error"`.
>    - Response says the tracking number is **not found / invalid** →
>      `outcome = "not_found"`.
>    - `status` is **delivered** → `outcome = "delivered"`.
>    - `status` is **out for delivery** → `outcome = "out_for_delivery"`.
>    - `status` is **in transit** AND `estimated_delivery` is **more than 7 days
>      away** → `outcome = "delayed"`.
>    - `status` is **in transit** (and not delayed) → `outcome = "in_transit"`.
>    - `status` is **exception / returned / lost** → `outcome = "escalate"`.
>    - Any unrecognized status → treat it as `outcome = "in_transit"` (sensible
>      default).
>
> 5. **Record the result.** For each tracking number, append a record holding:
>    `tracking_number`, `outcome`, `status`, `last_location`,
>    `estimated_delivery`, and a short human-readable `note`. For `delayed` and
>    `escalate` outcomes, also set `needs_attention: true`.
>
> 6. **Save the results file.** After every tracking number has been processed,
>    write the full list of result records as JSON to
>    **`tracking-status-<current_date>.json`** (e.g.
>    `tracking-status-2026-06-21.json`, using today's date in `YYYY-MM-DD`).
>    Include a small summary at the top: total processed and a count per
>    `outcome`. Then end the flow.
>
> Keep the carrier API host (`http://localhost:4000/v1`), the input file name,
> and the 7-day delay threshold easy to change. The loop must continue past
> individual failures — one bad tracking number should produce an `error` /
> `not_found` record, never stop the batch.

---

### Notes for whoever runs the prompt

- This exercises **file input** (read many IDs from a text file), a **loop**
  over the list, a **web fetch** per item (`agentAction` that retrieves a URL
  and parses JSON), **nested conditions** (status + delay window together), and
  **file output** (one dated JSON results file).
- The fetch-result classification is intentionally checked **before** the status
  classification so network/lookup failures never fall through into the happy
  path.
- A sample input file is provided at
  [tracking-ids.txt](tracking-ids.txt) — its prefixes (e.g. `DLV…`, `DLY…`,
  `FAIL…`) drive the mock API down each branch. See the README for the full
  prefix table.

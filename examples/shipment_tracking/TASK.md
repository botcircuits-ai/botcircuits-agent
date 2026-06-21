# Workflow name:  `shipment_tracking`

---

# Instruction Prompt

> Create a workflow that checks the live status of **many parcels at once**.
> It reads a list of tracking numbers from a text file, decides an **outcome
> for every item in that list**, and writes a single results file at the end.
> This is an unattended batch run — never ask the user anything.
>
> Use `http://localhost:4000/v1` as the carrier API host (a local mock — see
> `api/` and the README to start it).
>
> **What it should do:**
>
> 1. **Start.** Take a `tracking_file` input — the path to a plain-text file
>    holding one tracking number per line (default
>    `examples/shipment_tracking/tracking-ids.txt`). This file is the **list**
>    of items to process.
>
> 2. **Decide an `outcome` for every tracking number in the list.** For each
>    item, the per-item facts come from a deterministic carrier lookup: query
>    `http://localhost:4000/v1/track?number={tracking_number}` and read the JSON
>    response, extracting `status`, `last_location`, and `estimated_delivery`,
>    plus whether the lookup failed (non-200 / empty) or the number was not
>    found. Gather these facts **per item** — do not have the model decide the
>    outcomes itself.
>
> 3. **Classification rules** (applied to each item; check failures first):
>    - Lookup fails, times out, or returns a non-200 / empty response →
>      `outcome = "error"`.
>    - Carrier says the tracking number is **not found / invalid** →
>      `outcome = "not_found"`.
>    - `status` is **delivered** → `outcome = "delivered"`.
>    - `status` is **out for delivery** → `outcome = "out_for_delivery"`.
>    - `status` is **in transit** AND `estimated_delivery` is **more than 7 days
>      away** → `outcome = "delayed"`.
>    - `status` is **in transit** (and not delayed) → `outcome = "in_transit"`.
>    - `status` is **exception / returned / lost** → `outcome = "escalate"`.
>    - Any unrecognized status → default to `outcome = "in_transit"`.
>
> 4. **Each decided record** holds: `tracking_number`, `outcome`, `status`,
>    `last_location`, `estimated_delivery`, and a short human-readable `note`.
>    For `delayed` and `escalate` outcomes, also set `needs_attention: true`.
>    Collect all decided records into a single list.
>
> 5. **Save the results file.** After the whole list is decided, write the
>    collected records as JSON to **`tracking-status-<current_date>.json`**
>    (e.g. `tracking-status-2026-06-21.json`, today's date in `YYYY-MM-DD`).
>    Include a summary at the top: total processed and a count per `outcome`.
>    Then end the flow.
>
> Keep the carrier API host (`http://localhost:4000/v1`), the input file name,
> and the 7-day delay threshold easy to change. A single bad tracking number
> must produce an `error` / `not_found` record for that item — it must never
> stop the rest of the batch.

---

### Notes for whoever runs the prompt

- This is a **list/iteration** workflow: one decision applied to **every item**
  in a list read from a file. It should compile to a single **`listDecision`**
  step (engine decides each item deterministically and collects the records),
  **not** a manual `next_item → fetch → record → next_item` self-loop. The
  self-loop hands iteration to the model, traces only a couple of items, and is
  non-deterministic — see the engine/segment design.
- The per-item carrier lookup is the item's **fact source** (an `itemFacts`
  exec / item lookup), so the engine gathers each item's `status` etc. with no
  per-item LLM call. The failure / not-found checks are evaluated first.
- A sample input file is provided at
  [tracking-ids.txt](tracking-ids.txt) — its prefixes (e.g. `DLV…`, `DLY…`,
  `FAIL…`) drive the mock API down each branch. See the README for the full
  prefix table.
- After running, expect one decided record per tracking number (10 in the
  sample) and a per-`outcome` summary in the dated results file.

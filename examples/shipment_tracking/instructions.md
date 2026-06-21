# Workflow Authoring — Instruction Prompts

These are **natural-language instruction prompts** you can paste to the
`botcircuits-workflow-authoring` skill. They describe what the workflow should
do; the skill turns each into a runnable workflow (steps, conditions, build).
No JSON here — authoring is done separately.

---

## Use case: Complex flow with web fetch + multiple conditions

> Create a workflow named **`shipment_tracking`** that tracks a customer's
> parcel and decides what to do based on its live status.
>
> **What it should do, step by step:**
>
> 1. **Start.** Take a `tracking_number` and the customer's `email` as inputs.
>
> 2. **Fetch live status (web fetch).** Call the carrier's tracking API by
>    fetching the URL
>    `http://localhost:4000/v1/track?number={tracking_number}` and read the JSON
>    response. Extract `status`, `last_location`, and `estimated_delivery` from
>    it. (This is a local mock API — see `api/` and the README to start it.)
>
> 3. **Branch on the fetch result itself** (handle failure first):
>    - If the fetch fails, times out, or returns a non-200 / empty response →
>      go to a step that asks the customer to re-check the tracking number and
>      ends the flow gracefully.
>    - If the response says the tracking number is **not found / invalid** →
>      tell the customer it isn't recognized yet and end.
>    - Otherwise continue to the status branch below.
>
> 4. **Branch on `status` (multiple conditions):**
>    - If `status` is **delivered** → confirm delivery to the customer with the
>      delivery location and stop.
>    - If `status` is **out for delivery** → tell the customer it arrives today
>      and show `estimated_delivery`.
>    - If `status` is **in transit** AND `estimated_delivery` is **more than 7
>      days away** → flag it as delayed, then go to the escalation step.
>    - If `status` is **in transit** (and not delayed) → give a normal in-transit
>      update with `last_location` and `estimated_delivery`.
>    - If `status` is **exception / returned / lost** → go straight to the
>      escalation step.
>
> 5. **Escalation step (agent action).** Open a support ticket summarizing the
>    tracking number, current status, and reason, and tell the customer a human
>    will follow up.
>
> 6. **Satisfaction check (question + condition).** After any customer-facing
>    update (except the hard failures in step 3), ask the customer "Does this
>    answer your question?"
>    - If **no** → route to the escalation step.
>    - If **yes** → end the flow.
>
> Keep the carrier base URL and the 7-day delay threshold easy to change. Make
> the failure/exception paths terminal so the flow never loops, and make sure
> every status value has a branch (use a sensible default for anything
> unrecognized — treat unknown statuses as in-transit updates).

---

### Notes for whoever runs the prompt

- This exercises a **web fetch** (`agentAction` that retrieves a URL and parses
  JSON), **nested conditions** (status + delay window together), and a
  **question step** whose answer drives branching.
- The fetch-result branch is intentionally checked **before** the status branch
  so network/lookup failures never fall through into the happy path.
- Inputs (`tracking_number`, `email`) and extracted fields (`status`,
  `last_location`, `estimated_delivery`) become workflow variables during build.

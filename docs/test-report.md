# Test report

**Date:** 2026-10-21
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 62 of 62 checks pass: 32 scenarios, plus three checks over every request the workflow made in the run (5.1 minutes, most of it waiting for escalation timers)

## How it was tested

No real monday.com board, Twilio number, Claude account or Slack workspace was used. The test script:

1. starts mock monday.com (GraphQL), Twilio, Anthropic and Slack APIs on the test machine,
2. imports temporary copies of the workflow and the error alert workflow into n8n, with every API host pointed at the mocks, fake keys and a 9 second escalation timer, and activates both (n8n 2.x only runs an error workflow that is active),
3. posts leads to the webhook: forged and empty requests, website forms, nested portal JSON, form posts with the secret in the URL, repeats one after another and at the same moment, outages of Claude, monday, Twilio and Slack, a Claude refusal, and lead text that carries Slack markup,
4. waits for each n8n run to finish (the intake run and, where expected, the escalation run it starts in the background), then checks every request the workflow made and the node each run ended on,
5. deletes the temporary workflows.

Five temporary copies are used, each with its own settings: the main one, one with quiet hours that end three minutes later (so n8n parks the escalation run in its database and resumes it, the same path a 5 minute timer takes in production), one where everyone is away with custom column IDs, a UK number and a Twilio Messaging Service, one with a broken `AGENTS_JSON`, and one with `LEAD_WEBHOOK_SECRET` left empty.

### How strict the mocks are

- **monday.com:** every query and mutation is parsed and validated against a GraphQL schema written from monday's API reference (field names, argument names and types, variable types). Column values are checked the way monday checks them: a phone needs `countryShortName` and a number that belongs to that country (for +1 numbers the area code decides between the US, Canada, Puerto Rico and the other +1 countries), an email needs `email` and `text`, a status label must already exist unless `create_labels_if_missing` is set, long text is limited to 2,000 characters, and an unknown column ID fails. Errors come back with HTTP 200 and an `errors` array, like the real API. A missing token gets 401 and a wrong `API-Version` gets monday's version error, and the test fails if the workflow sent even one request that monday would reject. The `Idempotency-Key` header works as documented: a repeat gets the cached response with `Idempotency-Replayed: true`, and a repeat while the first request is still running gets 409 `IDEMPOTENCY_CONFLICT`.
- **Twilio:** the account SID in the path must match basic auth, the body must be form encoded, `To` and `From` must be E.164, a `MessagingServiceSid` must look like one, `Body` is required and limited to 1,600 characters, and errors use Twilio's codes (21211, 21604, 21610 and so on).
- **Claude:** the mock returns scripted tool calls keyed by the lead's message, so the test checks the workflow, not the model. It still rejects requests the real Messages API would reject: a missing key or version header, no `max_tokens`, no system prompt, anything other than one user message, an invalid tool schema, a tool that is not forced, a field the API does not know, or an effort setting on a model that has none (Haiku 4.5).
- **Slack:** the incoming webhook mock wants JSON with a `text` field, like the real one, and can be told to refuse posts.

### Check against the real Claude API

The mock cannot prove the real API accepts the request, so the exact request the workflow builds (from the Validate Lead node's code, for the website form lead below) was sent to Anthropic's token counting endpoint. That endpoint validates the system prompt, the message, the tool schema, the forced tool choice, the effort setting and the model name without generating anything or using credits.

| Model | Request | Result | Input tokens |
|---|---|---|---|
| `claude-sonnet-5` (default) | as built, effort `low` | 200 OK | 1,326 |
| `claude-opus-5` | as built, effort `low` | 200 OK | 1,258 |
| `claude-haiku-4-5` | as built, no effort field | 200 OK | 1,230 |
| `claude-haiku-4-5` | with effort `low` added by hand | 400, "This model does not support the effort parameter" | |
| `claude-opus-5-5` | as built | 400, forced `tool_choice` not supported | |
| `claude-fable-5-1` | as built | 400, forced `tool_choice` not supported | |

The last three rows are why the workflow leaves the effort field out for Haiku, and why the setup guide says not to use Opus 5.5 or Fable 5.1 here.

## Results

The real output of the final run. The results are printed at the end; the line at the top is printed earlier, while the test waits for the escalation run n8n parked in its database.

```
(waiting 126 s for the escalation run that n8n parked in the database)

Forged request, no secret
  PASS  refused with 401, nothing called, run flagged  (401, ended at Stop: Bad Secret)

Forged request, wrong secret
  PASS  refused with 401, nothing called, run flagged  (401, ended at Stop: Bad Secret)

Forged request, no secret / wrong secret
  PASS  the error workflow posts one Slack alert per refused run, with its link  (:rotating_light: *TEST real-estate-speed-to-lead (temporary)* failed at *Stop: Bad Secret*)

No valid phone or email
  PASS  refused with 400, monday never called  (400 {'ok': False, 'error': 'a valid phone number or email is required'}, ended at Respond: 400)

New lead (website form)
  PASS  answered 202 right away  (202 {'ok': True, 'status': 'accepted'})
  PASS  duplicate search by phone digits and lowercased email, with token and API version  ({"board": "5551234567", "phoneCol": "lead_phone", "emailCol": "lead_email", "phone": "15125550142", "email": "jamie.rivera@example.com"})
  PASS  one monday item with phone (E.164 + country), email, status New, agent, source, message, run ID  ({"lead_phone": {"phone": "+15125550142", "countryShortName": "US"}, "lead_email": {"email": "jamie.rivera@example.com", "text": "jamie.rivera@example.com"}, "le)
  PASS  instant text to the lead names the agent (basic auth, form encoded, From number)  (Hi Jamie, thanks for reaching out to Acme Realty! Maya Chen will call or text you in the next few minutes. You can also reach Maya directly at (512) 555-0101. Reply STOP to opt out.)
  PASS  the lead is texted before Claude is even called (speed first)  (log order [4, 5, 7, 10])
  PASS  one Claude call, forced tool, low effort; it gets the message but not the name, phone or email  (1 call(s))
  PASS  Claude's details saved on the item  ({"lead_intent":"Buyer","lead_budget":"$480k","lead_timeline":"ASAP","lead_area":"East Austin","lead_preapproved":"Yes",")
  PASS  agent text with the summary and the monday link  (New lead for you: Jamie Rivera)
  PASS  run ends cleanly, no flags  (ended at Needs Attention?)

Escalation, status still New
  PASS  status read again 9 seconds later, as its own run  (after 8.4 s)
  PASS  reassigned to Leo Park (Ana Diaz is away) with a note on the item  (Still New after 0.15 minutes, so this lead moved from Maya Chen to Leo Park.)
  PASS  Leo gets a text with the lead and the link  (Lead reassigned to you: Jamie Rivera)
  PASS  broker alerted in Slack with both agents mentioned  (<@U01BROKER01> :warning: *Lead not picked up within 0.15 minutes*)

Portal lead (nested JSON)
  PASS  fields read from the nested payload, phone with dots normalized, next agent in turn (Leo)  ({"lead_agent": "Leo Park", "lead_source": "Example Listings"})

Agent updates the status in time
  PASS  escalation run only reads the status, then stops  (ended at Still New?)

Same lead within DEDUPE_HOURS
  PASS  existing item gets a note, nothing new is created, nobody is texted, no Claude call  (ended at Update Saved?)
  PASS  no escalation timer started, no item created, no texts  (10 nodes ran)

Email only lead (form post, ?key=)
  PASS  accepted, routed to Maya, no text to the lead, the agent is told it is email only  (Investor, urgency 4/5)

Agent changed by hand
  PASS  escalation sees another agent on the item and does nothing  (ended at Still New?)

Same lead by email, now with a phone
  PASS  the phone is added to the existing item, no new routing  ({"lead_phone":{"phone":"+15125550166","countryShortName":"US"}})

Same submission twice at once
  PASS  exactly one monday item, one text to the lead, one to the agent  (1 item(s), 1 lead text(s))
  PASS  both create calls carried the same Idempotency-Key; the second got monday's 409 or replay and stopped  (3 create calls, runs ended at ['Copy Confirmed?', 'Needs Attention?'])

Search down, same lead sent again
  PASS  search retried 3 times, then the create call is replayed by monday and the run stops, no texts  (ended at Copy Confirmed?)

Claude down (529)
  PASS  retried 3 times, then sorted by keyword rules (seller, 3 to 6 months) and marked as such in monday  ({"lead_intent": "Seller", "lead_timeline": "3 to 6 months", "lead_urgency": "2"})
  PASS  the lead and the agent are still texted, the agent sees the AI summary is missing  (1 lead text(s))
  PASS  run flagged at Stop: Needs Attention and posted to Slack by the error workflow  (1. Claude did not answer after 3 tries (529 overloaded_error Overloaded). The lead was sor)

Lead text refused (opted out)
  PASS  retried 3 times; the agent is told to call; a note goes on the monday item  (The instant text to the lead did not go through (400 21610 Attempt to send to unsubscribed recipient)
  PASS  run flagged with the Twilio reason  (1. The instant text to the lead failed (400 21610 Attempt to send to unsubscribed recipien)

monday down when saving
  PASS  create retried 3 times; the lead and the agent are still texted; no details save, no timer  (2 texts)
  PASS  run flagged with the lead details for a manual add  (1. The lead was NOT saved in monday (500 Internal server error). Add it by hand. There is )

Canadian number at a US office
  PASS  saved with country CA, which monday's number check accepts (a US tag would be refused)  ({"phone": "+14165550133", "countryShortName": "CA"})

monday returns 200 with errors
  PASS  the error is not mistaken for success: run flagged with the monday error code  (1. monday did not save the AI details (ColumnValueException invalid value, please check our API docu)

Escalation cannot read monday
  PASS  read retried 3 times, nothing reassigned, run flagged with the item link and posted to Slack  (ended at Stop: Status Check Failed)

Repeat submission, monday down
  PASS  note retried 3 times, nothing routed or texted, run flagged with the item link  (ended at Stop: Update Failed)

Unreadable phone, valid email
  PASS  routed by email to Leo; nobody texts the lead; the agent is told it is email only  ({"lead_agent": "Leo Park", "lead_email": {"email": "pat.lowe@example.com", "text": "pat.lowe@example.com"}})
  PASS  run flagged with the number that could not be read  (1. The phone number "555-01" could not be read, so the lead was not texted.)

Claude answers without the tool (refusal)
  PASS  one call, not retried; keyword rules fill in and the item says so  ({"lead_intent": "Unknown", "lead_classified_by": "Rules (Claude unavailable)"})
  PASS  the lead and Maya are still texted; the run is flagged with the stop reason  (1. Claude answered without the classify_lead tool (stop_reason refusal). The lead was sort)

monday busy with the same key (409 on every try)
  PASS  create tried 3 times with one key; no texts, no Claude call, no timer  (3 create calls, ended at Stop: Not Routed)
  PASS  the run does not end quietly: Stop: Not Routed names the lead and the error workflow posts it  (Lead Uma Grant, +15125550148, uma.grant@example.com was NOT routed. monday answered every try with 4)

Agent text refused
  PASS  the lead is texted once; the text to Leo is tried 3 times  (3 tries to Leo)
  PASS  run flagged so the broker can pass the lead on, posted to Slack  (1. The text to Leo Park failed (400 21610 Attempt to send to unsubscribed recipient). Tell)

Reassignment fails
  PASS  reassign tried 3 times; the lead stays with Maya; Leo is not texted  (3 reassign calls)
  PASS  the broker is asked to move it by hand; what the lead typed is escaped, so <!channel> pings nobody  (Sky Reed: Buyer, urgency 3/5, from &lt;!channel&gt; Social Ad)

Next agent text and Slack both fail
  PASS  reassigned to Maya in monday; her text is tried 3 times  (3 tries to Maya)
  PASS  the broker alert says the text failed; Slack refused it 3 times, so the run fails and the error workflow reports it  (3 Slack tries, run error)

Round robin
  PASS  leads alternate Maya, Leo, Maya...; Ana (away) never gets one; repeats and unrouted leads do not move the pointer  (Maya, Leo, Maya, Leo, Maya, Leo, Maya, Leo, Maya, Leo, Maya, Leo, Maya, Leo)

Quiet hours
  PASS  the lead is texted at once, but promised a morning follow up  (Hi Kim, thanks for reaching out to Acme Realty! Maya Chen will get back to you f)
  PASS  the agent is told the deadline in local time  (Please update the status in monday by 3:22 PM, or the lead goes to the next agent.)
  PASS  the escalation run is parked in the database until quiet hours end plus ESCALATION_MINUTES  (waiting, waitTill 20:22:06 UTC, expected 20:22:06)

Quiet hours, long wait
  PASS  n8n resumes the parked run; no other agent is free, so the broker is asked to follow up  (<@U01BROKER01> :warning: *Lead not picked up 0.1 minutes after quiet hours*)

Everyone away (custom columns, UK number)
  PASS  the lead gets a text from the office; the item is Unassigned in the mapped columns  ({"status": {"label": "New"}, "text_agent": "Unassigned", "lead_phone": {"phone": "+447700900123", "countryShortName": "GB"}})
  PASS  texts go through the Messaging Service; nobody else texted; no timer; run flagged  (1. No agent is available. Everyone in AGENTS_JSON is marked away or has no valid)

AGENTS_JSON is broken
  PASS  the lead still gets the office text and an Unassigned item; the run says AGENTS_JSON is the problem  (1. AGENTS_JSON is empty or is not valid JSON, so nobody could be assigned. The l)

LEAD_WEBHOOK_SECRET left empty
  PASS  fails closed: no secret and an empty ?key= are both refused with 401, nothing called  (401, 401, ended at ['Stop: Bad Secret', 'Stop: Bad Secret'])

Every monday request
  PASS  authenticated, JSON, the right API-Version, and valid against the monday GraphQL schema  (92 requests checked)

Every Twilio request
  PASS  accepted by the mock Twilio API, apart from the numbers the test opted out  (38 requests checked)

Every Claude request
  PASS  accepted by the mock Messages API (the real request shape was also checked with count_tokens)  (19 requests checked)

62/62 checks passed in 5.1 minutes
```

## What the workflow sent

These are the requests for one website form lead and its escalation, recorded by the same mocks in a separate one lead run after the final test. The final test asserts the same values field by field (only the n8n run ID in `lead_run` differs from run to run). The 9 second test timer shows up as 0.15 minutes. In the Slack alert, `&amp;` is Slack's escaped `&`; Slack shows it as `&`. All names, numbers and IDs are made up.

**1. monday: duplicate search** (`POST /v2`, `2026-07` API version). Phone digits and the lowercased email, one request

```graphql
query FindLead($board: ID!, $phoneCol: String!, $emailCol: String!, $phone: String!, $email: String!) { by_phone: items_page_by_column_values(board_id: $board, limit: 10, columns: [{ column_id: $phoneCol, column_values: [$phone] }]) { items { id name created_at state url column_values(ids: [$phoneCol, $emailCol]) { id text value } } } by_email: items_page_by_column_values(board_id: $board, limit: 10, columns: [{ column_id: $emailCol, column_values: [$email] }]) { items { id name created_at state url column_values(ids: [$phoneCol, $emailCol]) { id text value } } } }
```

Variables:

```json
{
  "board": "5551234567",
  "phoneCol": "lead_phone",
  "emailCol": "lead_email",
  "phone": "15125550142",
  "email": "jamie.rivera@example.com"
}
```

**2. monday: create the item** (`POST /v2`, `2026-07` API version, with an `Idempotency-Key`). The column values are shown decoded here; they are sent as a JSON string, as monday requires

```graphql
mutation CreateLead($board: ID!, $group: String, $name: String!, $cols: JSON!, $runCol: String!) { create_item(board_id: $board, group_id: $group, item_name: $name, column_values: $cols, create_labels_if_missing: true) { id url column_values(ids: [$runCol]) { id text } } }
```

Variables:

```json
{
  "board": "5551234567",
  "name": "Jamie Rivera",
  "cols": {
    "lead_phone": {
      "phone": "+15125550142",
      "countryShortName": "US"
    },
    "lead_email": {
      "email": "jamie.rivera@example.com",
      "text": "jamie.rivera@example.com"
    },
    "lead_status": {
      "label": "New"
    },
    "lead_agent": "Maya Chen",
    "lead_source": "Website",
    "lead_message": {
      "text": "Hi! We're pre-approved for $480k and want a 3 bed & 2 bath in East Austin. Can we tour this weekend?"
    },
    "lead_run": "7641"
  },
  "runCol": "lead_run",
  "group": "new_leads"
}
```

**3. Twilio: the instant text to the lead** (`POST /2010-04-01/Accounts/{AccountSid}/Messages.json`, basic auth, form encoded)

```
To=+15125550142
From=+15125550100
Body=
Hi Jamie, thanks for reaching out to Acme Realty! Maya Chen will call or text you in the next few minutes. You can also reach Maya directly at (512) 555-0101. Reply STOP to opt out.
```

**4. Claude: classify the lead** (`POST /v1/messages`, `anthropic-version: 2023-06-01`). The message Claude gets (no name, phone or email):

```
Lead source: Website
Message from the lead:
"""
Hi! We're pre-approved for $480k and want a 3 bed & 2 bath in East Austin. Can we tour this weekend?
"""
```

Model `claude-sonnet-5`, `max_tokens` 2048, `output_config` `{"effort": "low"}`, `tool_choice` `{"type": "tool", "name": "classify_lead"}`. The tool's input schema:

```json
{
  "type": "object",
  "properties": {
    "intent": {
      "type": "string",
      "enum": [
        "buyer",
        "seller",
        "renter",
        "investor",
        "unknown"
      ],
      "description": "buyer: wants a home to live in. seller: wants to sell or asks what a home is worth. renter: wants to rent. investor: buys for rental income, flips or several properties."
    },
    "budget": {
      "type": "string",
      "description": "Budget or price range in their words, for example \"$450k to $500k\". Empty string if not given."
    },
    "timeline": {
      "type": "string",
      "enum": [
        "asap",
        "within_3_months",
        "3_to_6_months",
        "over_6_months",
        "unknown"
      ]
    },
    "area": {
      "type": "string",
      "description": "Neighborhood, city or ZIP they mention, or the area of the property they asked about. Empty string if none."
    },
    "pre_approved": {
      "type": "string",
      "enum": [
        "yes",
        "no",
        "unknown"
      ],
      "description": "Mortgage pre-approval or cash. unknown unless they say so."
    },
    "urgency": {
      "type": "integer",
      "minimum": 1,
      "maximum": 5,
      "description": "5: wants to act today or this week (a showing, an offer, a listing appointment). 4: within a month, or pre-approved and ready. 3: within 3 months. 2: 3 to 6 months. 1: just browsing or further out."
    },
    "summary": {
      "type": "string",
      "description": "One plain sentence for the agent, at most 140 characters."
    }
  },
  "required": [
    "intent",
    "budget",
    "timeline",
    "area",
    "pre_approved",
    "urgency",
    "summary"
  ]
}
```

**5. monday: save Claude's details** (`POST /v2`, `2026-07` API version, with an `Idempotency-Key`)

```graphql
mutation SaveDetails($board: ID!, $item: ID!, $cols: JSON!) { change_multiple_column_values(board_id: $board, item_id: $item, column_values: $cols) { id } }
```

Variables:

```json
{
  "board": "5551234567",
  "item": "9100000001",
  "cols": {
    "lead_intent": "Buyer",
    "lead_budget": "$480k",
    "lead_timeline": "ASAP",
    "lead_area": "East Austin",
    "lead_preapproved": "Yes",
    "lead_urgency": "5",
    "lead_summary": "Pre-approved buyer, $480k, wants a 3 bed & 2 bath in East Austin and can tour this weekend.",
    "lead_classified_by": "Claude"
  }
}
```

**6. Twilio: the text to the agent** (`POST /2010-04-01/Accounts/{AccountSid}/Messages.json`, basic auth, form encoded)

```
To=+15125550101
From=+15125550100
Body=
New lead for you: Jamie Rivera
Buyer, urgency 5/5
Budget: $480k
Timeline: ASAP
Area: East Austin
Pre-approved: Yes
"Pre-approved buyer, $480k, wants a 3 bed & 2 bath in East Austin and can tour this weekend."
Phone: (512) 555-0142
Email: jamie.rivera@example.com
Source: Website
Open in monday: https://example-realty.monday.com/boards/5551234567/pulses/9100000001
Update the status in monday within 0.15 min, or the lead goes to the next agent.
```

**7. monday: the escalation reads the status again** (`POST /v2`, `2026-07` API version). Sent by the background run once the timer is up

```graphql
query ReadLead($item: [ID!], $cols: [String!]) { items(ids: $item) { id name state url column_values(ids: $cols) { id text value } } }
```

Variables:

```json
{
  "item": [
    "9100000001"
  ],
  "cols": [
    "lead_status",
    "lead_agent"
  ]
}
```

**8. monday: reassign the lead and add a note** (`POST /v2`, `2026-07` API version, with an `Idempotency-Key`)

```graphql
mutation Reassign($board: ID!, $item: ID!, $cols: JSON!, $note: String!) { change_multiple_column_values(board_id: $board, item_id: $item, column_values: $cols) { id } create_update(item_id: $item, body: $note) { id } }
```

Variables:

```json
{
  "board": "5551234567",
  "item": "9100000001",
  "cols": {
    "lead_agent": "Leo Park"
  },
  "note": "Still New after 0.15 minutes, so this lead moved from Maya Chen to Leo Park."
}
```

**9. Twilio: the text to the next agent** (`POST /2010-04-01/Accounts/{AccountSid}/Messages.json`, basic auth, form encoded)

```
To=+15125550102
From=+15125550100
Body=
Lead reassigned to you: Jamie Rivera
Buyer, urgency 5/5
"Pre-approved buyer, $480k, wants a 3 bed & 2 bath in East Austin and can tour this weekend."
Phone: (512) 555-0142
Email: jamie.rivera@example.com
Maya Chen did not update it in time. Please reach out now.
Open in monday: https://example-realty.monday.com/boards/5551234567/pulses/9100000001
```

**10. Slack: the broker alert** (`POST` to the incoming webhook)

```json
{
  "text": "<@U01BROKER01> :warning: *Lead not picked up within 0.15 minutes*\nJamie Rivera: Buyer, urgency 5/5, from Website\n> Pre-approved buyer, $480k, wants a 3 bed &amp; 2 bath in East Austin and can tour this weekend.\nMoved from Maya Chen <@U01MAYA0001> to Leo Park <@U01LEO00002>, who was just texted.\nOpen in monday: https://example-realty.monday.com/boards/5551234567/pulses/9100000001"
}
```

## What the test does not cover

- The real services. The mocks are written from the official API references and cover the parts this workflow uses, so a change on monday's, Twilio's or Anthropic's side would not show up here. The Claude request was checked against the real API; the monday and Twilio requests were not, because that needs a live account.
- What Claude actually answers. The mock returns scripted answers, so the quality of the classification depends on the model and the prompt in the Validate Lead node.
- Load. Two copies of one lead at the same moment are tested; many different leads at the same moment are not (see the round robin limitation in the README).
- A real slow monday. The case where monday is still busy with the same key after every retry is simulated by the mock answering 409 three times, not by a real slow request.

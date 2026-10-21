# Setup: monday.com, Twilio, Claude, Slack and n8n

You need: self-hosted n8n reachable over HTTPS, a monday.com account, a Twilio account with an SMS capable number, an Anthropic API key and, optionally, a Slack incoming webhook. Plan on about 45 minutes, plus whatever time Twilio needs to approve your US texting registration.

## 1. The monday.com board

The workflow writes one item per lead. It needs these 15 columns. The IDs on the left are the defaults the workflow uses.

| Column ID | Title (any) | Type | What goes in it |
|---|---|---|---|
| `lead_phone` | Phone | Phone | The lead's number in E.164, with its country. monday refuses a number that does not belong to the country it is saved with, so +1 numbers get US, CA, PR and so on by area code |
| `lead_email` | Email | Email | The lead's email, lowercased |
| `lead_status` | Lead Status | Status | `New` when the lead arrives. Agents change it to anything else when they make contact |
| `lead_agent` | Agent | Text | The agent's name, exactly as in `AGENTS_JSON` |
| `lead_source` | Source | Text | Website, the portal name, or whatever the form sends as `source` |
| `lead_intent` | Intent | Text | Buyer, Seller, Renter, Investor or Unknown |
| `lead_budget` | Budget | Text | In the lead's words, for example `$480k` |
| `lead_timeline` | Timeline | Text | ASAP, Within 3 months, 3 to 6 months, Over 6 months or Unknown |
| `lead_area` | Area | Text | Neighborhood, city or ZIP |
| `lead_preapproved` | Pre-approved | Text | Yes, No or Unknown |
| `lead_urgency` | Urgency | Numbers | 1 to 5 |
| `lead_summary` | AI Summary | Text | Claude's one line summary |
| `lead_message` | Message | Long text | The lead's message plus any form fields |
| `lead_classified_by` | Classified By | Text | `Claude`, or `Rules (Claude unavailable)` when the fallback was used |
| `lead_run` | n8n Run | Text | The n8n execution ID. The workflow uses it to tell its own retry apart from a second copy of the same lead |

**The quick way: create the columns through the API.** Column IDs you pick yourself can only be set through the API (the board UI generates random ones). Create an empty board, copy its ID from the URL (`https://yourteam.monday.com/boards/1234567890`), open the API playground (profile picture, **Developers**, then **API playground**) and run this once, with your board ID in place of `1234567890`:

```graphql
mutation {
  phone: create_column(board_id: 1234567890, id: "lead_phone", title: "Phone", column_type: phone) { id }
  email: create_column(board_id: 1234567890, id: "lead_email", title: "Email", column_type: email) { id }
  status: create_column(board_id: 1234567890, id: "lead_status", title: "Lead Status", column_type: status) { id }
  agent: create_column(board_id: 1234567890, id: "lead_agent", title: "Agent", column_type: text) { id }
  source: create_column(board_id: 1234567890, id: "lead_source", title: "Source", column_type: text) { id }
  intent: create_column(board_id: 1234567890, id: "lead_intent", title: "Intent", column_type: text) { id }
  budget: create_column(board_id: 1234567890, id: "lead_budget", title: "Budget", column_type: text) { id }
  timeline: create_column(board_id: 1234567890, id: "lead_timeline", title: "Timeline", column_type: text) { id }
  area: create_column(board_id: 1234567890, id: "lead_area", title: "Area", column_type: text) { id }
  preapproved: create_column(board_id: 1234567890, id: "lead_preapproved", title: "Pre-approved", column_type: text) { id }
  urgency: create_column(board_id: 1234567890, id: "lead_urgency", title: "Urgency", column_type: numbers) { id }
  summary: create_column(board_id: 1234567890, id: "lead_summary", title: "AI Summary", column_type: text) { id }
  message: create_column(board_id: 1234567890, id: "lead_message", title: "Message", column_type: long_text) { id }
  classified: create_column(board_id: 1234567890, id: "lead_classified_by", title: "Classified By", column_type: text) { id }
  run: create_column(board_id: 1234567890, id: "lead_run", title: "n8n Run", column_type: text) { id }
}
```

monday never reuses a column ID, even after the column is deleted. If you delete one of these columns and make it again, give it a new ID and map it in `MONDAY_COLUMNS`.

**The other way: use an existing board.** Add the missing columns in the board UI, then find each column's ID: click your profile picture, open **monday.labs**, turn on **Developer mode**, and the dropdown menu next to each column title shows its ID (in the browser, not the desktop or mobile app). Map the ones that differ from the defaults:

```
MONDAY_COLUMNS={"phone": "phone_mkr1abcd", "status": "status", "agent": "text_mkr2efgh"}
```

The keys are `phone`, `email`, `status`, `agent`, `source`, `intent`, `budget`, `timeline`, `area`, `preapproved`, `urgency`, `summary`, `message`, `classifiedBy` and `run`. Any key you leave out keeps its default ID.

**Status labels.** Open the status column's label editor and add `New` plus the labels your agents will use, such as `Contacted`, `Showing booked` and `Not a fit`. If `New` is missing, the workflow creates it on the first lead. Any status other than `New` tells the workflow an agent is on it.

**Group (optional).** To put new leads into a specific group, set `MONDAY_GROUP_ID` to the group's ID. Leave it empty for the board's top group.

**Token.** Click your profile picture, then **Developers**, then **My access tokens**, and copy the token into `MONDAY_API_TOKEN`. The token acts as you, so the account needs access to the board. `MONDAY_BOARD_ID` is the number in the board URL.

## 2. Twilio

1. In the [Twilio Console](https://console.twilio.com), copy the **Account SID** and **Auth Token** from Account Info into `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`.
2. Buy a phone number with SMS (Phone Numbers, Manage, Buy a number) and put it in `TWILIO_FROM_NUMBER` in E.164 (`+15125550100`).
3. **Texting US numbers:** carriers block texts from unregistered 10 digit numbers. Register for A2P 10DLC (Messaging, Regulatory Compliance) and attach the number to a Messaging Service, then put the Messaging Service SID (starts with `MG`) in `TWILIO_MESSAGING_SERVICE_SID`. When it is set, the workflow sends through the Messaging Service instead of the single number. A verified toll free number also works.
4. Twilio handles STOP, START and HELP replies on its own. A lead who texted STOP cannot be texted again, and the workflow treats that refusal (error 21610) as a failed text: the agent is told to call instead.

While your account is in trial mode, Twilio only delivers to numbers you verified in the console, and every text starts with a trial notice.

## 3. Claude

Create a key at [console.anthropic.com](https://console.anthropic.com) under API keys and put it in `ANTHROPIC_API_KEY`. The default model is `claude-sonnet-5`, asked at low effort so its thinking stays short and the agent's text is not held up. `claude-haiku-4-5` is cheaper and `claude-opus-5` also works; set `ANTHROPIC_MODEL` to switch (the workflow leaves the effort setting out for Haiku, which does not have one). Do not use `claude-opus-5-5` or `claude-fable-5-1` here: they refuse the forced tool call the workflow relies on, so every lead would fall back to the keyword rules. Claude only gets the lead's message, the source and any form fields, never the name, phone or email.

## 4. Slack (optional)

1. At [api.slack.com/apps](https://api.slack.com/apps), create an app for your workspace, open **Incoming Webhooks**, turn it on and click **Add New Webhook to Workspace**. Pick the channel the broker watches. Copy the URL into `SLACK_WEBHOOK_URL`.
2. For mentions, get each person's Slack member ID: open their profile, click the three dots, **Copy member ID**. The broker's goes in `BROKER_SLACK_ID`, the agents' go in `AGENTS_JSON`.

The same webhook URL is used for escalation alerts and for the error workflow's failure alerts. Without it, escalations still reassign the lead and text the next agent; nobody gets a Slack message.

## 5. The agents

`AGENTS_JSON` is one line of JSON, a list of agents in round robin order:

```
AGENTS_JSON=[{"name": "Maya Chen", "phone": "+15125550101", "slack": "U0123MAYA", "email": "maya@example.com", "away": false}, {"name": "Leo Park", "phone": "+15125550102", "slack": "U0123LEO", "email": "leo@example.com", "away": false}]
```

- `name` is what goes in the Agent column and in the lead's text, so use the name the lead should see.
- `phone` must be E.164. An agent without a valid phone is skipped, and the run is flagged so you notice.
- `slack` is optional, for mentions in the broker alert. `email` is stored for reference only.
- `away: true` takes an agent out of the rotation (vacation, day off). The escalation check reads the list again when it runs, so marking someone away also stops them from getting reassigned leads.

n8n reads its environment variables when it starts, so restart n8n (or the container) after changing `AGENTS_JSON`. Escalation timers that are already waiting survive the restart and use the new list when they fire.

## 6. n8n

1. Add the variables from [.env.example](../.env.example) to your n8n environment and restart n8n. Self-hosted n8n needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` so nodes can read `$env`. (n8n Cloud does not allow `$env`. There, replace `$env.` with `$vars.` in the workflow and create the same names under Variables.)
2. Import `workflow/error-alert-slack.json` and **activate it**. n8n 2.x only runs an error workflow that is active; an inactive one is skipped and no alert is sent.
3. Import `workflow/real-estate-speed-to-lead.json`. In its workflow settings, choose the error alert workflow as the **Error Workflow**.
4. Activate the lead workflow. Leads only reach the production URL of an active workflow, and n8n only saves the round robin pointer on production runs.

Your lead URL is `https://YOUR-N8N-DOMAIN/webhook/real-estate-lead`.

## 7. Connect the form or portal

Send each lead as a POST with a JSON body (or a regular form post), and the secret in the `X-Lead-Secret` header:

```bash
curl -X POST https://YOUR-N8N-DOMAIN/webhook/real-estate-lead \
  -H "Content-Type: application/json" \
  -H "X-Lead-Secret: your-secret" \
  -d '{"firstName": "Jamie", "lastName": "Rivera", "phone": "(512) 555-0142", "email": "jamie.rivera@example.com", "message": "Pre-approved for $480k, looking for a 3 bed in East Austin. Can we tour this weekend?", "source": "Website"}'
```

If your form tool cannot set headers, add the secret to the URL instead: `.../webhook/real-estate-lead?key=your-secret`. That works, but the URL (with the secret) can end up in logs, so prefer the header when you can.

The workflow looks for the usual field names at the top level and inside `contact`, `lead`, `lead.contact`, `data` and `fields`:

| Detail | Field names it accepts |
|---|---|
| Name | `name`, `fullName`, `full_name`, `contactName`, or `firstName` plus `lastName` (and the snake case versions) |
| Phone | `phone`, `phoneNumber`, `phone_number`, `mobile`, `mobilePhone`, `cell`, `cellPhone`, `tel`, `telephone` |
| Email | `email`, `emailAddress`, `email_address` |
| Message | `message`, `comments`, `notes`, `inquiry`, `enquiry`, `question`, `details` |
| Source | `source`, `leadSource`, `portal`, `origin` (default `Website`) |
| Property | `property`, `propertyAddress`, `listingAddress`, `listing.address`, `address` |
| Optional form fields | `intent` or `leadType`, `budget` or `priceRange`, `timeline` or `timeframe`, `area` or `city` or `zip`, `preApproved` or `financing` |

The webhook answers:

| Answer | Meaning |
|---|---|
| `202 {"ok": true, "status": "accepted"}` | The lead is being routed. Repeats of a recent lead also get 202, so your form never retries them |
| `400` | No valid phone number or email in the request |
| `401` | The secret is missing or wrong |

Most listing portals deliver leads by email or through their own CRM integrations rather than a webhook. For those, put a small Zapier, Make or email parser step in front that posts the fields above to this URL.

## 8. Try it

Send the curl above with your own phone number. Within a few seconds you should get the text from the office, the first agent gets the alert with the monday link, and the item appears on the board with status New. Leave the status alone and, `ESCALATION_MINUTES` later, the lead moves to the next agent and the broker gets the Slack alert.

## Troubleshooting

If something does not happen, open the workflow's **Executions** list in n8n:

| You see | Likely cause |
|---|---|
| No execution at all | The workflow is not active, or the form posts to the wrong URL |
| Run stopped at "Stop: Bad Secret" | The form does not send `X-Lead-Secret`, or the value differs from `LEAD_WEBHOOK_SECRET` (with `LEAD_WEBHOOK_SECRET` empty, every lead stops here) |
| Run stopped at "Stop: Not Routed" | monday was still busy saving the same submission after three tries, usually because the form sent it twice while monday was slow. Check the board: if the lead is missing or nobody texted them, route it by hand |
| Run ended at "Respond: 400" | The phone number could not be read and there is no email. Check `DEFAULT_COUNTRY` for numbers without a country code |
| Flagged: "The lead was NOT saved in monday" | `MONDAY_API_TOKEN`, `MONDAY_BOARD_ID`, a column ID, or `MONDAY_GROUP_ID` is wrong. The error text names the column |
| Flagged: "The instant text to the lead failed" | Check the Twilio credentials, the From number or Messaging Service, and A2P registration. Error 21610 means the lead opted out |
| Flagged: "Claude did not answer" or "answered without the classify_lead tool" | Check `ANTHROPIC_API_KEY`, your credit balance and `ANTHROPIC_MODEL` (see section 3 for the models that work). The lead was still routed with keyword rules |
| Flagged: "No agent is available" or "AGENTS_JSON is empty or is not valid JSON" | Everyone is marked away, or the JSON has a typo (paste it into a JSON validator) |
| No escalation run after the timer | The workflow was not active when the lead came in, or the agent changed the status in time (that run ends at "Still New?") |
| Errors never reach Slack | The error alert workflow is not active, or it is not selected as the Error Workflow |

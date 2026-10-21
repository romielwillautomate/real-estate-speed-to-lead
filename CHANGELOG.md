# Changelog

## v1 (2026-10-21)

First build, tested end to end on n8n 2.19.5 against mock monday.com, Twilio, Claude and Slack APIs (32 scenarios and 62 checks after the review below).

### What is in it

- A lead webhook that accepts JSON or form posts, checks a shared secret (`X-Lead-Secret` header or `?key=`) in constant time and rejects everything when no secret is configured.
- Phone numbers normalized to E.164 (with a default country for national numbers) and emails lowercased. Field names are read from the top level and from the usual nested objects (`contact`, `lead`, `data`, `fields`).
- Duplicate check in monday by phone and email in one GraphQL query. The same lead within `DEDUPE_HOURS` gets a note (and any new phone or email) on its existing item, with no new routing and no texts.
- monday's `Idempotency-Key` header on every write, so two copies of one submission (a double click, a portal retry) create one item and one set of texts, even when they arrive at the same moment.
- Round robin across `AGENTS_JSON`, skipping agents marked away or without a valid phone. The pointer only moves when a lead is really routed.
- An instant Twilio text to the lead with the agent's name, sent before Claude is called.
- Claude classification through a forced tool call: intent, budget, timeline, area, pre-approval, urgency 1 to 5 and a one line summary. Claude never sees the lead's name, phone or email. If Claude fails, keyword rules fill in, and the item and the agent text say so.
- A text to the agent with the summary, the contact details and the monday link.
- Escalation as a separate background run: after `ESCALATION_MINUTES`, a lead still at status New with the same agent moves to the next available agent, who gets a text, and the broker gets a Slack alert. If the agent changed the status or someone reassigned it by hand, nothing happens.
- Quiet hours (`QUIET_HOURS` in `TIMEZONE`): the lead still gets the instant text, worded as a morning follow up, and the escalation waits until quiet hours end.
- Failure handling: retries on every API call, monday errors that come back with HTTP 200 detected, and one summary of everything that went wrong per run, raised through the error alert workflow after the lead and the agent were already texted.
- Sticky notes on the canvas, the error alert workflow, setup guide and an end to end mock test.

### Found while testing

- n8n's executions list leaves out runs it still tracks as active, so a parked escalation run did not show up there while it waited. The test now fetches the parked run by the ID the intake run recorded.
- An `AGENTS_JSON` value with a typo was reported as "everyone is marked away". It now has its own message that names the variable.
- Claude Sonnet 5 can think before it answers even when the tool call is forced, and thinking counts toward `max_tokens`. The limit went from 1,024 to 2,048 tokens, so a long thought does not cut off the tool call (only the tokens used are billed).

### Fixed in review

A second pass checked every node and every API call against the official docs, then made the mocks stricter and added the failure paths the README described but nothing tested. The three bugs below were confirmed by running the new checks against the earlier workflow, where all three failed.

- **A lead could be lost without a trace.** When monday answered every create try with 409 `IDEMPOTENCY_CONFLICT`, the run ended as a success at "Route Lead?": nobody was texted and nobody was told. That happens when a slow monday is still saving this run's own first try, or another copy of the lead. The run now stops at the new "Stop: Not Routed" node, and the alert names the lead so someone can check the board.
- **+1 numbers outside the US failed the monday save.** Every +1 number was saved with country US (or CA when `DEFAULT_COUNTRY=CA`), and monday checks the number against its country with libphonenumber. A Canadian lead at a US office, or a US lead at a Canadian office, was refused. The country now comes from the area code (US, Canada, Puerto Rico and the other +1 countries).
- **Lead text could ping the whole Slack channel.** The name, source and summary went into Slack unescaped, so a lead named `<!channel>` would notify everyone. Text that came from the lead is now escaped the way Slack documents (`&`, `<`, `>`), in the broker alert and in the error alerts. Emails with `<`, `>` or quotes are no longer accepted as valid.
- Claude is now asked at low effort (`output_config.effort`), which keeps Sonnet 5's thinking short so the agent's text is not held up. Haiku 4.5 has no effort setting, so the field is left out for it. Both requests were checked on Anthropic's token counting endpoint, which also showed that `claude-opus-5-5` and `claude-fable-5-1` refuse the forced tool call. The docs now say so.
- `DEDUPE_HOURS=0` and `ESCALATION_MINUTES=0` used to fall back to 72 hours and 5 minutes. They now go to the documented minimums.
- The escalation's "could not read the status" message is built in the Check Status node and escaped for Slack.
- Test: checks for "no timer started" read the nodes each run executed. The executions list leaves out waiting runs, so the old check would have passed even with a stray timer. Error alerts are matched to their run by the execution link. The 400 answer is checked word for word. Any monday request the real API would reject (auth, content type, version, parse or schema error) now fails the test, and every Twilio and Claude request is checked too.
- Test: new scenarios for a 409 on every try, a Claude refusal, a phone that cannot be read next to a valid email, a refused agent text, a failed reassignment (with Slack markup in the lead's source), a failed next agent text together with Slack refusing the broker alert, a Canadian number, and an empty `LEAD_WEBHOOK_SECRET`.

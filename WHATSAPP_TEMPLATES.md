# WhatsApp templates to submit in Meta Business Manager

Free text works only within 24 hours of the recipient's last message to the bot. A template
message does not reopen that window; only a reply from the person does. Voice rules: plain,
no emojis, no exclamation marks. Language code is `en` (the code sends `en`; if you create the
templates as English (South Africa) the code must send `en_ZA` instead).

Meta rules these drafts follow: a body cannot start or end with a variable, variable values
cannot contain line breaks, a body is at most 1,024 characters, and every variable needs a
sample at submission. Category for every template below: **Utility** (it is about a job the
person is already part of). Meta may reclassify `pf_announcement` as Marketing; see the note there.

## Which messages leave the window

| Message | Sent when | Window |
|---|---|---|
| Approval message | You send `/approve` | Outside if more than 24h since they signed up |
| Job offer | You tap an offer button | Outside, unless the plumber wrote in the last 24h |
| "Taken by another plumber" | A different plumber takes it | Outside for plumbers who have not replied |
| Job-close prompt | 24h after "take" | Always outside |
| Customer confirmed notice | Day 4 | Outside |
| Open-jobs pattern | 3 unresolved jobs | Outside |
| Credits added | You send `/credit C-0001` or `/grant` | Outside if they paid more than 24h after asking |
| Credit returned | A job ends with no charge | Outside if it is settled days later |
| BROADCAST | Admin command | Outside |
| Customer "sound right?" with Yes / Something's off buttons | 2h after "done" | Outside, unless the customer wrote in the last 24h |
| Customer "did he get it sorted?" | 24h after "take" | Outside |

Inside the window (no template needed): the "you've got it" confirmation, the amount question
and its replies, the "no go" acknowledgement, the BUY menu and order details, the name question,
the "out of credits" reply, every reply to a customer's answer, STATUS, and every command reply.

Weekly invoices, payment reminders and "leads paused" are no longer sent for new jobs (leads are
paid with credits). They only apply if a full-fee job ever happens, so they are not drafted here.

## Path A: one catch-all template (the stopgap)

`CloudMessenger` falls back to a single template with the whole message as `{{1}}` (line breaks
become spaces) when Meta refuses free text. Set `WA_FALLBACK_TEMPLATE=plumber_message` once approved.

**plumber_message** · Utility
```
PlumberFlow message for you. {{1}} Reply here if you need to.
```
Sample: `New job — Randburg. Burst pipe. 1 plumber viewing. Uses 1 credit, you have 4. Reply take to claim it. L-0412`

Limits: Meta often rejects or reclassifies catch-all templates, and every message arrives as one
unbroken paragraph. It does not cover the customer Yes / Something's off buttons. Treat it as a
stopgap for any message that has no template of its own.

## Path B: one template per message (recommended; the code sends these)

**pf_approved**
```
You're approved on PlumberFlow, {{1}}. {{2}} When a job comes in, reply take to claim it. The first plumber to reply gets it. Send done when it is finished, or no go if it falls through. Reply HELP for all commands.
```
Samples: `Sipho` · `You have 2 free trial leads, no fee on those.`

**pf_job_offer**
```
New job — {{1}}
{{2}}

{{3}} viewing
{{4}}

Reply take to claim it. Job code {{5}}, first reply wins.
```
Samples: `Randburg` · `Burst pipe · Emergency — right now` · `1 plumber` · `Uses 1 credit, you have 4` · `L-0412`
({{4}} is `Trial lead, no fee` for a trial lead, or `Uses 1 credit, you have none, reply BUY to top up`.)

**pf_job_taken_elsewhere**
```
Update: {{1}} has been taken by another plumber.
```
Sample: `Randburg burst pipe`

**pf_job_close_prompt**
```
Job check for {{1}}: the {{2}} {{3}} job is still open on my side.

Done? Send "done" and what you charged.

If it fell through, send "no go". Nothing owed.
```
Samples: `Sipho` · `Randburg` · `burst pipe`

**pf_customer_confirmed**
```
Job update for {{1}}: {{2}} confirmed the {{3}} was sorted on {{4}}.

{{5}}

If that's wrong, reply wrong now and I'll pull it.
```
Samples: `Sipho` · `Thandi` · `burst pipe` · `Tuesday` · `Nothing owed, no fee on this one.`

**pf_open_jobs_pattern**
```
Open jobs for {{1}}: {{2}} jobs have no update from you.

{{3}}

Done, or no go? Reply like this: 1 done R2400, 2 no go

Leads pause until these are closed off.
```
Samples: `Sipho` · `3` · `1. Randburg burst pipe · Tuesday | 2. Fourways leak · Wednesday | 3. Randburg geyser · Thursday`

**pf_credits_added**
```
Payment received, {{1}}. {{2}} credits have been added to your account and your balance is now {{3}}. Each lead you take uses one credit.
```
Samples: `Sipho` · `5` · `6`

**pf_credit_returned**
```
Credit returned for {{1}}: the {{2}} job did not go ahead, so your credit is back. Your balance is now {{3}}.
```
Samples: `Sipho` · `Randburg burst pipe` · `4`

**pf_announcement** (BROADCAST)
```
PlumberFlow announcement: {{1}} Questions? Reply here.
```
Sample: `Packs of 5 and 10 credits are now 5% and 10% cheaper.`
Note: Meta may reclassify this one as Marketing, which is billed higher. Keep announcements about
how the service works for plumbers who are already using it.

**pf_confirm_logged** (customer) · two quick-reply buttons: `Yes` and `Something's off`
```
Quick check, {{1}}. {{2}} logged the {{3}} job at {{4}}. Sound right?
```
Samples: `Thandi` · `Sipho` · `burst pipe` · `R2,400`

The bot attaches the button payloads itself (`<job id>|yes` and `<job id>|off`); you only create the
two button labels in Meta.

**pf_did_it_get_sorted** (customer)
```
Job check, {{1}}. Did {{2}} get the {{3}} sorted?

One word is fine.
```
Samples: `Thandi` · `Sipho` · `burst pipe`

## How the code uses them

Free text is always tried first. If Meta refuses it (the 24-hour window has closed), the message is
sent as its own template above, with the values filled in. If that template is not approved yet, the
catch-all `plumber_message` is used when `WA_FALLBACK_TEMPLATE` is set; otherwise the send fails and
is logged, as before. So submitting templates one at a time is safe: each starts working as soon as
Meta approves it, with no deploy.

- Template language: `en` by default. If you create them as English (South Africa), set the Fly
  secret `WA_TEMPLATE_LANG=en_ZA`.
- The customer *Yes* / *Something's off* message goes as `pf_confirm_logged` with two quick-reply
  buttons. When the customer taps one, the bot reads it like the normal button.
- When you create `pf_confirm_logged`, add exactly two **Quick reply** buttons, in this order:
  `Yes`, then `Something's off`. Do not add other buttons.
- The messages sent when you run `/approve`, `/credit` and `/grant` use the same fallback.
- A test checks that each template's number of values matches this file, so the two cannot drift.

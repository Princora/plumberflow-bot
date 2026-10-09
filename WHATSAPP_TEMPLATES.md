# WhatsApp templates to submit in Meta Business Manager

Free text works only within 24 hours of the recipient's last message to the bot. A template
message does not reopen that window; only a reply from the person does. Voice rules: plain,
no emojis, no exclamation marks. Language code is `en` (the code sends `en`; if you create
the templates as English (South Africa) the code must send `en_ZA` instead).

Meta rules these drafts follow: a body cannot start or end with a variable, variable values
cannot contain line breaks, and every variable needs a sample at submission.

## Which messages leave the window

| Message | Sent when | Window |
|---|---|---|
| Job offer (A1) | You tap an offer button | Outside, unless the plumber wrote in the last 24h |
| "Taken by another plumber" | A different plumber takes it | Outside for plumbers who have not replied |
| Job-close prompt (A3) | 24h after "take" | Always outside (the "take" was 24h ago) |
| Customer confirmed notice (A7) | Day 4 | Outside |
| Open-jobs pattern (A9) | 3 unresolved jobs | Outside |
| Weekly invoice and zero week (A10) | Friday 17:00 SAST | Outside |
| Overdue reminder (A11), leads paused (A12) | Day after due, Thursday | Outside |
| Payment received | You mark an invoice paid | Outside |
| BROADCAST | Admin command | Outside |
| B1 "sound right?" with Yes / Something's off buttons | 2h after "done" | Outside, unless the customer wrote in the last 24h |
| B2 "did he get it sorted?" | 24h after "take" | Outside |

Inside the window (no template needed): the "you've got it" confirmation, the amount
question and its replies, the "no go" acknowledgement, every reply to a customer's answer
(B3 to B6), the lead-logged message, STATUS, and every command reply.

## Path A: one catch-all template (works with the code today)

`CloudMessenger` already falls back to a single template with the whole message as `{{1}}`
(line breaks become spaces). Set `WA_FALLBACK_TEMPLATE=plumber_message` once approved.

**plumber_message** · Utility
```
PlumberFlow message for you. {{1}} Reply here if you need to.
```
Sample: `New job — Randburg. Burst pipe. Free to take. R220 due after the job is done. Reply take to claim it. L-0412`

Risk: Meta often rejects or reclassifies catch-all templates, and every message arrives as one
unbroken paragraph. It also does not cover the B1 buttons. Treat it as a stopgap.

## Path B: one template per message (recommended; needs a small code change)

Each needs `CloudMessenger` to send named parameters instead of the whole text. I will wire
this once you confirm the names. Category for all of these: Utility.

**pf_job_offer**
```
New job — {{1}}
{{2}}

Free to take. Plumbers viewing: {{3}}
R220 due after the job is done

Reply take to claim it. Job code {{4}}, first reply wins.
```
Samples: `Randburg` · `Burst pipe · Emergency — right now` · `1` · `L-0412`

**pf_job_taken_elsewhere**
```
Update: {{1}} has been taken by another plumber.
```
Sample: `Randburg burst pipe`

**pf_job_close_prompt** (A3)
```
Job check for {{1}}: the {{2}} {{3}} job is still open on my side.

Done? Send "done" and what you charged.

If it fell through, send "no go". Nothing owed.
```
Samples: `Sipho` · `Randburg` · `burst pipe`

**pf_customer_confirmed** (A7)
```
Job update for {{1}}: {{2}} confirmed the {{3}} was sorted on {{4}}.

{{5}} on this week's invoice.

If that's wrong, reply "wrong" now and I'll pull it.
```
Samples: `Sipho` · `Thandi` · `burst pipe` · `Tuesday` · `R220`

**pf_open_jobs_pattern** (A9)
```
Open jobs for {{1}}: {{2}} jobs have no update from you.

{{3}}

Done, or no go? Reply like this: 1 done R2400, 2 no go

Leads pause until these are closed off.
```
Samples: `Sipho` · `3` · `1. Randburg burst pipe · Tuesday | 2. Fourways leak · Wednesday | 3. Randburg geyser · Thursday`

**pf_weekly_invoice** (A10)
```
Weekly invoice for {{1}}, week of {{2}}.

{{3}}

Total due: {{4}}

PayShap to {{5}}. Due Monday end-of-day.
```
Samples: `Sipho` · `5–9 Oct` · `2 jobs completed: Randburg burst pipe · Tuesday · R220 | Fourways leak · Thursday · R220` · `R440` · `082 000 0000`

**pf_zero_week** (A10, no jobs)
```
Weekly summary for {{1}}, week of {{2}}.

No completed jobs. Nothing owed.
```
Samples: `Sipho` · `5–9 Oct`

**pf_payment_reminder** (A11)
```
Payment reminder for {{1}}: {{2}} from last week is still outstanding.

Leads pause Thursday if it's not settled. Not a threat, just how it works.
```
Samples: `Sipho` · `R440`

**pf_leads_paused** (A12)
```
Leads paused for {{1}}. {{2}} outstanding.

Settle and you're back on same day. Nothing else changes.
```
Samples: `Sipho` · `R440`

**pf_payment_received**
```
Payment received for {{1}}: {{2}} cleared. Thanks.
```
**pf_payment_received_restored**
```
Payment received for {{1}}: {{2}} cleared. Leads are back on.
```
Samples: `Sipho` · `R440`

**pf_announcement** (BROADCAST)
```
PlumberFlow announcement: {{1}} Questions? Reply here.
```
Sample: `Fees stay at R220 per job through December.`

**pf_confirm_logged** (B1) · two quick-reply buttons: `Yes` and `Something's off`
```
Quick check, {{1}}. {{2}} logged the {{3}} job at {{4}}. Sound right?
```
Samples: `Thandi` · `Sipho` · `burst pipe` · `R2,400`

Button payloads must be `<job id>|yes` and `<job id>|off`. A tap on a template button
arrives as `type: "button"` with `button.payload`, not `interactive.button_reply`, and the
dispatch router ignores that shape today. Both the sending and the receiving side need code.

**pf_did_it_get_sorted** (B2)
```
Job check, {{1}}. Did {{2}} get the {{3}} sorted?

One word is fine.
```
Samples: `Thandi` · `Sipho` · `burst pipe`

-- New plumbers wait for approval before they are suggested for leads.
--
-- A hold keeps a plumber out of the lead suggestions while he can still reply to the
-- bot, send "done" / "no go" and be invoiced. A plumber who finishes WhatsApp onboarding
-- gets the 'pending_approval' hold; the founder clears it with /approve in Telegram.
--
-- This also allows 'self_paused' (the PAUSE / RESUME commands), so it supersedes 003:
-- apply 004 on its own, or 003 then 004, with the same result. Safe to re-run.

alter table plumber_holds drop constraint if exists plumber_holds_reason_check;
alter table plumber_holds add constraint plumber_holds_reason_check
  check (reason in ('unresolved_jobs', 'unpaid_invoice', 'self_paused', 'pending_approval'));

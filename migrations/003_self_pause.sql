-- Lets a plumber pause himself (the PAUSE / RESUME commands) without being switched
-- off entirely. plumbers.active = false would also stop his "done" and "no go" replies
-- being recognised and skip his Friday invoice, so a self-pause is a hold instead.
-- The dispatch service already warns before offering a lead to a held plumber.
-- Not applied anywhere yet.

alter table plumber_holds drop constraint if exists plumber_holds_reason_check;
alter table plumber_holds add constraint plumber_holds_reason_check
  check (reason in ('unresolved_jobs', 'unpaid_invoice', 'self_paused'));

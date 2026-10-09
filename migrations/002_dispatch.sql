-- PlumberFlow dispatch: leads you route by hand from Telegram, and the offers sent to plumbers.
-- Run after 001. Names are prefixed so they cannot collide with a leads table you port later.
-- job_lifecycle.lead_id holds dispatch_leads.ref (text), so the two join on that.

create sequence if not exists dispatch_lead_seq start 1;

create table if not exists dispatch_leads (
  id                   uuid primary key default gen_random_uuid(),
  ref                  text not null unique,              -- "L-0412", what plumbers and you see
  customer_phone       text not null,
  customer_name        text,
  suburb               text,
  keyword              text,
  address              text,                              -- withheld from plumbers until they take it
  summary              text,
  status               text not null default 'open'
                         check (status in ('open', 'offered', 'taken', 'skipped')),
  taken_by             text,
  taken_at             timestamptz,
  telegram_message_id  bigint,
  no_taker_alert_at    timestamptz,
  created_at           timestamptz not null default now()
);

create table if not exists dispatch_offers (
  id          uuid primary key default gen_random_uuid(),
  lead_ref    text not null references dispatch_leads (ref),
  plumber_id  text not null,
  status      text not null default 'offered' check (status in ('offered', 'taken', 'lost')),
  offered_at  timestamptz not null default now(),
  unique (lead_ref, plumber_id)
);
create index if not exists dispatch_offers_plumber_idx on dispatch_offers (plumber_id, status);

alter table dispatch_leads  enable row level security;
alter table dispatch_offers enable row level security;

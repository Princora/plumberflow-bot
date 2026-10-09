-- PlumberFlow completion verification: job lifecycle schema
-- Review before applying. Layers on top of your existing leads/plumbers tables.
-- lead_id and plumber_id are text so this applies whether your ids are uuid, bigint
-- or anything else. Once you confirm your real id types, tighten them to match and
-- add foreign keys (leads -> job_lifecycle.lead_id, plumbers -> *.plumber_id).

-- gen_random_uuid() is built in on Postgres 13+ (Neon, Supabase), no extension needed.

-- One row per accepted lead. Raw facts only; the outcome is derived by resolve().
create table if not exists job_lifecycle (
  id                  uuid primary key default gen_random_uuid(),
  lead_id             text not null unique,
  plumber_id          text not null,
  customer_phone      text not null,
  customer_name       text,
  suburb              text,
  job_keyword         text,

  accepted_at         timestamptz not null default now(),

  -- plumber side
  plumber_claim       text check (plumber_claim in ('done', 'no_go')),
  plumber_claim_at    timestamptz,
  job_value_cents     integer check (job_value_cents is null or job_value_cents > 0),
  no_go_reason        text,

  -- customer side
  customer_response   text check (customer_response in
                        ('yes', 'off', 'off_price', 'never_came', 'not_sorted')),
  customer_response_at timestamptz,

  -- lifecycle
  status              text not null default 'accepted' check (status in
                        ('accepted', 'awaiting_customer', 'charged', 'no_charge',
                         'unresolved', 'disputed', 'void')),
  fee_cents           integer not null default 22000,
  flags               text[] not null default '{}',
  charged_at          timestamptz,
  resolved_at         timestamptz,
  invoice_id          uuid,

  -- idempotency markers: each message fires at most once per job
  a3_sent_at          timestamptz,
  b1_sent_at          timestamptz,
  b2_sent_at          timestamptz,
  a7_sent_at          timestamptz,

  created_at          timestamptz not null default now()
);

create index if not exists job_lifecycle_plumber_status_idx
  on job_lifecycle (plumber_id, status);
create index if not exists job_lifecycle_customer_idx
  on job_lifecycle (customer_phone, accepted_at desc);
create index if not exists job_lifecycle_open_idx
  on job_lifecycle (accepted_at)
  where status in ('accepted', 'awaiting_customer');

-- Weekly invoices. One per plumber per week; 'zero' rows dedupe the no-jobs message.
create table if not exists invoices (
  id                  uuid primary key default gen_random_uuid(),
  plumber_id          text not null,
  week_start          date not null,
  job_count           integer not null default 0,
  total_cents         integer not null default 0,
  status              text not null default 'sent' check (status in ('sent', 'paid', 'zero')),
  sent_at             timestamptz,
  due_at              timestamptz,
  reminded_at         timestamptz,
  paused_at           timestamptz,
  paid_at             timestamptz,
  unique (plumber_id, week_start)
);

-- Conversation state for multi-step plumber replies ("done" -> "what did you charge?").
create table if not exists plumber_state (
  plumber_id          text primary key,
  step                text not null,
  job_id              uuid,
  options             jsonb,
  tries               integer not null default 0,
  updated_at          timestamptz not null default now()
);

-- Lead-routing pauses. Your router must skip any plumber with a row here.
create table if not exists plumber_holds (
  plumber_id          text not null,
  reason              text not null check (reason in ('unresolved_jobs', 'unpaid_invoice')),
  created_at          timestamptz not null default now(),
  primary key (plumber_id, reason)
);

-- Append-only audit trail. Doubles as the fraud-signal source.
create table if not exists job_events (
  id                  bigint generated always as identity primary key,
  job_id              uuid,
  plumber_id          text,
  kind                text not null,
  payload             jsonb,
  created_at          timestamptz not null default now()
);
create index if not exists job_events_plumber_kind_idx on job_events (plumber_id, kind);

-- Service-role only. The bot uses the service key; nothing here should be
-- reachable with the anon key.
alter table job_lifecycle  enable row level security;
alter table invoices       enable row level security;
alter table plumber_state  enable row level security;
alter table plumber_holds  enable row level security;
alter table job_events     enable row level security;

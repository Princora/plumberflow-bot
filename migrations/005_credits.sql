-- Lead credits: after the free trial a plumber buys credits and each lead they take uses one.
--
-- plumbers.credits is the balance. credit_orders holds the packs plumbers ask to buy
-- (paid by EFT, confirmed by the founder with /credit <ref>); an order becomes 'paid' once,
-- and that same statement adds the credits, so a double confirmation can never add them twice.
-- Safe to re-run.

alter table plumbers add column if not exists credits integer not null default 0;
alter table plumbers drop constraint if exists plumbers_credits_not_negative;
alter table plumbers add constraint plumbers_credits_not_negative check (credits >= 0);

create sequence if not exists credit_order_seq start 1;

create table if not exists credit_orders (
  ref         text primary key,                 -- C-0001
  plumber_id  text not null,
  credits     integer not null check (credits > 0),
  price_cents integer not null check (price_cents > 0),
  status      text not null default 'pending' check (status in ('pending', 'paid', 'cancelled')),
  created_at  timestamptz not null default now(),
  paid_at     timestamptz
);

create index if not exists credit_orders_status on credit_orders (status, created_at);
create index if not exists credit_orders_plumber on credit_orders (plumber_id, status);

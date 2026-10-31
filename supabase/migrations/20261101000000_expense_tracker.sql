create table if not exists public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  name text not null check (char_length(name) between 1 and 80),
  whatsapp_phone text not null default '',
  whatsapp_opt_in boolean not null default false,
  created_at timestamptz not null default now(),
  constraint profiles_whatsapp_phone_format
    check (whatsapp_phone = '' or whatsapp_phone ~ '^\+[1-9][0-9]{7,14}$'),
  constraint profiles_whatsapp_consent
    check (not whatsapp_opt_in or whatsapp_phone <> '')
);

create table if not exists public.transactions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  kind text not null check (kind in ('debit', 'credit')),
  amount numeric(12, 2) not null check (amount > 0 and amount <= 100000000),
  category text not null check (category in (
    'Food & Dining', 'Transport', 'Housing', 'Bills & Utilities', 'Shopping',
    'Health', 'Entertainment', 'Education', 'Travel', 'Income', 'Other'
  )),
  payee text not null check (char_length(payee) between 1 and 100),
  note text not null default '' check (char_length(note) <= 500),
  transaction_date date not null check (transaction_date <= current_date),
  created_at timestamptz not null default now(),
  constraint transactions_debit_not_income check (kind <> 'debit' or category <> 'Income')
);

create index if not exists transactions_user_date
  on public.transactions (user_id, transaction_date desc, created_at desc);

create table if not exists public.budgets (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  category text not null check (category in (
    'Food & Dining', 'Transport', 'Housing', 'Bills & Utilities', 'Shopping',
    'Health', 'Entertainment', 'Education', 'Travel', 'Other'
  )),
  monthly_limit numeric(12, 2) not null
    check (monthly_limit > 0 and monthly_limit <= 100000000),
  created_at timestamptz not null default now(),
  unique (user_id, category)
);

create table if not exists public.alert_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  category text not null,
  period_key text not null check (period_key ~ '^\d{4}-\d{2}$'),
  spent numeric(12, 2) not null check (spent >= 0),
  monthly_limit numeric(12, 2) not null check (monthly_limit > 0),
  status text not null check (status in ('pending', 'sent', 'failed')),
  detail text not null default '',
  created_at timestamptz not null default now(),
  unique (user_id, category, period_key)
);

create or replace function public.create_expense_tracker_profile()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, name)
  values (
    new.id,
    coalesce(nullif(new.raw_user_meta_data ->> 'name', ''), split_part(new.email, '@', 1))
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created_expense_tracker on auth.users;
create trigger on_auth_user_created_expense_tracker
  after insert on auth.users
  for each row execute procedure public.create_expense_tracker_profile();

revoke all on function public.create_expense_tracker_profile() from public, anon, authenticated;

alter table public.profiles enable row level security;
alter table public.transactions enable row level security;
alter table public.budgets enable row level security;
alter table public.alert_events enable row level security;

drop policy if exists "Users can read their own profile" on public.profiles;
create policy "Users can read their own profile"
  on public.profiles for select to authenticated
  using ((select auth.uid()) = id);
drop policy if exists "Users can create their own profile" on public.profiles;
create policy "Users can create their own profile"
  on public.profiles for insert to authenticated
  with check ((select auth.uid()) = id);
drop policy if exists "Users can update their own profile" on public.profiles;
create policy "Users can update their own profile"
  on public.profiles for update to authenticated
  using ((select auth.uid()) = id)
  with check ((select auth.uid()) = id);

drop policy if exists "Users can read their own transactions" on public.transactions;
create policy "Users can read their own transactions"
  on public.transactions for select to authenticated
  using ((select auth.uid()) = user_id);
drop policy if exists "Users can add their own transactions" on public.transactions;
create policy "Users can add their own transactions"
  on public.transactions for insert to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can read their own budgets" on public.budgets;
create policy "Users can read their own budgets"
  on public.budgets for select to authenticated
  using ((select auth.uid()) = user_id);
drop policy if exists "Users can add their own budgets" on public.budgets;
create policy "Users can add their own budgets"
  on public.budgets for insert to authenticated
  with check ((select auth.uid()) = user_id);
drop policy if exists "Users can update their own budgets" on public.budgets;
create policy "Users can update their own budgets"
  on public.budgets for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can read their own alert events" on public.alert_events;
create policy "Users can read their own alert events"
  on public.alert_events for select to authenticated
  using ((select auth.uid()) = user_id);

revoke all on table
  public.profiles,
  public.transactions,
  public.budgets,
  public.alert_events
from anon, authenticated;

grant usage on schema public to authenticated;
grant select, insert, update on public.profiles to authenticated;
grant select, insert on public.transactions to authenticated;
grant select, insert, update on public.budgets to authenticated;
grant select on public.alert_events to authenticated;
grant usage on schema public to service_role;
grant all on public.profiles, public.transactions, public.budgets, public.alert_events to service_role;

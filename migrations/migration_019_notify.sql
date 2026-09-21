-- ============================================================================
-- BrickMargin · migration 019 · per-set notifications
-- Each watched set decides for itself: whether to email at all, and how far
-- the price has to fall before it's worth an email. A global switch on the
-- profile silences everything at once. SMS is modelled now, switched on later.
-- ============================================================================
alter table watches add column if not exists notify_email   boolean not null default true;
alter table watches add column if not exists notify_sms     boolean not null default false;
alter table watches add column if not exists target_drop_pct numeric(5,2);   -- null = use the profile default
alter table watches add column if not exists target_price   numeric(10,2);   -- or an absolute: "tell me at $400"
alter table profiles add column if not exists notify_all    boolean not null default true;
alter table profiles add column if not exists phone         text;
alter table profiles add column if not exists sms_enabled   boolean not null default false;
notify pgrst, 'reload schema';

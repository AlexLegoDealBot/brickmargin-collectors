-- ============================================================================
-- BrickMargin · migration 016c · minifigures from BrickLink
-- Rebrickable has no BrickLink IDs for figures, so the catalogue is now built
-- from BrickLink's own set inventories. Clear the Rebrickable-keyed rows
-- (fig-XXXXXX) — they can never be priced — and track which sets are done.
-- ============================================================================
alter table sets add column if not exists minifigs_checked_at timestamptz;
create index if not exists sets_minifigs_checked_idx on sets (minifigs_checked_at nulls first);
delete from minifigs where fig_num like 'fig-%';
notify pgrst, 'reload schema';

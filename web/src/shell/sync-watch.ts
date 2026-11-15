/**
 * Notices a new watch sync between two status loads, so the status strip can announce it with
 * `notifyDataChanged('sync')` and the heart-rate chart re-fetches (a window that was
 * `preliminary` at the last sync may be `final` after this one).
 *
 *   load 1  lastSyncAt 10:02           first load: remembered, nothing announced
 *   load 2  lastSyncAt 10:02           unchanged: nothing
 *   load 3  lastSyncAt 10:17           changed: announce
 *   load 4  lastSyncAt null            became null (the proxy lost track): nothing, remembered
 *   load 5  lastSyncAt 10:32           changed from null: announce (a sync did happen)
 *
 * Times are compared as instants, so the same moment written two ways ("...Z" and "+00:00") is
 * not a change. A value that is not a valid time is compared as text.
 */
export interface SyncWatch {
  /** Records one status load's `lastSyncAt`; true when it shows a new sync. */
  changed(lastSyncAt: string | null): boolean;
}

const NOT_LOADED = Symbol('not loaded');

export function createSyncWatch(): SyncWatch {
  let previous: string | null | typeof NOT_LOADED = NOT_LOADED;
  const key = (value: string) => {
    const ms = Date.parse(value);
    return Number.isFinite(ms) ? String(ms) : value;
  };
  return {
    changed(lastSyncAt) {
      const before = previous;
      previous = lastSyncAt;
      if (before === NOT_LOADED || lastSyncAt === null) return false;
      return before === null || key(before) !== key(lastSyncAt);
    },
  };
}

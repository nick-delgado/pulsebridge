# Messages log (T19)

The collapsible strip at the bottom of the page: every cuff send attempt the simulator recorded
(D12), stored messages that no send record names, unreadable send records and FHIR
transactions, newest first by send time. It talks only to `listMessages` and sets the shared
selection.

| File           | What it holds                                                                              |
| -------------- | ------------------------------------------------------------------------------------------ |
| `index.ts`     | `mountMessages`, the shell's entry point                                                   |
| `log.ts`       | the panel: filters, loading, paging, keyed rendering, polling, selection (the flow)        |
| `rows.ts`      | pure: each row's words (both acknowledgment levels, lookups, writers), keys and sort times |
| `paging.ts`    | pure: following short pages (bounded) and merging a refresh into what is shown             |
| `messages.css` | `.log-…` styles, tokens only                                                               |

## Design

- **A list, not a table.** Rows of four kinds carry different facts (an attempt has two
  acknowledgment levels, a transaction a writer and resources), which one table header cannot
  describe honestly. Each row is a list item with a heading and labelled facts (`<dl>`); on a
  wide screen the facts line up in columns, on a phone they stack. A screen reader hears the same
  at every width.
- **Two acknowledgment levels (D7)**, as on the cuff side and with the same words
  (`commitAckPhrase`, `ACK_MODE_NOTE`, imported from `src/cuff/readings.ts` so they cannot
  drift): "Commit-level acknowledgment (from the store)" and "Application-level outcome (from the
  mapper)". Reasons are sentences from `src/api/reasons.ts`.
- **Only a confirmed lookup has an outcome** (proxy amendment 1). `not-found` and `failed` rows
  say so plainly and are never "pending" or polled.
- **Store-only rows** have no trace route, so no "Select" button. A `possibleDuplicateOf` is a
  hint with a link to the attempt row when it is loaded, never a confirmed link.
- **Paging.** Filters are applied by the proxy after the join, so pages can be short or empty
  while `nextCursor` is set. A load follows them on its own for at most 4 pages, then "Load more"
  continues; the footer says how many pages were searched. A `partial` page shows a notice with
  "Reload the log".
- **Filters** follow the proxy's semantics: an outcome filter matches only rows that have an
  outcome; transactions never match one. The note under the filters says so.
- **Refresh** on `cuff-send`, `sync` and `share`, and every 3 s while a shown row is pending and
  the strip is open (at most 2 minutes, then "Check again"). A refresh reloads the newest rows
  and merges them by key: unchanged rows keep their DOM, changed rows are rebuilt in place with
  their buttons kept, and the row at the top of the scrolled log stays in place when rows are
  added above it. Focus therefore survives.
- **Late answers** are dropped: one list request at a time, each with its own controller and a
  generation number; a filter change aborts what is in flight.
- **Selection.** "Select" on an attempt row sets `{source: 'cuff', attemptId, observationId,
summary, unshareableReason}`. The cuff panel loads its trace when the attempt was sent in this
  session; the share area acts on it after reading the Observation.
- **Text only.** Every server string is a text node; generated ids (`log-row-N`) only, never a
  server id (an unreadable row's id may be `""`).

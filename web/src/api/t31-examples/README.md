# T31 AuditEvent examples (copied)

Exact copies of the five send-attempt AuditEvents T31 and T33 generate in
`libs/pulsebridge_fhir/examples/` (`auditevent-send-accepted.json`, `auditevent-send-accepted-mllp.json`,
`auditevent-send-rejected.json`, `auditevent-send-no-ack.json`,
`auditevent-send-unreadable-reply.json`). All synthetic.

They are copied, not imported from `libs/` at run time, because `web/` must build without the Python
packages. The mock (`../mock-data.ts`) uses them as templates and varies only values, and
`../mock-audit.test.ts` compares the mock's AuditEvents with them element by element.

`../t31-examples.test.ts` keeps the copies in step: it fails when a copy differs from the library's
file (same JSON, same key order), when the library has an example with no copy here, or when a copy
has no library file. If it fails, copy the library's `auditevent-send-*.json` files here again.

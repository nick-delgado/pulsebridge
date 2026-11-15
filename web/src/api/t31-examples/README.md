# T31 AuditEvent examples (copied)

Exact copies of the four send-attempt AuditEvents T31 generates in
`libs/pulsebridge_fhir/examples/` (`auditevent-send-accepted.json`, `auditevent-send-rejected.json`,
`auditevent-send-no-ack.json`, `auditevent-send-unreadable-reply.json`), taken from the integration
branch after T31 merged. All synthetic.

They are copied, not imported from `libs/`, because `web/` must build and test without the Python
packages. The mock (`../mock-data.ts`) uses them as templates and varies only values, and
`../mock-audit.test.ts` compares the mock's AuditEvents with them element by element. If T31 changes
its model, copy the new files here and the test shows what the mock must change.

# Open Questions & Future Work

## Open Questions

1. **Mailbox ID stability:** Do Fastmail mailbox IDs change if folders are
   renamed? Need to handle re-resolution gracefully.
2. **Multi-label emails:** Some emails could fit multiple folders. Current
   design picks the single best match. Is that sufficient?

## Future Enhancements

See [prd.md Out of Scope](../prd.md#out-of-scope-current) for the full list of
scoped-out features. Items with design exploration notes are documented in
[dev/design-ideas.md](../dev/design-ideas.md).

Notable future enhancements not yet in design-ideas:
- **JMAP push notifications:** Instead of polling, use JMAP's EventSource
  push mechanism to react to new EmailDelivery state changes in near-realtime.
- **Multiple account support:** Extend to handle multiple Fastmail accounts
  or even non-Fastmail JMAP servers.

# Native Responses snapshots

The pinned upstream decoder projects provider events into an append-only neutral
stream. A `response.output_item.done` or `response.completed` snapshot that
replaces earlier text or function arguments raises a projection conflict. Before
the local patch, observing this conflict during exact native replay marked the
outbound encoder errored and dropped subsequent events, including completion.

`patches/responses-native-snapshots.patch` introduces a distinct internal
`SnapshotConflict` chunk so observers preserve the remaining native stream.
It keeps exact native Responses replay alive for these authoritative snapshots and recognizes a replayed completion
even when the neutral projection cannot express the correction. Provider `error`
and `response.failed` events still terminate native streams. Chat/Anthropic
translation continues to reject corrections that its append-only output cannot
represent. No route, model, credential or network settings change.

The Docker builder runs the upstream protocol, translation and algorithm tests,
including the
patch's text/function-argument corrections at item and terminal boundaries.
`tests/test_responses_transport.py` also verifies the built HTTP endpoint preserves
the correction, final output, sequence numbers and usage, while Chat translation
returns an error. Run it with `SWITCHYARD_TEST_BINARY` set as documented in the
[README](../README.md#validate-and-test). The agent repository adds browser and SDK
coverage through the complete native route.

Reassess or remove this patch when updating the upstream pin if its intermediate
stream representation gains replacement semantics or changes how replay observes
errors. These synthetic tests establish wire behavior, not live-provider quality.

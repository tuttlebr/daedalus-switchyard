# Native Responses snapshots

Native Responses providers can correct earlier text or function arguments in
`response.output_item.done` or `response.completed`. Treat these snapshots as
authoritative when consuming the native Responses stream. The gateway preserves
the correction and terminal event so the Agent and its clients can replace the
earlier value rather than append it twice.

The pinned upstream decoder also projects events into an append-only neutral
stream for other output formats. That representation cannot express a replacement,
so a correcting snapshot raises a projection conflict.

`patches/responses-native-snapshots.patch` introduces a distinct internal
`SnapshotConflict` chunk so observers preserve the remaining native stream.
It keeps exact native Responses replay alive for these authoritative snapshots and recognizes a replayed completion
even when the neutral projection cannot express the correction. Provider `error`
and `response.failed` events still terminate native streams. Chat/Anthropic
translation continues to reject corrections that its append-only output cannot
represent. Choose native Responses when the consumer needs replacement semantics;
a Chat or Anthropic consumer cannot represent every native correction.

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

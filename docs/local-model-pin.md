# Local model request pin

This document records the earlier local-model design. Current selectable route
files do not expose these local aliases; this is not the active route inventory.

The historical `daedalus/local` and `daedalus/hindsight-local`
routes request `nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB` through the
shared Dynamo gateway. This is one selected local model for both
aliases, including clients configured for image input.

The serving artifact is pinned separately to revision
`c6cc2e8ffa353403213d6b9b18aaf8eb684d705a`. A request model name does not verify
loaded weights, backend configuration, output quality or application benefit.
Deployment evidence must establish the actual serving artifact.

| Route configuration  | Fixed local targets                |
| -------------------- | ---------------------------------- |
| `routes_openai.toml` | `local`, `hindsight_local` |
| Historical hosted-provider variant | `local`, `hindsight_local` |

Both profiles use the existing Chat client and `LOCAL_API_KEY` mechanism at
`http://inference-gateway-internal.daedalus-dynamo.svc.cluster.local:8000/v1`.
The gateway requires a valid `LOCAL_API_KEY` and routes by the target's `model`
field. A different local model can be selected using its advertised `/v1/models`
ID without changing the client URL; update model-specific options accordingly.
The aliases remain fixed passthrough routes without a classifier or cloud
fallback.

The `local` target enables thinking and requests efficient reasoning with
`chat_template_kwargs = { enable_thinking = true, medium_effort = true }`. The
typed Dynamo extension `nvext = { max_thinking_tokens = 256 }` bounds a
reasoning span within the independent request output limit. It is not an
aggregate workflow reasoning ceiling. `hindsight_local` keeps its
nonthinking default and do not force a thinking-token budget. Trusted caller
fields retain the existing shallow merge precedence.

Merge the declared local destination and target changes into a fresh copy of the
active configuration. Preserve concurrent cloud, classifier, route, credential
and deployment changes; do not replay an entire older profile over live state.
The `none` option and `daedalus/none` alias are retired; do not restore them for
background or vision roles.

Retiring a previous inference worker must preserve the existing gateway address,
NodePort, model-header routes, authentication and supported compatibility IDs.
Catalogue aliases must identify the actual loaded Bolt checkpoint. Keep caches
and a rollback configuration; changing these scalar route files does not itself
retire a worker or verify model-serving compatibility.

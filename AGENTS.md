# Switchyard repository guidance

For reviews of `config/llms/*.toml` against provider specifications and Switchyard
compatibility, or Q&A-guided additions and edits, use
[switchyard-provider-config](.agents/skills/switchyard-provider-config/SKILL.md).
Use `config/api-spec/` as local evidence and check its provenance before treating
it as authoritative. Review mode does not authorize configuration changes or live
model traffic.

Follow parent repository instructions when present. Shared model-request,
reasoning, or transport contract changes also use the parent's `daedalus-co-design`
skill. Keep this provider configuration skill in this submodule; keep the shared
co-design skill and its lessons in the parent repository.

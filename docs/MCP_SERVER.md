# `safesink-mcp`: checks inside an agent

An agent writing a manifest can check it without shelling out:

```json
{
  "mcpServers": {
    "safesink": {
      "command": "safesink-mcp",
      "args": ["--root", "/path/to/project"]
    }
  }
}
```

## Lint-only by default

| Tool | What it does | Available |
| --- | --- | --- |
| `lint_manifest` | lints the declared annotations in a manifest, never starting it; every finding carries a `suggestion` with the concrete next step | always |
| `describe_rules` | lists the lint codes, each rule's rationale, caveat and suggested fix, generated from the rules themselves | always |
| `verify_manifest` | runs the conformance suite, which **executes the server under test** | only with `--allow-exec` |

`verify_manifest` is not advertised to the agent and refuses calls unless the
operator started `safesink-mcp --allow-exec`. An agent should not be able to turn
"check my manifest" into "run this command" on its own.

Both file tools take a path relative to `--root` (the working directory by
default) and refuse anything that resolves outside it, including symlinked
escapes. Manifests over 1 MB are refused. Failures come back as tool errors with
a code, never as a crashed session.

## Advice, not verdicts

A lint finding names the declaration problem and a `suggestion` for that tool,
for example `--contract delete_note=destructiveHint:true` or the annotation
to change. The suggestion is derived from the rule table that `describe_rules`
prints, so the codes, the rationale and the advice cannot drift apart. It stops
at advice: an agent still has to run the verifier to learn whether the fixed
claim is true.

## Why this shape

The valuable part of a conformance check is honesty about the boundary: does this
tool write where it says it only reads? That question needs the server running,
and running an untrusted server is a decision for the human who configured the
integration. Lint answers the declaration half safely, so it is the default; the
executing half is opt-in.

# Better Hindsight setup

Finish the normal Hermes memory-plugin setup:

```bash
hermes memory setup better_hindsight
```

Then create `~/.hermes/better_hindsight/config.json` as described in
[`docs/configuration.md`](docs/configuration.md). The context-aware planner remains off until
`planner.mode` is explicitly set to `shadow` or `active`. Installation does not restart the gateway. An
already-running process continues with its loaded provider until a separate normal restart is
authorized.

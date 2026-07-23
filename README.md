# Thrum

Thrum is the small, product-independent status CLI for the local Uoink,
Zing, and Writer suite. It reads the ratified public discovery contracts and
prints one bounded view of product health and the grab → study → write
workflow.

It does not start, stop, update, proxy, or call a product on the user's
behalf. It never reads product tokens or databases, and it never probes the
reserved Zing HTTP port.

## Local use

Thrum requires Python 3.11 or newer and has no runtime dependencies.

```text
python -m pip install .
suite doctor
suite status --json
```

`thrum` is an alias for `suite`.

The resident Uoink and Writer checks follow the suite discovery order:
an explicit CLI URL, then a valid per-user runtime lease, then the product's
default loopback address. Explicit URLs can be supplied with
`--uoink-url` and `--writer-url`.

Zing is stdio-only. Thrum looks for the installed `zing` command and validates
the output of `zing serve-mcp --print-config desktop`. Writer launchability is
checked the same way with `writer serve-mcp --print-config`. A missing command
is a calm absence; a command that returns an invalid configuration is
unhealthy.

The suite contract does not yet define a trusted install catalog. Until one
exists, commands that are installed outside `PATH` cannot be discovered.
Likewise, the public contracts expose no latest-artifact references, so the
workflow reports stage readiness and explicitly marks references as
`not_exposed`.

## Exit status

- `0`: no unhealthy product was found. Optional absence and unconfigured
  credentials are calm states.
- `1`: at least one discovered or explicitly configured product is unhealthy.
- `2`: command-line usage error.

## Local verification

```text
python -m pip install -e .
python -m unittest discover -s tests -v
python packaging/clean_install_check.py
```

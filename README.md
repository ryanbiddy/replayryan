# ReplayRyan Toolkit

The ReplayRyan Toolkit is the one place your local tools answer for themselves.
It reads the ratified public discovery contracts of **Uoink**, **Zing**, and
**Writer** and shows a single bounded view of product health and the
grab → study → write workflow.

Each product stays its own tool. Uoink is the corpus tool, Zing studies and
directs video, Writer drafts. The toolkit is what holds them: it reports, it
links, and it stops there.

It does not start, stop, update, proxy, or call a product on your behalf. It
never reads product tokens or databases, and it never probes the reserved Zing
HTTP port.

## Local use

Requires Python 3.11 or newer. No runtime dependencies.

```text
python -m pip install .
rr doctor
rr status --json
```

`replayryan` is the long-form alias for `rr`.

For the same status in a local browser:

```text
rr serve
```

Then open `http://127.0.0.1:5178`. The page is server-rendered, read-only,
loopback-bound, and refuses non-loopback Host headers, query strings, and write
methods. It uses the already-validated service manifest paths for links to a
running product; it does not read or place a product token in a URL.

The resident Uoink and Writer checks follow the suite discovery order:
an explicit CLI URL, then a valid per-user runtime lease, then the product's
default loopback address. Explicit URLs can be supplied with
`--uoink-url` and `--writer-url`.

Zing is stdio-only. The toolkit looks for the installed `zing` command and
validates the output of `zing serve-mcp --print-config desktop`. Writer
launchability is checked the same way with `writer serve-mcp --print-config`. A
missing command is a calm absence; a command that returns an invalid
configuration is unhealthy.

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

# Configuration & environment

*How ARC finds `arc.toml`, how it loads `.env`, and the precedence between
them.*

## `arc.toml` resolution

`load_arc_toml(path=None)` (`arc/core/config.py`) returns
`(config_path, config_dict)`. Resolution order (`resolve_config_path`):

1. an explicit `path` if it exists;
2. `./arc.toml` in the current working directory, when no explicit path was
   passed;
3. otherwise the bundled fallbacks — the repo-level `arc/arc.toml` is the
   default package catalogue; the inner `arc/arc/arc.toml` is a documented
   *pointer* with no independent package/extension lists.

Project-local configs overlay the bundled repo-level config. In particular,
`[packages].paths` is appended to the default package paths, so an external
package project can add only its own path instead of copying ARC's whole
package catalogue.

Loads are memoised by `(path, mtime)` and returned as a fresh deepcopy so
callers can mutate without corrupting the cache.

Helpers:

- `resolve_package_paths(config, config_path)` — `[packages].paths` →
  absolute paths.
- `filter_package_paths(paths, package_config)` — apply
  `[packages].enabled/disabled` (shared by the kernel and the orchestrator).
- `package_name_for_path(dir)` — read a package's declared name.

## `.env` loading

`load_env()` (`arc/core/env.py`) is a zero-dependency `KEY=VALUE` parser.
**Precedence: the real process environment always wins** — files only *fill
in* variables that aren't already set. Search order (lowest first):

```text
~/.arc/.env  →  ./.env  →  process environment (authoritative)
```

It is idempotent (guarded by a `_loaded` flag) and called once by each entry
point before packages read config.

```{warning}
A checked-in `arc/.env` with real provider credentials will be loaded into the
process environment. Keep secrets out of committed `.env` files; use
`.env.example` for documentation and your own untracked `.env` for values.
```

## See also

- {doc}`../reference/configuration` — every env var and `arc.toml` key.
- {doc}`../packages/enable-disable` — startup vs session package filtering.

## API reference

```{eval-rst}
.. automodule:: arc.core.config
   :members:
   :undoc-members:

.. automodule:: arc.core.env
   :members:
   :undoc-members:
```

# Stack checklist: Python (on demand)

Load when the diff contains .py. Lint and typecheck gates already ran: look for what they miss. Only the diff; cite file:line.

## Correctness: major
- Mutable default argument; late-binding closure in a loop; `is` on str/int; bare `except:` or `except Exception: pass`.
- Lost cause: `raise X` inside `except` without `from e`; error swallowed into a default value.
- File, socket, lock or DB cursor opened without `with`; `subprocess` without `timeout` / `check`; `requests` without timeout.
- `open()` / `read_text()` without `encoding=` on text that can contain non-ASCII; naive `datetime.now()` where time zones matter.
- Dict/list mutated while iterating; `==` on floats for money; `sum` of Decimal and float.

## Security: blocker when reachable from input
- `shell=True` or f-string into a command; SQL built by f-string / `%` / `.format`; `pickle` / `yaml.load` / `eval` / `exec` on external data.
- Path joined from user input without a resolve-and-prefix check; `tempfile.mktemp`; secrets in logs or defaults.

## Structure: minor unless it hides a bug
- Python version syntax newer than the project's `python_requires`; new import of a package missing from requirements.
- `print` / `breakpoint()` left in; `assert` used for runtime validation (stripped under `-O`).
- Async code: blocking call inside `async def`; un-awaited coroutine; task created and never kept or awaited.

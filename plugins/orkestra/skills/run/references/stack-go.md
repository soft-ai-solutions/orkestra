# Stack checklist: Go (on demand)

Load when the diff contains .go. `go vet` / lint gates already ran: look for what they miss. Only the diff; cite file:line.

## Errors: major
- Ignored error (`_ =`, unchecked `Close`/`Write`/`Scan`); error wrapped with `%v` instead of `%w`; `err` shadowed in an inner scope.
- `panic` in library code for an expected failure; `log.Fatal` / `os.Exit` outside `main`.
- Nil check on an interface holding a typed nil; map write on a nil map; unchecked type assertion (use the `, ok` form).

## Concurrency: blocker when it can corrupt data or hang
- Goroutine without a stop condition (no `ctx.Done()`, no closed channel): leak. Loop variable captured by a goroutine on Go < 1.22.
- Shared map or slice written without a mutex or channel; `sync.WaitGroup.Add` inside the goroutine; copying a `sync.Mutex`.
- Send on a closed channel; unbuffered send with no receiver; `select` without a `default` or `ctx` case on a blocking path.

## Resources: major
- `defer` inside a loop; `defer resp.Body.Close()` before the error check; HTTP client without timeout; response body not drained.
- `context.Context` not first parameter or not passed down; `context.Background()` inside a request path.

## Structure: minor
- Exported name without doc comment on a public package; new dependency in `go.mod` not used by the task.
- Table tests that share mutable state; `t.Fatal` called from a goroutine.

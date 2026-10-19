# Stack checklist: JVM (on demand: Java, Kotlin, Scala)

Load when the diff contains .java, .kt, .kts or .scala. Compiler and lint gates already ran: look for what they miss. Only the diff; cite file:line.

## Correctness: major
- `catch (Exception)` / `Throwable` that swallows or only logs; checked exception wrapped without the cause; `finally` that returns or throws.
- `==` on strings or boxed numbers; `equals` overridden without `hashCode`; mutable object used as a map key.
- Nullability: Kotlin `!!` without a guard; Java `Optional.get()` without `isPresent`; platform types from Java trusted as non-null.
- `BigDecimal` built from a double; `SimpleDateFormat` or other non-thread-safe state shared between threads; default charset or time zone.

## Resources and concurrency: major
- Stream, connection or `ExecutorService` not closed (`try-with-resources` / `use {}`); unbounded thread or queue creation.
- Blocking call on a coroutine dispatcher meant for CPU work; `GlobalScope.launch`; swallowed `CancellationException`.
- Shared mutable collection without a concurrent type or lock; `synchronized` on a public or boxed object.

## Security: blocker when reachable from input
- SQL by string concatenation; `ObjectInputStream` / unsafe deserialization of external data; path built from input without normalization.
- Secrets in code, logs or `toString()` of a data class; disabled TLS or hostname verification.

## Structure: minor
- New dependency in `pom.xml` / `build.gradle*` the task does not need; `System.out` / `printStackTrace` left in; Java target level raised.

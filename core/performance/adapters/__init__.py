"""Optional instrumentation wrappers around existing subsystems.

Everything under `core.performance.adapters` follows the same shape:
wrap an existing object (a pool, an auth service, a database
connection) behind the same public interface, add timing/gauges around
its calls using the ambient `RequestProfiler` from
`core.performance.context`, and change nothing about existing call
signatures or exception behaviour. Adopting an adapter is opt-in at the
call site — nothing here monkeypatches or otherwise mutates the
wrapped object.

When no profiler is bound to the current context (unsampled or
disabled requests, background jobs that never went through the
middleware), every adapter degrades to a direct, uninstrumented
delegate call: no stage is opened, no gauge is read, no metric point is
allocated.
"""

# Ownership, concurrency and hot paths

- Start with one owner. Borrow or pass a slice when lifetime permits. Transfer
  ownership across execution boundaries, reuse buffers when their owner can do
  so safely and reserve `Arc` for genuinely independent lifetimes. Account for
  copies, allocation, reference counts and indirection in the measured hot path.
- Use a dedicated owner and bounded MPSC for mutable control-plane state or
  composite state that spans asynchronous work. Specify capacity, queue-full
  behavior, request deadlines, cancellation, shutdown/drain behavior and task
  supervision. Keep independent tenant or partition work parallel. Move long
  compute or blocking I/O off the owner and the async runtime thread.
- Keep a mutex for short synchronous sections that protect a coherent invariant.
  Release the guard before awaiting, calling external code or running long work.
  Use an async mutex when a shared resource actually requires a guard across
  an await. Measure contention, queue age, saturation and cancellation in tests.
- Keep atomics for independent counters, flags or an established protocol.
  Explain who publishes and reads state, the synchronization relation, ordering
  on success and failure, and how reclamation or ABA is excluded when relevant.
  Use an existing primitive or a mutex when the state spans multiple variables.
  Test meaningful interleavings. Use a model checker for new lock-free protocols.
- Encapsulate unavoidable `unsafe` behind a safe API and document its `# Safety`
  contract and each block's local proof. An intentional panic must establish
  why recovery is impossible in that context. Handle fallible cleanup explicitly
  and preserve the primary failure. Escalate invariant failures using existing
  tracing and OTLP contracts.
- Benchmark identical representative workloads before and after the change.
  Check throughput, tail latency, memory, allocation rate, backpressure and
  fairness. Preserve cancellation, admission limits and fault recovery.

## Source guides

- [Tokio shared state](https://tokio.rs/tokio/tutorial/shared-state)
- [Abseil on atomic operations](https://abseil.io/docs/cpp/atomic_danger)

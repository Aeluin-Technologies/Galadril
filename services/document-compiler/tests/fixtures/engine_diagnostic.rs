//! Controlled upstream fixtures can report diagnostics without production
//! logs.
fn main() -> anyhow::Result<()> {
    use tectonic_status_base::ChatterLevel;
    use tectonic_status_base::plain::PlainStatusBackend;
    document_compiler::worker::run_with_status(&mut PlainStatusBackend::new(
        ChatterLevel::Minimal,
    ))
}

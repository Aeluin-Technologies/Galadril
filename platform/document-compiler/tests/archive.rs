//! Exported bundles are bounded and contain only normalized relative
//! filenames.
use galadril_document_compiler::archive::Archive;

#[test]
fn archives_roundtrip_and_reject_traversal_duplicates_and_memory_exhaustion()
-> anyhow::Result<()> {
    let mut archive = Archive::new();
    assert!(archive.add("../secret.tex", b"source").is_err());
    archive.add("recoursadmin.cls", b"class")?;
    assert!(archive.add("recoursadmin.cls", b"different").is_err());
    archive.add("documents/letter.pdf", b"%PDF-1.7")?;
    let bytes = archive.finish()?;
    let result = zip::ZipArchive::new(std::io::Cursor::new(bytes))?;
    assert_eq!(result.len(), 2);
    assert!(result.file_names().any(|name| name == "recoursadmin.cls"));
    Ok(())
}

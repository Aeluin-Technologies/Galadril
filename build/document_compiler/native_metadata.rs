//! Bazel supplies pinned native archives; Cargo metadata supplies header
//! paths.
use std::error::Error;
use std::path::PathBuf;

fn main() -> Result<(), Box<dyn Error>> {
    let root = std::env::current_dir()?.join(std::env::var("NATIVE_MARKER")?);
    let root = root.parent().ok_or("Native marker has no parent")?;
    let include = PathBuf::from(std::env::var("OUT_DIR")?).join("include");
    copy_headers(&root.join("usr/include"), &include)?;
    println!(
        "cargo:include-path={};{}",
        include.display(),
        include.join("freetype2").display()
    );
    println!("cargo:define_static=");
    let target = std::env::var("TARGET")?;
    let triple = if target.starts_with("aarch64") {
        "aarch64-linux-gnu"
    } else {
        "x86_64-linux-gnu"
    };
    let libraries: PathBuf = root.join("usr/lib").join(triple);
    println!("cargo:rustc-link-search=native={}", libraries.display());
    for library in std::env::var("NATIVE_LIBRARIES")?
        .split(',')
        .filter(|name| !name.is_empty())
    {
        let kind = if libraries.join(format!("lib{library}.a")).exists() {
            "static"
        } else {
            "dylib"
        };
        println!("cargo:rustc-link-lib={kind}={library}");
    }
    Ok(())
}

/// Keep downstream build actions independent of producer runfile trees.
fn copy_headers(
    source: &std::path::Path,
    destination: &std::path::Path,
) -> Result<(), Box<dyn Error>> {
    std::fs::create_dir_all(destination)?;
    for entry in std::fs::read_dir(source)? {
        let entry = entry?;
        let target = destination.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_headers(&entry.path(), &target)?;
        } else {
            std::fs::copy(entry.path(), target)?;
        }
    }
    Ok(())
}

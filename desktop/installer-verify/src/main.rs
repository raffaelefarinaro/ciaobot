//! Verify a Ciaobot release archive without requiring Python, Homebrew, or Minisign.
//!
//! This binary is published beside `install.sh`. The shell installer uses it to
//! verify the Tauri updater archive before it is extracted or moved into the
//! user's Applications directory.

use base64::{engine::general_purpose::STANDARD, Engine as _};
use minisign_verify::{PublicKey, Signature};
use std::{env, fs, path::Path, process::ExitCode};

const PUBLIC_KEY: &str = "untrusted comment: minisign public key: 9AE9394001E725283\nRWSDUnIeQDnpmnNJiTjLmN6XOVFqgn1A0EXvTVG7AJIZXJxhyFN9osxm";

fn read_tauri_signature(path: &Path) -> Result<Signature, String> {
    let encoded = fs::read_to_string(path)
        .map_err(|error| format!("could not read release signature: {error}"))?;
    let trimmed = encoded.trim();
    let minisign = if trimmed.starts_with("untrusted comment:") {
        trimmed.to_owned()
    } else {
        let decoded = STANDARD
            .decode(trimmed)
            .map_err(|error| format!("invalid base64 release signature: {error}"))?;
        String::from_utf8(decoded)
            .map_err(|error| format!("release signature is not UTF-8 minisign data: {error}"))?
    };
    Signature::decode(&minisign)
        .map_err(|error| format!("invalid release signature: {error}"))
}

fn main() -> ExitCode {
    let mut args = env::args_os();
    let _program = args.next();
    let Some(payload) = args.next() else {
        eprintln!("usage: ciaobot-installer-verify <archive> <signature>");
        return ExitCode::from(2);
    };
    let Some(signature) = args.next() else {
        eprintln!("usage: ciaobot-installer-verify <archive> <signature>");
        return ExitCode::from(2);
    };
    if args.next().is_some() {
        eprintln!("usage: ciaobot-installer-verify <archive> <signature>");
        return ExitCode::from(2);
    }

    let result = (|| -> Result<(), String> {
        let public_key = PublicKey::decode(PUBLIC_KEY)
            .map_err(|error| format!("invalid embedded public key: {error}"))?;
        let signature = read_tauri_signature(Path::new(&signature))?;
        let payload = fs::read(&payload)
            .map_err(|error| format!("could not read release archive: {error}"))?;
        public_key
            .verify(&payload, &signature, true)
            .map_err(|error| format!("release signature verification failed: {error}"))?;
        println!("{}", signature.trusted_comment());
        Ok(())
    })();

    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::from(1)
        }
    }
}

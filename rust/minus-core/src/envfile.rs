//! Editing `.env` without destroying it, mirrored from
//! `src/minus/services/env_file.py`.
//!
//! A settings file a person also edits cannot be round-tripped through a parser:
//! that loses comments, ordering, blank lines and unknown keys. Nothing here is
//! rewritten except the specific lines being changed, and the write is
//! temp-file-then-rename so a reader never sees a half-written file.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

const HEADER: &str = "# --- written by the minus dashboard ---";
const NEEDS_QUOTING: &[char] = &[' ', '\t', '"', '\'', '#', '$', '`', '\\'];

/// A value safe to put on the right-hand side of an `=`.
///
/// Only backslash and double quote are escaped, because those are the only two
/// `python-dotenv` unescapes. Escaping `$` would corrupt it rather than protect
/// it: dotenv hands the backslash back as part of the value.
pub fn quote(value: &str) -> String {
    if value.is_empty() || value.chars().any(|character| NEEDS_QUOTING.contains(&character)) {
        let escaped = value.replace('\\', "\\\\").replace('"', "\\\"");
        format!("\"{escaped}\"")
    } else {
        value.to_string()
    }
}

/// Set each key in `updates`, returning the names actually written.
pub fn update_env_file(
    path: &Path,
    updates: &BTreeMap<String, String>,
    allowed: Option<&BTreeSet<String>>,
) -> io::Result<Vec<String>> {
    if let Some(allowed) = allowed {
        if let Some(refused) = updates.keys().find(|key| !allowed.contains(*key)) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("Refusing to write {refused} to {}", path.display()),
            ));
        }
    }

    let original = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == io::ErrorKind::NotFound => String::new(),
        Err(error) => return Err(error),
    };

    let mut remaining: BTreeMap<&String, &String> = updates.iter().collect();
    let mut lines: Vec<String> = Vec::new();

    for line in original.split('\n') {
        match split_assignment(line) {
            Some((prefix, key, equals, _)) if remaining.contains_key(&key) => {
                let value = remaining.remove(&key).expect("checked above");
                lines.push(format!("{prefix}{key}{equals}{}", quote(value)));
            }
            _ => lines.push(line.to_string()),
        }
    }

    if !remaining.is_empty() {
        if lines.last().is_some_and(|line| !line.trim().is_empty()) {
            lines.push(String::new());
        }
        if !original.contains(HEADER) {
            lines.push(HEADER.to_string());
        }
        // Sorted, so repeated writes produce a stable file rather than one whose
        // diff depends on map iteration order.
        for (key, value) in remaining {
            lines.push(format!("{key}={}", quote(value)));
        }
    }

    let mut content = lines.join("\n");
    if !content.ends_with('\n') {
        content.push('\n');
    }
    write_atomically(path, &content)?;

    Ok(updates.keys().cloned().collect())
}

/// `KEY=value`, with the optional `export ` a shell-sourced file may carry.
///
/// Returns (prefix, key, equals, value) so a rewritten line keeps its own
/// indentation, its `export` and the spacing before the `=`.
fn split_assignment(line: &str) -> Option<(String, String, String, String)> {
    let bytes = line.as_bytes();

    let mut indent_end = 0;
    while indent_end < bytes.len() && (bytes[indent_end] == b' ' || bytes[indent_end] == b'\t') {
        indent_end += 1;
    }

    // `export` must be followed by whitespace: "exported=1" is a key, and
    // treating it as a prefix would rewrite the wrong thing.
    let mut prefix_end = indent_end;
    if line[indent_end..].starts_with("export")
        && bytes
            .get(indent_end + 6)
            .is_some_and(|byte| *byte == b' ' || *byte == b'\t')
    {
        let mut after = indent_end + 6;
        while after < bytes.len() && (bytes[after] == b' ' || bytes[after] == b'\t') {
            after += 1;
        }
        prefix_end = after;
    }

    let key_start = prefix_end;
    match bytes.get(key_start) {
        Some(byte) if byte.is_ascii_alphabetic() || *byte == b'_' => {}
        _ => return None,
    }

    let mut key_end = key_start;
    while key_end < bytes.len()
        && (bytes[key_end].is_ascii_alphanumeric() || bytes[key_end] == b'_')
    {
        key_end += 1;
    }

    let mut equals_at = key_end;
    while equals_at < bytes.len() && (bytes[equals_at] == b' ' || bytes[equals_at] == b'\t') {
        equals_at += 1;
    }
    if bytes.get(equals_at) != Some(&b'=') {
        return None;
    }

    let value_start = equals_at + 1;
    Some((
        line[..key_start].to_string(),
        line[key_start..key_end].to_string(),
        line[key_end..value_start].to_string(),
        line[value_start..].to_string(),
    ))
}

fn write_atomically(path: &Path, content: &str) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }

    let mode = fs::metadata(path)
        .map(|meta| meta.permissions().mode() & 0o777)
        .unwrap_or(0o600);

    let temp = path.with_file_name(format!(
        ".{}.tmp",
        path.file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_else(|| "env".into())
    ));
    fs::write(&temp, content)?;
    fs::set_permissions(&temp, fs::Permissions::from_mode(mode))?;
    fs::rename(&temp, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_path(tag: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "minus-core-envfile-{}-{tag}",
            std::process::id()
        ));
        fs::create_dir_all(&dir).unwrap();
        dir.join(".env")
    }

    #[test]
    fn quoting_matches_the_python_rules() {
        assert_eq!(quote("plain"), "plain");
        assert_eq!(quote(""), "\"\"");
        assert_eq!(quote("two words"), "\"two words\"");
        assert_eq!(quote("a\"b"), "\"a\\\"b\"");
        assert_eq!(quote("a\\b"), "\"a\\\\b\"");
        // `$` is quoted but not escaped, exactly as dotenv expects.
        assert_eq!(quote("$HOME"), "\"$HOME\"");
    }

    #[test]
    fn rewriting_keeps_comments_ordering_and_unknown_keys() {
        let path = temp_path("keep");
        fs::write(
            &path,
            "# why this is what it is\nA=1\nB=2  # inline comment\nOTHER=keep\n",
        )
        .unwrap();

        let mut updates = BTreeMap::new();
        updates.insert("B".to_string(), "new value".to_string());

        let written = update_env_file(&path, &updates, None).unwrap();
        let text = fs::read_to_string(&path).unwrap();

        assert_eq!(written, vec!["B".to_string()]);
        assert!(text.contains("# why this is what it is"));
        assert!(text.contains("A=1"));
        assert!(text.contains("B=\"new value\""));
        assert!(text.contains("OTHER=keep"));

        fs::remove_dir_all(path.parent().unwrap()).ok();
    }

    #[test]
    fn a_new_key_is_appended_under_the_header_once() {
        let path = temp_path("append");
        fs::write(&path, "A=1\n").unwrap();

        let mut updates = BTreeMap::new();
        updates.insert("MINUS_CHAT_MODEL".to_string(), "a/b".to_string());
        update_env_file(&path, &updates, None).unwrap();
        update_env_file(&path, &updates, None).unwrap();

        let text = fs::read_to_string(&path).unwrap();
        assert_eq!(text.matches(HEADER).count(), 1);
        assert_eq!(text.matches("MINUS_CHAT_MODEL=a/b").count(), 1);

        fs::remove_dir_all(path.parent().unwrap()).ok();
    }

    #[test]
    fn export_lines_are_recognised_and_keep_their_prefix() {
        let path = temp_path("export");
        fs::write(&path, "export MINUS_LOG_LEVEL=DEBUG\n").unwrap();

        let mut updates = BTreeMap::new();
        updates.insert("MINUS_LOG_LEVEL".to_string(), "INFO".to_string());
        update_env_file(&path, &updates, None).unwrap();

        assert_eq!(fs::read_to_string(&path).unwrap(), "export MINUS_LOG_LEVEL=INFO\n");
        fs::remove_dir_all(path.parent().unwrap()).ok();
    }

    #[test]
    fn an_unallowed_key_is_refused() {
        let path = temp_path("allowed");
        let mut updates = BTreeMap::new();
        updates.insert("MINUS_CHAT_MODEL".to_string(), "a/b".to_string());

        let mut allowed = BTreeSet::new();
        allowed.insert("OPENROUTER_API_KEY".to_string());

        let error = update_env_file(&path, &updates, Some(&allowed)).expect_err("refused");
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        fs::remove_dir_all(path.parent().unwrap()).ok();
    }

    #[test]
    fn a_new_file_is_created_private() {
        let path = temp_path("mode");
        let mut updates = BTreeMap::new();
        updates.insert("MINUS_CHAT_MODEL".to_string(), "a/b".to_string());

        update_env_file(&path, &updates, None).unwrap();

        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
        fs::remove_dir_all(path.parent().unwrap()).ok();
    }
}

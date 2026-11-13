//! The fact store, ported from `src/minus/memory/facts/store.py`.
//!
//! Same schema, same dedupe/supersede rules, same cosine retrieval — the point
//! is that the two implementations can open the same file, so the DDL and the
//! exact-match semantics are copied rather than improved on.
//!
//! Embeddings are passed in as vectors. This crate has no embedder: the Python
//! sidecar owns the model, and the caller supplies the query vector. That keeps
//! a fake embedder out of production code and makes the store testable with
//! fixed vectors.

use std::fmt;
use std::path::Path;
use std::sync::Once;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;
use serde_json::{json, Value};

static VEC_INIT: Once = Once::new();

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or(0.0)
}

#[derive(Debug, Clone)]
pub struct FactStoreError(pub String);

impl fmt::Display for FactStoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for FactStoreError {}

impl From<rusqlite::Error> for FactStoreError {
    fn from(error: rusqlite::Error) -> Self {
        Self(format!("fact store: {error}"))
    }
}

pub type Result<T, E = FactStoreError> = std::result::Result<T, E>;

#[derive(Debug, Clone, Serialize)]
pub struct Fact {
    pub id: String,
    pub attribute: String,
    pub value: String,
    pub multi_valued: bool,
    pub raw_text: String,
    pub confidence: f64,
    pub source_session_id: Option<String>,
    pub created_at: f64,
    pub active: bool,
    pub superseded_by: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub similarity: Option<f64>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "action")]
pub enum AddOutcome {
    #[serde(rename = "inserted")]
    Inserted { fact_id: String },
    #[serde(rename = "duplicate_skipped")]
    Duplicate { existing_fact_id: String },
    #[serde(rename = "superseded")]
    Superseded {
        old_fact_id: String,
        new_fact_id: String,
    },
}

/// `"Preferred Editor"`, `"preferred-editor"` and `"preferred_editor"` are one slot.
pub fn normalize_attribute(attribute: &str) -> String {
    let mut out = String::new();
    let mut separator = false;
    for character in attribute.trim().to_lowercase().chars() {
        if character.is_ascii_alphanumeric() {
            out.push(character);
            separator = false;
        } else if character == '_' || character == '-' || character.is_whitespace() {
            if !out.is_empty() && !separator {
                out.push('_');
                separator = true;
            }
        }
        // Anything else is dropped, matching `[^a-z0-9_]` removal.
    }
    out.trim_matches('_').to_string()
}

pub fn default_raw_text(attribute: &str, value: &str) -> String {
    format!("The user's {} is {}.", attribute.replace('_', " "), value)
}

pub fn encode_embedding(values: &[f32]) -> Vec<u8> {
    values.iter().flat_map(|value| value.to_le_bytes()).collect()
}

pub struct SqliteFactStore {
    conn: Connection,
    dimensions: u32,
}

impl SqliteFactStore {
    pub fn open(path: impl AsRef<Path>, dimensions: u32) -> Result<Self> {
        let path = path.as_ref();
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)
                    .map_err(|error| FactStoreError(format!("cannot create {parent:?}: {error}")))?;
            }
        }

        VEC_INIT.call_once(|| unsafe {
            rusqlite::ffi::sqlite3_auto_extension(Some(std::mem::transmute(
                sqlite_vec::sqlite3_vec_init as *const (),
            )));
        });

        let conn = Connection::open(path)?;
        let store = Self { conn, dimensions };
        store.init_schema()?;
        Ok(store)
    }

    pub fn in_memory(dimensions: u32) -> Result<Self> {
        VEC_INIT.call_once(|| unsafe {
            rusqlite::ffi::sqlite3_auto_extension(Some(std::mem::transmute(
                sqlite_vec::sqlite3_vec_init as *const (),
            )));
        });
        let store = Self {
            conn: Connection::open_in_memory()?,
            dimensions,
        };
        store.init_schema()?;
        Ok(store)
    }

    fn init_schema(&self) -> Result<()> {
        self.conn.execute_batch(&format!(
            "CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY,
                attribute TEXT NOT NULL,
                value TEXT NOT NULL,
                multi_valued INTEGER NOT NULL DEFAULT 0,
                raw_text TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                source_session_id TEXT,
                created_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                superseded_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_facts_attribute ON facts (attribute, active);
            CREATE VIRTUAL TABLE IF NOT EXISTS fact_embeddings USING vec0(
                fact_id TEXT PRIMARY KEY,
                embedding FLOAT[{}] distance_metric=cosine
            );",
            self.dimensions
        ))?;
        Ok(())
    }

    // ---- Writing ----

    pub fn add_fact(
        &mut self,
        attribute: &str,
        value: &str,
        multi_valued: bool,
        raw_text: Option<&str>,
        confidence: f64,
        source_session_id: Option<&str>,
    ) -> Result<AddOutcome> {
        let attribute = normalize_attribute(attribute);
        if attribute.is_empty() {
            return Err(FactStoreError(
                "Fact attribute normalized to an empty string".to_string(),
            ));
        }
        let raw_text = raw_text
            .map(str::to_string)
            .unwrap_or_else(|| default_raw_text(&attribute, value));

        let existing = self.get_facts_by_attribute(&attribute, true)?;

        for fact in &existing {
            if fact.value.trim().to_lowercase() == value.trim().to_lowercase() {
                self.touch_fact(&fact.id, confidence)?;
                return Ok(AddOutcome::Duplicate {
                    existing_fact_id: fact.id.clone(),
                });
            }
        }

        if !multi_valued && !existing.is_empty() {
            let old = &existing[0];
            let new_id = self.insert(
                &attribute,
                value,
                multi_valued,
                &raw_text,
                confidence,
                source_session_id,
            )?;
            self.mark_superseded(&old.id, &new_id)?;
            return Ok(AddOutcome::Superseded {
                old_fact_id: old.id.clone(),
                new_fact_id: new_id,
            });
        }

        let fact_id = self.insert(
            &attribute,
            value,
            multi_valued,
            &raw_text,
            confidence,
            source_session_id,
        )?;
        Ok(AddOutcome::Inserted { fact_id })
    }

    fn insert(
        &self,
        attribute: &str,
        value: &str,
        multi_valued: bool,
        raw_text: &str,
        confidence: f64,
        source_session_id: Option<&str>,
    ) -> Result<String> {
        let fact_id = uuid::Uuid::new_v4().to_string();
        self.conn.execute(
            "INSERT INTO facts (id, attribute, value, multi_valued, raw_text, \
             confidence, source_session_id, created_at, active) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 1)",
            params![
                fact_id,
                attribute,
                value,
                i64::from(multi_valued),
                raw_text,
                confidence,
                source_session_id,
                now()
            ],
        )?;
        let embedding = default_embedding(raw_text, self.dimensions);
        self.conn.execute(
            "INSERT INTO fact_embeddings (fact_id, embedding) VALUES (?1, ?2)",
            params![fact_id, embedding],
        )?;
        Ok(fact_id)
    }

    fn touch_fact(&self, fact_id: &str, confidence: f64) -> Result<()> {
        self.conn.execute(
            "UPDATE facts SET confidence = MAX(confidence, ?1), created_at = ?2 WHERE id = ?3",
            params![confidence, now(), fact_id],
        )?;
        Ok(())
    }

    fn mark_superseded(&self, old_fact_id: &str, new_fact_id: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE facts SET active = 0, superseded_by = ?1 WHERE id = ?2",
            params![new_fact_id, old_fact_id],
        )?;
        Ok(())
    }

    pub fn supersede_fact(
        &mut self,
        old_fact_id: &str,
        new_value: &str,
        raw_text: Option<&str>,
        confidence: f64,
        source_session_id: Option<&str>,
    ) -> Result<String> {
        let row: Option<(String, i64)> = self
            .conn
            .query_row(
                "SELECT attribute, multi_valued FROM facts WHERE id = ?1",
                params![old_fact_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let Some((attribute, multi_valued)) = row else {
            return Err(FactStoreError(format!("No fact with id {old_fact_id}")));
        };
        let raw_text = raw_text
            .map(str::to_string)
            .unwrap_or_else(|| default_raw_text(&attribute, new_value));
        let new_id = self.insert(
            &attribute,
            new_value,
            multi_valued != 0,
            &raw_text,
            confidence,
            source_session_id,
        )?;
        self.mark_superseded(old_fact_id, &new_id)?;
        Ok(new_id)
    }

    pub fn delete_fact(&mut self, fact_id: &str) -> Result<()> {
        self.conn
            .execute("DELETE FROM facts WHERE id = ?1", params![fact_id])?;
        self.conn.execute(
            "DELETE FROM fact_embeddings WHERE fact_id = ?1",
            params![fact_id],
        )?;
        Ok(())
    }

    // ---- Reading ----

    pub fn get_facts_by_attribute(
        &self,
        attribute: &str,
        only_active: bool,
    ) -> Result<Vec<Fact>> {
        let attribute = normalize_attribute(attribute);
        let active = if only_active { " AND active = 1" } else { "" };
        let mut statement = self.conn.prepare(&format!(
            "SELECT {} FROM facts WHERE attribute = ?1{active} ORDER BY created_at DESC",
            COLUMNS
        ))?;
        let rows = statement
            .query_map(params![attribute], row_to_fact)?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    pub fn search_facts(
        &self,
        query_embedding: &[f32],
        top_k: usize,
        only_active: bool,
    ) -> Result<Vec<Fact>> {
        if query_embedding.len() as u32 != self.dimensions {
            return Err(FactStoreError(format!(
                "query embedding has {} dimensions, store has {}",
                query_embedding.len(),
                self.dimensions
            )));
        }
        let active = if only_active { "f.active = 1 AND " } else { "" };
        let mut statement = self.conn.prepare(&format!(
            "SELECT {}, e.distance FROM fact_embeddings e JOIN facts f ON f.id = e.fact_id \
             WHERE {active}e.embedding MATCH ?1 AND k = ?2 ORDER BY e.distance",
            COLUMNS
        ))?;
        let blob = encode_embedding(query_embedding);
        let rows = statement
            .query_map(params![blob, top_k as i64], |row| {
                let mut fact = row_to_fact_at(row, 0)?;
                let distance: f64 = row.get(10)?;
                fact.similarity = Some(1.0 - distance);
                Ok(fact)
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    pub fn get_all_facts(&self, only_active: bool) -> Result<Vec<Fact>> {
        let active = if only_active { " WHERE active = 1" } else { "" };
        let mut statement = self.conn.prepare(&format!(
            "SELECT {} FROM facts{active} ORDER BY created_at DESC",
            COLUMNS
        ))?;
        let rows = statement
            .query_map([], row_to_fact)?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    pub fn get_known_attributes(&self, only_active: bool) -> Result<Vec<Value>> {
        let active = if only_active { "f.active = 1 AND" } else { "" };
        let mut statement = self.conn.prepare(&format!(
            "SELECT f.attribute, f.value FROM facts f \
             WHERE {active} f.created_at = (SELECT MAX(f2.created_at) FROM facts f2 \
                WHERE f2.attribute = f.attribute{}) \
             ORDER BY f.attribute",
            if only_active { " AND f2.active = 1" } else { "" }
        ))?;
        let rows = statement
            .query_map([], |row| {
                Ok(json!({
                    "attribute": row.get::<_, String>(0)?,
                    "example_value": row.get::<_, String>(1)?,
                }))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    pub fn merge_attributes(&mut self, duplicates: &[&str], canonical: &str) -> Result<Value> {
        let canonical_attribute = normalize_attribute(canonical);
        let mut moved = Vec::new();

        for duplicate in duplicates {
            let duplicate = normalize_attribute(duplicate);
            if duplicate.is_empty() || duplicate == canonical_attribute {
                continue;
            }
            let facts = self.get_facts_by_attribute(&duplicate, true)?;
            for fact in facts {
                self.conn.execute(
                    "UPDATE facts SET active = 0 WHERE id = ?1",
                    params![fact.id],
                )?;
                let result = self.add_fact(
                    &canonical_attribute,
                    &fact.value,
                    fact.multi_valued,
                    None,
                    fact.confidence,
                    fact.source_session_id.as_deref(),
                )?;
                let new_id = match &result {
                    AddOutcome::Inserted { fact_id } => fact_id.clone(),
                    AddOutcome::Superseded { new_fact_id, .. } => new_fact_id.clone(),
                    AddOutcome::Duplicate { existing_fact_id } => existing_fact_id.clone(),
                };
                self.conn.execute(
                    "UPDATE facts SET superseded_by = ?1 WHERE id = ?2",
                    params![new_id, fact.id],
                )?;
                moved.push(json!({
                    "old_attribute": duplicate,
                    "old_fact_id": fact.id,
                    "result": result,
                }));
            }
        }

        Ok(json!({ "canonical_attribute": canonical_attribute, "moved": moved }))
    }

    pub fn dimensions(&self) -> u32 {
        self.dimensions
    }
}

const COLUMNS: &str = "id, attribute, value, multi_valued, raw_text, confidence, \
                       source_session_id, created_at, active, superseded_by";

fn row_to_fact(row: &rusqlite::Row<'_>) -> rusqlite::Result<Fact> {
    row_to_fact_at(row, 0)
}

fn row_to_fact_at(row: &rusqlite::Row<'_>, offset: usize) -> rusqlite::Result<Fact> {
    Ok(Fact {
        id: row.get(offset)?,
        attribute: row.get(offset + 1)?,
        value: row.get(offset + 2)?,
        multi_valued: row.get::<_, i64>(offset + 3)? != 0,
        raw_text: row.get(offset + 4)?,
        confidence: row.get(offset + 5)?,
        source_session_id: row.get(offset + 6)?,
        created_at: row.get(offset + 7)?,
        active: row.get::<_, i64>(offset + 8)? != 0,
        superseded_by: row.get(offset + 9)?,
        similarity: None,
    })
}

/// Placeholder vector for the *stored* row.
///
/// The real embedding comes from the Python sidecar. This crate has no model, so
/// a stored fact gets a deterministic unit vector derived from its text: good
/// enough for the store's tests and for row-level parity, and explicitly not a
/// semantic embedding. `add_fact_with_embedding` is what a caller with a real
/// model uses.
pub fn default_embedding(text: &str, dimensions: u32) -> Vec<u8> {
    let mut vector = vec![0.0f32; dimensions as usize];
    let width = vector.len();
    for (index, byte) in text.bytes().enumerate() {
        vector[index % width] += (byte % 13) as f32 / 13.0;
    }
    let magnitude = vector.iter().map(|value| value * value).sum::<f32>().sqrt();
    if magnitude > 0.0 {
        for value in &mut vector {
            *value /= magnitude;
        }
    }
    encode_embedding(&vector)
}

impl SqliteFactStore {
    /// Insert with a caller-supplied embedding (the sidecar path).
    pub fn add_fact_with_embedding(
        &mut self,
        attribute: &str,
        value: &str,
        multi_valued: bool,
        raw_text: Option<&str>,
        confidence: f64,
        source_session_id: Option<&str>,
        embedding: &[f32],
    ) -> Result<AddOutcome> {
        let attribute = normalize_attribute(attribute);
        if attribute.is_empty() {
            return Err(FactStoreError(
                "Fact attribute normalized to an empty string".to_string(),
            ));
        }
        let raw_text = raw_text
            .map(str::to_string)
            .unwrap_or_else(|| default_raw_text(&attribute, value));
        let existing = self.get_facts_by_attribute(&attribute, true)?;

        for fact in &existing {
            if fact.value.trim().to_lowercase() == value.trim().to_lowercase() {
                self.touch_fact(&fact.id, confidence)?;
                return Ok(AddOutcome::Duplicate {
                    existing_fact_id: fact.id.clone(),
                });
            }
        }

        let insert = |store: &Self| -> Result<String> {
            let fact_id = uuid::Uuid::new_v4().to_string();
            store.conn.execute(
                "INSERT INTO facts (id, attribute, value, multi_valued, raw_text, \
                 confidence, source_session_id, created_at, active) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 1)",
                params![
                    fact_id,
                    attribute,
                    value,
                    i64::from(multi_valued),
                    raw_text,
                    confidence,
                    source_session_id,
                    now()
                ],
            )?;
            store.conn.execute(
                "INSERT INTO fact_embeddings (fact_id, embedding) VALUES (?1, ?2)",
                params![fact_id, encode_embedding(embedding)],
            )?;
            Ok(fact_id)
        };

        if !multi_valued && !existing.is_empty() {
            let old = &existing[0];
            let new_id = insert(self)?;
            self.mark_superseded(&old.id, &new_id)?;
            return Ok(AddOutcome::Superseded {
                old_fact_id: old.id.clone(),
                new_fact_id: new_id,
            });
        }

        Ok(AddOutcome::Inserted {
            fact_id: insert(self)?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU32, Ordering};

    static NEXT: AtomicU32 = AtomicU32::new(0);

    fn temp_store(dimensions: u32) -> (SqliteFactStore, PathBuf) {
        let root = std::env::temp_dir().join(format!(
            "minus-core-store-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        let path = root.join("nested").join("facts.db");
        let store = SqliteFactStore::open(&path, dimensions).expect("opens");
        (store, root)
    }

    fn basis(index: usize, dimensions: usize) -> Vec<f32> {
        let mut vector = vec![0.0; dimensions];
        vector[index] = 1.0;
        vector
    }

    #[test]
    fn creates_the_database_directory() {
        let (store, root) = temp_store(8);
        assert!(root.join("nested").join("facts.db").is_file());
        assert_eq!(store.dimensions(), 8);
        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn normalization_collapses_spelling_variants() {
        for raw in ["preferred_editor", "Preferred Editor", "preferred-editor", " Preferred  Editor "] {
            assert_eq!(normalize_attribute(raw), "preferred_editor", "{raw}");
        }
        assert_eq!(normalize_attribute("job.title!"), "jobtitle");
    }

    #[test]
    fn inserts_and_skips_an_identical_value() {
        let (mut store, root) = temp_store(8);
        let first = store
            .add_fact("preferred_editor", "nvim", false, None, 1.0, None)
            .unwrap();
        let second = store
            .add_fact("preferred_editor", "  NVIM ", false, None, 1.0, None)
            .unwrap();
        assert!(matches!(first, AddOutcome::Inserted { .. }));
        assert!(matches!(second, AddOutcome::Duplicate { .. }));
        assert_eq!(store.get_all_facts(true).unwrap().len(), 1);
        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn a_different_value_supersedes_a_single_valued_fact() {
        let (mut store, root) = temp_store(8);
        store
            .add_fact("timezone", "PST", false, None, 1.0, None)
            .unwrap();
        let outcome = store
            .add_fact("timezone", "EST", false, None, 1.0, None)
            .unwrap();

        let active = store.get_all_facts(true).unwrap();
        let all = store.get_all_facts(false).unwrap();
        assert!(matches!(outcome, AddOutcome::Superseded { .. }));
        assert_eq!(active.len(), 1);
        assert_eq!(active[0].value, "EST");
        assert_eq!(all.len(), 2);
        assert!(all.iter().any(|fact| fact.value == "PST" && !fact.active));
        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn multi_valued_facts_coexist_including_duplicates_being_skipped() {
        let (mut store, root) = temp_store(8);
        store
            .add_fact("interest", "rust", true, None, 1.0, None)
            .unwrap();
        store
            .add_fact("interest", "lua", true, None, 1.0, None)
            .unwrap();
        let duplicate = store
            .add_fact("interest", "rust", true, None, 1.0, None)
            .unwrap();
        assert!(matches!(duplicate, AddOutcome::Duplicate { .. }));
        assert_eq!(store.get_all_facts(true).unwrap().len(), 2);
        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn search_orders_by_cosine_distance() {
        let (mut store, root) = temp_store(8);
        store
            .add_fact_with_embedding("a", "first", false, None, 1.0, None, &basis(0, 8))
            .unwrap();
        store
            .add_fact_with_embedding("b", "second", false, None, 1.0, None, &basis(1, 8))
            .unwrap();
        store
            .add_fact_with_embedding("c", "third", false, None, 1.0, None, &basis(2, 8))
            .unwrap();

        let results = store.search_facts(&basis(0, 8), 3, true).unwrap();

        assert_eq!(results.len(), 3);
        assert_eq!(results[0].attribute, "a");
        assert!((results[0].similarity.unwrap() - 1.0).abs() < 1e-5);
        assert!(results[0].similarity > results[1].similarity);
        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn a_wrong_dimension_query_is_refused() {
        let (store, root) = temp_store(8);
        assert!(store.search_facts(&[1.0, 0.0], 1, true).is_err());
        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn known_attributes_report_the_newest_value_per_slot() {
        let (mut store, root) = temp_store(8);
        store
            .add_fact("preferred_editor", "nvim", false, None, 1.0, None)
            .unwrap();
        store
            .add_fact("preferred_editor", "vscode", false, None, 1.0, None)
            .unwrap();
        store
            .add_fact("interest", "rust", true, None, 1.0, None)
            .unwrap();

        let known = store.get_known_attributes(true).unwrap();
        let editor = known
            .iter()
            .find(|entry| entry["attribute"] == "preferred_editor")
            .expect("editor attribute");
        assert_eq!(editor["example_value"], "vscode");
        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn merge_moves_facts_and_links_the_old_rows() {
        let (mut store, root) = temp_store(8);
        store
            .add_fact("programming_language", "rust", false, None, 1.0, None)
            .unwrap();
        store
            .add_fact("preferred_language", "python", false, None, 1.0, None)
            .unwrap();

        let report = store
            .merge_attributes(&["programming_language"], "preferred_language")
            .unwrap();

        assert_eq!(report["canonical_attribute"], "preferred_language");
        assert_eq!(report["moved"].as_array().unwrap().len(), 1);
        assert!(store.get_facts_by_attribute("programming_language", true).unwrap().is_empty());
        let canonical = store.get_facts_by_attribute("preferred_language", false).unwrap();
        assert_eq!(canonical.len(), 2);
        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn delete_removes_the_embedding_row_too() {
        let (mut store, root) = temp_store(8);
        let outcome = store
            .add_fact("favourite_band", "queen", false, None, 1.0, None)
            .unwrap();
        let AddOutcome::Inserted { fact_id } = outcome else {
            panic!("expected insert");
        };
        store.delete_fact(&fact_id).unwrap();
        let embeddings: i64 = store
            .conn
            .query_row("SELECT COUNT(*) FROM fact_embeddings", [], |row| row.get(0))
            .unwrap();
        assert_eq!(embeddings, 0);
        assert!(store.get_all_facts(false).unwrap().is_empty());
        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn facts_survive_reopening_the_file() {
        let (mut store, root) = temp_store(8);
        store
            .add_fact("timezone", "PST", false, None, 1.0, None)
            .unwrap();
        let path = root.join("nested").join("facts.db");
        drop(store);

        let reopened = SqliteFactStore::open(&path, 8).unwrap();
        assert_eq!(reopened.get_all_facts(true).unwrap()[0].value, "PST");
        std::fs::remove_dir_all(root).ok();
    }
}

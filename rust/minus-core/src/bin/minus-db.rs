//! `minus-db`: seed, list or search a fact store from the shell.
//!
//! Exists for the cross-language parity check: this binary writes a database the
//! Python `SqliteFactStore` can open, and reads one Python wrote. It is also the
//! simplest way to see the store's semantics without a conversation.
//!
//! Usage:
//!   minus-db --db PATH [--dimensions N] seed
//!   minus-db --db PATH list
//!   minus-db --db PATH search --query 1,0,0,0,0,0,0,0

use std::path::PathBuf;

use minus_core::store::{AddOutcome, SqliteFactStore};
use serde_json::json;

fn basis(index: usize, dimensions: usize) -> Vec<f32> {
    let mut vector = vec![0.0; dimensions];
    vector[index % dimensions] = 1.0;
    vector
}

fn fail(message: impl std::fmt::Display) -> ! {
    eprintln!("minus-db: {message}");
    std::process::exit(1)
}

fn main() {
    let mut db: Option<PathBuf> = None;
    let mut dimensions: u32 = 8;
    let mut command: Option<String> = None;
    let mut query: Vec<f32> = Vec::new();

    let mut args = std::env::args().skip(1);
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--db" => db = args.next().map(PathBuf::from),
            "--dimensions" => {
                dimensions = args
                    .next()
                    .and_then(|value| value.parse().ok())
                    .unwrap_or_else(|| fail("--dimensions needs a number"))
            }
            "--query" => {
                query = args
                    .next()
                    .map(|value| {
                        value
                            .split(',')
                            .filter_map(|part| part.trim().parse::<f32>().ok())
                            .collect()
                    })
                    .unwrap_or_default()
            }
            "--help" | "-h" => {
                println!("minus-db --db PATH [--dimensions N] seed|list|search [--query v1,v2,...]");
                return;
            }
            other => command = Some(other.to_string()),
        }
    }

    let Some(db) = db else { fail("--db is required") };
    let command = command.unwrap_or_else(|| fail("a command is required: seed, list or search"));

    let mut store = match SqliteFactStore::open(&db, dimensions) {
        Ok(store) => store,
        Err(error) => fail(error),
    };

    match command.as_str() {
        "seed" => {
            let inserts = [
                ("preferred_editor", "nvim", false, 0usize),
                ("preferred_editor", "vscode", false, 1usize),
                ("interest", "rust", true, 2usize),
                ("interest", "lua", true, 3usize),
            ];
            for (attribute, value, multi, index) in inserts {
                let outcome = store
                    .add_fact_with_embedding(
                        attribute,
                        value,
                        multi,
                        None,
                        1.0,
                        None,
                        &basis(index, dimensions as usize),
                    )
                    .unwrap_or_else(|error| fail(error));
                assert!(matches!(
                    outcome,
                    AddOutcome::Inserted { .. } | AddOutcome::Superseded { .. }
                ));
            }
            println!("{}", json!({"seeded": store.get_all_facts(false).unwrap().len()}));
        }
        "list" => {
            let active = store.get_all_facts(true).unwrap_or_else(|error| fail(error));
            let inactive = store
                .get_all_facts(false)
                .unwrap_or_else(|error| fail(error))
                .into_iter()
                .filter(|fact| !fact.active)
                .collect::<Vec<_>>();
            println!("{}", json!({"active": active, "inactive": inactive}));
        }
        "search" => {
            if query.is_empty() {
                fail("search needs --query v1,v2,...");
            }
            let results = store
                .search_facts(&query, 5, true)
                .unwrap_or_else(|error| fail(error));
            let rows: Vec<serde_json::Value> = results
                .iter()
                .map(|fact| {
                    json!({
                        "id": fact.id,
                        "attribute": fact.attribute,
                        "value": fact.value,
                        "similarity": fact.similarity,
                    })
                })
                .collect();
            println!("{}", json!(rows));
        }
        other => fail(format!("unknown command {other:?}")),
    }
}

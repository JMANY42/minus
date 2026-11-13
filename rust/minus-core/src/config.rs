//! Settings, mirrored from `src/minus/config.py`.
//!
//! This is a *subset* port: the fields needed to answer `get_config` and
//! `set_config` against a Rust core, with the same names, defaults and
//! disposition (live / restart-required / blocked / not-applicable) as the
//! Python table in `assembly.build_config_controller`. Anything absent here is
//! absent from `describe()` too, so a dashboard sees fewer settings rather than
//! wrong ones.
//!
//! Reads are the same three layers pydantic-settings uses: defaults, then
//! `.env` at the project root, then the real environment, which wins.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Text,
    Integer,
    Float,
    Boolean,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Disposition {
    /// Changing it takes effect on the running object.
    Live,
    /// Changing it needs a restart.
    Restart,
    /// Refused, with the reason: changing it would invalidate stored data.
    Blocked(&'static str),
    /// Refused, with the reason: it cannot mean anything here.
    NotApplicable(&'static str),
}

#[derive(Clone, Debug)]
pub struct Setting {
    pub name: &'static str,
    pub kind: Kind,
    pub disposition: Disposition,
    pub default: Value,
}

pub const SECRETS: &[&str] = &[
    "openrouter_api_key",
    "llm_api_key",
    "google_client_id",
    "google_client_secret",
    "google_refresh_token",
];

const BLOCKED_DIM: &str = "The fact store's vector dimension is fixed when the table is created; \
                           changing it would invalidate every embedding in semantic_memory.db.";
const BLOCKED_MODEL: &str = "Stored embeddings come from this model; changing it without \
                             re-embedding every fact silently corrupts similarity search.";
const NO_CONSOLE: &str = "There is no console handler when running as a service.";

/// The field table. Order is the order `config.py` declares them in, which is
/// the order a reader of `get_config` sees.
pub fn settings() -> Vec<Setting> {
    let setting = |name, kind, disposition, default: Value| Setting {
        name,
        kind,
        disposition,
        default,
    };
    use Disposition::{Blocked, Live, NotApplicable, Restart};
    use Kind::{Boolean, Float, Integer, Text};

    vec![
        setting("openrouter_api_key", Text, Restart, json!("")),
        setting("openrouter_base_url", Text, Restart, json!("https://openrouter.ai/api/v1")),
        setting("app_title", Text, Restart, json!("Minus")),
        setting("llm_base_url", Text, Restart, json!("")),
        setting("llm_api_key", Text, Restart, json!("")),
        setting("chat_model", Text, Live, json!("openai/gpt-oss-20b:nitro")),
        setting("fact_extraction_model", Text, Live, json!("openai/gpt-oss-20b:nitro")),
        setting("deep_model", Text, Live, json!("deepseek/deepseek-v4-flash-0731:nitro")),
        setting("deep_reasoning_effort", Text, Live, json!("high")),
        setting("deep_max_tool_rounds", Integer, Live, json!(4)),
        setting("deep_timeout_seconds", Float, Live, json!(120.0)),
        setting("max_retries", Integer, Live, json!(3)),
        setting("max_tool_rounds", Integer, Live, json!(7)),
        setting("disabled_tools", Text, Live, json!("")),
        setting("google_client_id", Text, Restart, json!("")),
        setting("google_client_secret", Text, Restart, json!("")),
        setting("google_refresh_token", Text, Restart, json!("")),
        setting("google_tasks_list", Text, Restart, json!("")),
        setting("google_calendar", Text, Restart, json!("")),
        setting("idle_conversation_seconds", Float, Live, json!(30.0)),
        setting("relevance_threshold", Float, Live, json!(0.356)),
        setting("fact_search_top_k", Integer, Live, json!(5)),
        setting("embedding_model", Text, Blocked(BLOCKED_MODEL), json!("all-MiniLM-L6-v2")),
        setting("embedding_dim", Integer, Blocked(BLOCKED_DIM), json!(384)),
        setting("tts_voice", Text, Live, json!("am_puck")),
        setting("tts_speed", Float, Live, json!(1.0)),
        setting("tts_lang", Text, Live, json!("en-us")),
        setting("tts_chunk_max_chars", Integer, Live, json!(300)),
        setting("tts_first_chunk_max_chars", Integer, Live, json!(60)),
        setting("stt_model", Text, Restart, json!("small.en")),
        setting("stt_realtime_model", Text, Restart, json!("tiny.en")),
        setting("stt_device", Text, Restart, json!("cuda")),
        setting("timezone", Text, Restart, json!("America/Chicago")),
        setting("log_level", Text, Live, json!("DEBUG")),
        setting("console_log_level", Text, NotApplicable(NO_CONSOLE), json!("INFO")),
        setting("log_retention", Integer, Restart, json!(30)),
        setting("plugins_enabled", Boolean, Restart, json!(true)),
    ]
}

/// `MINUS_<UPPER>` for every field except the one the Python model aliases.
pub fn env_name(name: &str) -> String {
    if name == "openrouter_api_key" {
        "OPENROUTER_API_KEY".to_string()
    } else {
        format!("MINUS_{}", name.to_uppercase())
    }
}

/// Project root: `MINUS_PROJECT_ROOT`, else the working directory.
pub fn project_root() -> PathBuf {
    match std::env::var("MINUS_PROJECT_ROOT") {
        Ok(value) if !value.trim().is_empty() => PathBuf::from(value),
        _ => std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")),
    }
}

#[derive(Clone, Debug)]
pub struct Config {
    values: BTreeMap<String, Value>,
}

impl Default for Config {
    fn default() -> Self {
        Self::new()
    }
}

impl Config {
    pub fn new() -> Self {
        let values = settings()
            .into_iter()
            .map(|setting| (setting.name.to_string(), setting.default))
            .collect();
        Self { values }
    }

    /// Defaults, then `.env`, then the process environment.
    pub fn load() -> Result<Self, String> {
        Self::load_from(Some(&project_root().join(".env")))
    }

    pub fn load_from(dotenv: Option<&Path>) -> Result<Self, String> {
        let mut config = Self::new();
        let mut problems = Vec::new();

        if let Some(path) = dotenv {
            if path.is_file() {
                match dotenvy::from_path_iter(path) {
                    Ok(entries) => {
                        for (key, value) in entries.flatten() {
                            config.apply_env(&key, &value, &mut problems);
                        }
                    }
                    Err(error) => problems.push(format!("{}: {error}", path.display())),
                }
            }
        }

        // Environment last, so a real variable beats the file -- and a stale
        // export cannot be silently defeated by editing .env.
        for setting in settings() {
            let name = env_name(setting.name);
            if let Ok(value) = std::env::var(&name) {
                config.apply_env(&name, &value, &mut problems);
            }
        }

        if problems.is_empty() {
            Ok(config)
        } else {
            Err(problems.join("; "))
        }
    }

    fn apply_env(&mut self, key: &str, raw: &str, problems: &mut Vec<String>) {
        let Some(setting) = settings().into_iter().find(|item| env_name(item.name) == key) else {
            return; // Unknown keys are ignored, exactly as pydantic's extra="ignore".
        };
        match coerce(setting.kind, &Value::from(raw)) {
            Ok(value) => {
                self.values.insert(setting.name.to_string(), value);
            }
            Err(reason) => problems.push(format!("{key}: {reason}")),
        }
    }

    pub fn get(&self, name: &str) -> Option<&Value> {
        self.values.get(name)
    }

    /// A text setting, or `""` when it is unset.
    pub fn text(&self, name: &str) -> String {
        self.get(name)
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string()
    }

    /// An integer setting, or 0 when it is unset.
    pub fn integer(&self, name: &str) -> i64 {
        self.get(name).and_then(Value::as_i64).unwrap_or(0)
    }

    /// Change a setting, returning the coerced value.
    ///
    /// A live setting is stored here; a restart-required one is validated and
    /// returned but *not* stored, matching `ConfigController.apply`, which
    /// leaves the running object alone and reports `restart_required`. Storing
    /// it would make `get_config` describe a value the process is not using.
    pub fn set(&mut self, name: &str, value: &Value) -> Result<Value, String> {
        if SECRETS.contains(&name) {
            return Err("Secrets cannot be set over the control socket.".to_string());
        }

        let Some(setting) = settings().into_iter().find(|item| item.name == name) else {
            return Err(format!("Unknown setting {name:?}"));
        };

        match setting.disposition {
            Disposition::Blocked(reason) => Err(reason.to_string()),
            Disposition::NotApplicable(reason) => Err(reason.to_string()),
            Disposition::Live => {
                let coerced = coerce(setting.kind, value)?;
                self.values.insert(name.to_string(), coerced.clone());
                Ok(coerced)
            }
            Disposition::Restart => coerce(setting.kind, value),
        }
    }

    /// The shape `ConfigController.describe()` returns.
    pub fn describe(&self) -> Value {
        let mut values = Map::new();
        let mut live = Map::new();
        let mut restart_required = Map::new();
        let mut blocked = Map::new();
        let mut not_applicable = Map::new();

        for setting in settings() {
            let Some(value) = self.values.get(setting.name) else {
                continue;
            };
            match setting.disposition {
                Disposition::Live => {
                    live.insert(setting.name.into(), value.clone());
                }
                Disposition::Restart => {
                    restart_required.insert(setting.name.into(), value.clone());
                }
                Disposition::Blocked(reason) => {
                    blocked.insert(setting.name.into(), Value::from(reason));
                }
                Disposition::NotApplicable(reason) => {
                    not_applicable.insert(setting.name.into(), Value::from(reason));
                }
            }

            if !SECRETS.contains(&setting.name) {
                values.insert(setting.name.into(), value.clone());
            }
        }

        let mut secrets: Vec<&str> = SECRETS.to_vec();
        secrets.sort_unstable();

        json!({
            "values": values,
            "live": live,
            "restart_required": restart_required,
            "blocked": blocked,
            "not_applicable": not_applicable,
            "secrets": secrets,
        })
    }

    /// The `.env` lines a set of changes would write.
    pub fn env_updates(changes: &Map<String, Value>) -> BTreeMap<String, String> {
        changes
            .iter()
            .filter(|(name, _)| !SECRETS.contains(&name.as_str()))
            .map(|(name, value)| {
                (
                    env_name(name),
                    match value {
                        Value::String(text) => text.clone(),
                        other => other.to_string(),
                    },
                )
            })
            .collect()
    }
}

/// Whether a setting takes effect on a running process.
pub fn is_live(name: &str) -> bool {
    settings()
        .into_iter()
        .find(|setting| setting.name == name)
        .is_some_and(|setting| matches!(setting.disposition, Disposition::Live))
}

pub fn coerce(kind: Kind, value: &Value) -> Result<Value, String> {
    match kind {
        Kind::Text => match value {
            Value::String(text) => Ok(Value::from(text.clone())),
            other => Err(format!("expected a string, got {other}")),
        },
        Kind::Integer => match value {
            Value::Number(number) => match number.as_i64() {
                Some(int) => Ok(Value::from(int)),
                None => Err(format!("expected an integer, got {number}")),
            },
            Value::String(text) => text
                .trim()
                .parse::<i64>()
                .map(Value::from)
                .map_err(|_| format!("expected an integer, got {text:?}")),
            other => Err(format!("expected an integer, got {other}")),
        },
        Kind::Float => match value {
            Value::Number(number) => Ok(Value::from(
                number.as_f64().ok_or_else(|| format!("expected a number, got {number}"))?,
            )),
            Value::String(text) => text
                .trim()
                .parse::<f64>()
                .map(Value::from)
                .map_err(|_| format!("expected a number, got {text:?}")),
            other => Err(format!("expected a number, got {other}")),
        },
        Kind::Boolean => match value {
            Value::Bool(flag) => Ok(Value::from(*flag)),
            Value::String(text) => match text.trim().to_ascii_lowercase().as_str() {
                "true" | "1" | "yes" | "on" | "t" => Ok(Value::from(true)),
                "false" | "0" | "no" | "off" | "f" => Ok(Value::from(false)),
                other => Err(format!("expected a boolean, got {other:?}")),
            },
            other => Err(format!("expected a boolean, got {other}")),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn clean() -> Config {
        Config::new()
    }

    #[test]
    fn defaults_match_the_python_model() {
        let config = clean();
        assert_eq!(config.get("chat_model").unwrap(), "openai/gpt-oss-20b:nitro");
        assert_eq!(
            config.get("deep_model").unwrap(),
            "deepseek/deepseek-v4-flash-0731:nitro"
        );
        assert_eq!(config.get("deep_reasoning_effort").unwrap(), "high");
        assert_eq!(config.get("deep_max_tool_rounds").unwrap(), 4);
        assert_eq!(config.get("max_tool_rounds").unwrap(), 7);
        assert_eq!(config.get("relevance_threshold").unwrap(), 0.356);
        assert_eq!(config.get("tts_first_chunk_max_chars").unwrap(), 60);
        assert_eq!(config.get("tts_chunk_max_chars").unwrap(), 300);
        assert_eq!(config.get("idle_conversation_seconds").unwrap(), 30.0);
    }

    #[test]
    fn describe_hides_secrets_and_partitions_the_rest() {
        let described = clean().describe();

        assert!(described["values"].get("openrouter_api_key").is_none());
        assert_eq!(described["values"]["chat_model"], "openai/gpt-oss-20b:nitro");
        assert_eq!(described["live"]["chat_model"], "openai/gpt-oss-20b:nitro");
        assert!(described["restart_required"].get("stt_device").is_some());
        assert!(described["blocked"].get("embedding_dim").is_some());
        assert!(described["not_applicable"].get("console_log_level").is_some());
        assert_eq!(described["secrets"][0], "google_client_id");
    }

    #[test]
    fn a_live_write_is_coerced_and_stored() {
        let mut config = clean();
        let value = config
            .set("relevance_threshold", &json!("0.5"))
            .expect("coerces a numeric string");
        assert_eq!(value, 0.5);
        assert_eq!(config.get("relevance_threshold").unwrap(), 0.5);
    }

    #[test]
    fn refusals_carry_their_reason() {
        let mut config = clean();

        let secret = config
            .set("openrouter_api_key", &json!("x"))
            .expect_err("refused");
        assert!(secret.contains("Secrets"));

        let blocked = config
            .set("embedding_dim", &json!(512))
            .expect_err("refused");
        assert!(blocked.contains("vector dimension"));

        let unknown = config
            .set("not_a_setting", &json!(1))
            .expect_err("refused");
        assert!(unknown.contains("Unknown setting"));

        assert!(config.set("fact_search_top_k", &json!("many")).is_err());
    }

    #[test]
    fn env_name_follows_the_alias() {
        assert_eq!(env_name("openrouter_api_key"), "OPENROUTER_API_KEY");
        assert_eq!(env_name("chat_model"), "MINUS_CHAT_MODEL");
    }

    #[test]
    fn dotenv_is_layered_then_the_environment_wins() {
        // Hermetic: a developer's exported MINUS_* must not decide the result.
        for key in ["MINUS_CHAT_MODEL", "MINUS_MAX_RETRIES"] {
            std::env::remove_var(key);
        }
        let dir = std::env::temp_dir().join(format!("minus-core-config-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let env_path = dir.join(".env");
        std::fs::write(&env_path, "MINUS_CHAT_MODEL=from/file\nMINUS_MAX_RETRIES=5\n").unwrap();

        let config = Config::load_from(Some(&env_path)).expect("loads");
        assert_eq!(config.get("chat_model").unwrap(), "from/file");
        assert_eq!(config.get("max_retries").unwrap(), 5);

        std::env::set_var("MINUS_CHAT_MODEL", "from/env");
        let config = Config::load_from(Some(&env_path)).expect("loads");
        std::env::remove_var("MINUS_CHAT_MODEL");
        assert_eq!(config.get("chat_model").unwrap(), "from/env");

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_bad_value_is_reported_not_silently_defaulted() {
        let dir = std::env::temp_dir().join(format!("minus-core-bad-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let env_path = dir.join(".env");
        std::fs::write(&env_path, "MINUS_FACT_SEARCH_TOP_K=lots\n").unwrap();

        let error = Config::load_from(Some(&env_path)).expect_err("reports");
        assert!(error.contains("MINUS_FACT_SEARCH_TOP_K"));

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn env_updates_name_only_what_is_persisted() {
        let mut changes = Map::new();
        changes.insert("chat_model".into(), json!("a/b"));
        changes.insert("max_retries".into(), json!(2));

        let updates = Config::env_updates(&changes);

        assert_eq!(updates["MINUS_CHAT_MODEL"], "a/b");
        assert_eq!(updates["MINUS_MAX_RETRIES"], "2");
    }
}

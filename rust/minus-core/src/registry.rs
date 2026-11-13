//! Tools: the function *is* the schema, and the registry dispenses them.
//!
//! The Python side derives a JSON Schema from a signature and a Google-style
//! docstring (`src/minus/tools/schema.py`). Rust cannot introspect a function
//! at runtime, so the remap is: each tool's arguments are a struct with
//! `#[derive(JsonSchema)]`, and `Tool::new::<Args, _>` turns that derivation
//! plus `serde` validation into the same two things the Python registry builds
//! — a schema to advertise and a validator to run before the body.
//!
//! Registration is explicit (no `inventory`): the composition root decides what
//! a tier holds, exactly as `assembly.py` does. A static constructor would take
//! that decision away from the caller.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::sync::Arc;

use schemars::JsonSchema;
use serde::de::DeserializeOwned;
use serde_json::{json, Map, Value};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToolError {
    Unknown(String),
    Disabled(String),
    Argument(String),
    Execution(String),
    Generation(String),
}

impl fmt::Display for ToolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unknown(message)
            | Self::Disabled(message)
            | Self::Argument(message)
            | Self::Execution(message)
            | Self::Generation(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for ToolError {}

impl From<serde_json::Error> for ToolError {
    fn from(error: serde_json::Error) -> Self {
        Self::Argument(error.to_string())
    }
}

type Runner = Arc<dyn Fn(&Value) -> Result<Value, ToolError> + Send + Sync>;

pub struct Tool {
    pub name: String,
    pub description: String,
    pub category: String,
    pub parameters: Value,
    run: Runner,
}

impl Tool {
    pub fn new<A, F>(name: &str, description: &str, run: F) -> Self
    where
        A: DeserializeOwned + JsonSchema,
        F: Fn(A) -> Result<Value, ToolError> + Send + Sync + 'static,
    {
        let tool_name = name.to_string();
        Self {
            name: tool_name.clone(),
            description: description.to_string(),
            category: String::new(),
            parameters: parameters_schema::<A>(),
            run: Arc::new(move |arguments: &Value| {
                let typed: A = serde_json::from_value(arguments.clone()).map_err(|error| {
                    ToolError::Argument(format!(
                        "Invalid arguments for {tool_name:?}: {error}"
                    ))
                })?;
                run(typed)
            }),
        }
    }

    pub fn with_category(mut self, category: &str) -> Self {
        self.category = normalize_category(category);
        self
    }

    pub fn schema(&self) -> Value {
        json!({
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        })
    }

    fn call(&self, arguments: &Value) -> Result<Value, ToolError> {
        (self.run)(arguments)
    }
}

/// JSON Schema in the shape the Python registry emits: an object schema with no
/// `$schema`/`title` noise and no extra properties.
pub fn parameters_schema<A: JsonSchema>() -> Value {
    let mut value = serde_json::to_value(schemars::schema_for!(A)).unwrap_or_else(|_| json!({}));
    strip(&mut value);
    let object = value.as_object_mut().expect("schema is an object");
    object.insert("type".into(), Value::from("object"));
    object.insert("additionalProperties".into(), Value::from(false));
    value
}

fn strip(value: &mut Value) {
    if let Value::Object(map) = value {
        map.remove("$schema");
        map.remove("title");
        if let Some(properties) = map.get_mut("properties").and_then(Value::as_object_mut) {
            for property in properties.values_mut() {
                strip(property);
            }
            // pydantic's output has no `title` per property either; strip() above
            // already removed them, this loop keeps nested objects honest.
            for property in properties.values_mut() {
                if let Some(nested) = property.get_mut("properties").and_then(Value::as_object_mut) {
                    for inner in nested.values_mut() {
                        strip(inner);
                    }
                }
            }
        }
    }
}

pub fn normalize_category(category: &str) -> String {
    category
        .split('/')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .map(str::to_lowercase)
        .collect::<Vec<_>>()
        .join("/")
}

#[derive(Default)]
pub struct Registry {
    tools: BTreeMap<String, Arc<Tool>>,
    disabled: BTreeSet<String>,
}

impl Registry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn add(&mut self, tool: Tool) -> Result<(), ToolError> {
        if self.tools.contains_key(&tool.name) {
            return Err(ToolError::Argument(format!(
                "Tool {:?} is already registered",
                tool.name
            )));
        }
        self.tools.insert(tool.name.clone(), Arc::new(tool));
        Ok(())
    }

    pub fn get(&self, name: &str) -> Result<&Arc<Tool>, ToolError> {
        self.tools.get(name).ok_or_else(|| {
            ToolError::Unknown(format!(
                "Unknown tool: {name}. Available tools: {}",
                self.names().join(", ")
            ))
        })
    }

    pub fn names(&self) -> Vec<String> {
        self.tools.keys().cloned().collect()
    }

    pub fn enabled(&self, name: &str) -> bool {
        self.tools.contains_key(name) && !self.disabled.contains(name)
    }

    pub fn set_enabled(&mut self, name: &str, enabled: bool) -> Result<(), ToolError> {
        if !self.tools.contains_key(name) {
            return Err(ToolError::Unknown(format!("Unknown tool: {name}")));
        }
        if enabled {
            self.disabled.remove(name);
        } else {
            self.disabled.insert(name.to_string());
        }
        Ok(())
    }

    pub fn disabled(&self) -> Vec<String> {
        self.disabled
            .iter()
            .filter(|name| self.tools.contains_key(*name))
            .cloned()
            .collect()
    }

    pub fn schemas(&self) -> Vec<Value> {
        self.tools
            .values()
            .filter(|tool| !self.disabled.contains(&tool.name))
            .map(|tool| tool.schema())
            .collect()
    }

    pub fn subset(&self, names: &[&str]) -> Result<Registry, ToolError> {
        let mut scoped = Registry::new();
        for name in names {
            let tool = self.get(name)?;
            scoped.tools.insert((*name).to_string(), Arc::clone(tool));
        }
        for name in &self.disabled {
            if scoped.tools.contains_key(name) {
                scoped.disabled.insert(name.clone());
            }
        }
        Ok(scoped)
    }

    pub fn dispatch(&self, name: &str, arguments: &Value) -> Result<String, ToolError> {
        let tool = self.get(name)?;
        if self.disabled.contains(name) {
            return Err(ToolError::Disabled(format!(
                "Tool {name:?} is switched off and cannot be called."
            )));
        }
        let result = tool.call(arguments).map_err(|error| match error {
            ToolError::Argument(message) => ToolError::Argument(message),
            other => ToolError::Execution(format!("Tool {name:?} failed: {other}")),
        })?;
        Ok(match result {
            Value::String(text) => text,
            other => serde_json::to_string(&other).unwrap_or_else(|_| "{}".to_string()),
        })
    }

    /// Descriptions for a reader rather than a model, including disabled tools.
    pub fn describe(&self) -> Vec<Value> {
        self.tools
            .values()
            .map(|tool| {
                json!({
                    "name": tool.name,
                    "description": tool.description,
                    "enabled": !self.disabled.contains(&tool.name),
                    "category": tool.category,
                })
            })
            .collect()
    }

    pub fn as_map(&self) -> Map<String, Value> {
        self.tools
            .iter()
            .map(|(name, tool)| (name.clone(), tool.schema()))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Debug, Deserialize, JsonSchema)]
    struct EchoArgs {
        text: String,
        #[serde(default)]
        loud: bool,
    }

    fn echo(args: EchoArgs) -> Result<Value, ToolError> {
        let text = if args.loud {
            args.text.to_uppercase()
        } else {
            args.text
        };
        Ok(json!({"echoed": text, "length": text.chars().count()}))
    }

    fn registry() -> Registry {
        let mut registry = Registry::new();
        registry
            .add(Tool::new::<EchoArgs, _>("echo_text", "Echo text.", echo))
            .unwrap();
        registry
    }

    #[test]
    fn schema_is_normalised_like_the_python_registry() {
        let registry = registry();
        let schema = registry.get("echo_text").unwrap().parameters.clone();

        assert!(schema.get("title").is_none());
        assert!(schema.get("$schema").is_none());
        assert_eq!(schema["type"], "object");
        assert_eq!(schema["additionalProperties"], false);
        assert_eq!(schema["required"], json!(["text"]));
        assert_eq!(schema["properties"]["text"]["type"], "string");
        assert_eq!(schema["properties"]["loud"]["type"], "boolean");
        assert!(schema["properties"]["loud"].get("title").is_none());
    }

    #[test]
    fn dispatch_validates_and_serialises() {
        let registry = registry();
        let result = registry
            .dispatch("echo_text", &json!({"text": "hi", "loud": true}))
            .unwrap();
        assert_eq!(result, r#"{"echoed":"HI","length":2}"#);
    }

    #[test]
    fn bad_arguments_are_argument_errors() {
        let registry = registry();
        assert!(matches!(
            registry.dispatch("echo_text", &json!({})),
            Err(ToolError::Argument(_))
        ));
        assert!(matches!(
            registry.dispatch("echo_text", &json!({"text": 5})),
            Err(ToolError::Argument(_))
        ));
        // Unknown keys are ignored, as pydantic's default `extra="ignore"` does
        // on the Python side; `additionalProperties: false` is what the model is
        // told, not a stricter runtime than the original.
        assert_eq!(
            registry
                .dispatch("echo_text", &json!({"text": "hi", "nope": 1}))
                .unwrap(),
            r#"{"echoed":"hi","length":2}"#
        );
    }

    #[test]
    fn unknown_and_disabled_tools_are_refused_distinctly() {
        let mut registry = registry();
        assert!(matches!(
            registry.dispatch("nope", &json!({})),
            Err(ToolError::Unknown(_))
        ));
        registry.set_enabled("echo_text", false).unwrap();
        assert!(matches!(
            registry.dispatch("echo_text", &json!({"text": "hi"})),
            Err(ToolError::Disabled(_))
        ));
        assert!(registry.schemas().is_empty());
        assert_eq!(registry.names(), vec!["echo_text".to_string()]);
    }

    #[test]
    fn subset_carries_the_disabled_set() {
        let mut registry = registry();
        registry.set_enabled("echo_text", false).unwrap();
        let scoped = registry.subset(&["echo_text"]).unwrap();
        assert!(scoped.disabled().contains(&"echo_text".to_string()));
        assert!(scoped.schemas().is_empty());
    }

    #[test]
    fn duplicate_registration_is_refused() {
        let mut registry = registry();
        assert!(registry
            .add(Tool::new::<EchoArgs, _>("echo_text", "again", echo))
            .is_err());
    }
}

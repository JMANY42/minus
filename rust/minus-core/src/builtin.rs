//! Worked example tools for the Rust registry.
//!
//! The Python side has `tools/builtin/clock.py` and `files.py`; this crate has
//! one echo tool so the registry has something real to derive a schema from and
//! the loop tests have something real to dispatch. Anything filesystem-shaped
//! waits for the sidecar/loop phase where the workspace root is threaded in.

use serde::Deserialize;
use serde_json::{json, Value};

use crate::registry::{Registry, Tool, ToolError};

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct EchoArgs {
    /// Text to echo back.
    pub text: String,
    /// Upper-case the echoed text.
    #[serde(default)]
    pub loud: bool,
}

pub fn echo_text(args: EchoArgs) -> Result<Value, ToolError> {
    let text = if args.loud {
        args.text.to_uppercase()
    } else {
        args.text
    };
    Ok(json!({"echoed": text, "length": text.chars().count()}))
}

pub fn registry_with_builtins() -> Result<Registry, ToolError> {
    let mut registry = Registry::new();
    registry.add(Tool::new::<EchoArgs, _>(
        "echo_text",
        "Echo text back. Returns the text and its length.",
        echo_text,
    ))?;
    Ok(registry)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_builtin_registry_advertises_and_runs_the_tool() {
        let registry = registry_with_builtins().unwrap();

        assert_eq!(registry.names(), vec!["echo_text".to_string()]);
        let schema = &registry.schemas()[0]["function"]["parameters"];
        assert_eq!(schema["required"], json!(["text"]));
        assert_eq!(schema["properties"]["text"]["description"], "Text to echo back.");

        let result = registry
            .dispatch("echo_text", &json!({"text": "hi", "loud": true}))
            .unwrap();
        assert_eq!(result, r#"{"echoed":"HI","length":2}"#);
    }
}

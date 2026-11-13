//! The one tool loop both tiers run, ported from `src/minus/core/loop.py`.
//!
//! Two rules are load-bearing and are copied rather than re-derived:
//!   * a round whose tool calls all failed does not spend the productive-round
//!     budget — the model sent bad arguments and deserves a correction;
//!   * running out of rounds is not an error — ask once more with no tools
//!     offered and return whatever comes back.
//!
//! `max_attempts` is the absolute ceiling that makes the first rule safe; the
//! caller sets it to `max(max_tool_rounds, 1) * 2`.

use std::fmt;
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::registry::{Registry, ToolError};

pub const RETRY_NOTE: &str = "The previous attempt failed to generate a valid tool call. \
Return a valid response that matches the tool schema exactly. Do not repeat malformed \
arguments or duplicate keys.";

fn function_kind() -> String {
    "function".to_string()
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct FunctionCall {
    pub name: String,
    pub arguments: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCall {
    pub id: String,
    #[serde(rename = "type", default = "function_kind")]
    pub kind: String,
    pub function: FunctionCall,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Message {
    pub role: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tool_calls: Vec<ToolCall>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
}

impl Message {
    pub fn system(content: impl Into<String>) -> Self {
        Self {
            role: "system".into(),
            content: Some(content.into()),
            ..Self::default()
        }
    }

    pub fn user(content: impl Into<String>) -> Self {
        Self {
            role: "user".into(),
            content: Some(content.into()),
            ..Self::default()
        }
    }

    pub fn assistant(content: impl Into<String>) -> Self {
        Self {
            role: "assistant".into(),
            content: Some(content.into()),
            ..Self::default()
        }
    }

    pub fn tool_result(tool_call_id: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            role: "tool".into(),
            content: Some(content.into()),
            tool_call_id: Some(tool_call_id.into()),
            ..Self::default()
        }
    }

    pub fn text(&self) -> &str {
        self.content.as_deref().unwrap_or("")
    }
}

#[derive(Debug, Clone)]
pub enum LlmError {
    Transport(String),
    Malformed(String),
    Http(String),
}

impl fmt::Display for LlmError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Transport(message) | Self::Malformed(message) | Self::Http(message) => {
                f.write_str(message)
            }
        }
    }
}

impl std::error::Error for LlmError {}

#[derive(Debug, Clone)]
pub struct CompletionRequest {
    pub messages: Vec<Message>,
    pub model: Option<String>,
    pub system_prompt: Option<String>,
    pub tools: Option<Vec<Value>>,
    pub tool_choice: String,
    pub reasoning_effort: Option<String>,
    pub retry_note: Option<String>,
}

pub struct Completion {
    pub message: Message,
}

pub trait ChatModel {
    fn complete(&self, request: &CompletionRequest) -> Result<Completion, LlmError>;
}

fn generation_failure(error: &LlmError) -> Message {
    Message::system(format!(
        "The previous assistant turn could not be generated: {error} Do not repeat that \
         attempt. Either call one of the available tools with valid arguments, or answer the \
         user in plain text."
    ))
}

fn tool_failure(name: &str, error: impl fmt::Display) -> String {
    format!(
        "Tool execution failed for {name:?}: {error}. Please retry with valid arguments, or \
         answer without the tool."
    )
}

pub struct ToolLoop {
    pub model: Arc<dyn ChatModel>,
    pub messages: Vec<Message>,
    pub registry: Option<Arc<Registry>>,
    pub system_prompt: Option<String>,
    pub model_name: Option<String>,
    pub max_tool_rounds: usize,
    pub max_attempts: usize,
    pub reasoning_effort: Option<String>,
    pub retry_note: Option<String>,
}

impl ToolLoop {
    pub fn new(model: Arc<dyn ChatModel>, max_tool_rounds: usize) -> Self {
        Self {
            model,
            messages: Vec::new(),
            registry: None,
            system_prompt: None,
            model_name: None,
            max_tool_rounds,
            max_attempts: std::cmp::max(max_tool_rounds, 1) * 2,
            reasoning_effort: None,
            retry_note: Some(RETRY_NOTE.to_string()),
        }
    }

    fn request(&self, tools: Option<Vec<Value>>) -> CompletionRequest {
        CompletionRequest {
            messages: self.messages.clone(),
            model: self.model_name.clone(),
            system_prompt: self.system_prompt.clone(),
            tools: tools.filter(|tools| !tools.is_empty()),
            tool_choice: "auto".to_string(),
            reasoning_effort: self.reasoning_effort.clone(),
            retry_note: self.retry_note.clone(),
        }
    }

    fn dispatch(&self, call: &ToolCall) -> Result<String, ToolError> {
        let Some(registry) = self.registry.as_ref() else {
            return Err(ToolError::Unknown(format!(
                "Tool {} is not available.",
                call.function.name
            )));
        };
        let arguments: Value = serde_json::from_str(&call.function.arguments).map_err(|error| {
            ToolError::Argument(format!(
                "Arguments for {:?} were not valid JSON: {error}",
                call.function.name
            ))
        })?;
        registry.dispatch(&call.function.name, &arguments)
    }

    /// Run to a final assistant turn, appending everything on the way.
    pub fn run(&mut self) -> Result<Message, ToolError> {
        let schemas = self.registry.as_ref().map(|registry| registry.schemas());
        let mut completed_rounds = 0usize;

        for _ in 0..self.max_attempts.max(1) {
            if completed_rounds >= self.max_tool_rounds {
                break;
            }

            let completion = match self.model.complete(&self.request(schemas.clone())) {
                Ok(completion) => completion,
                Err(error) => {
                    self.messages.push(generation_failure(&error));
                    continue;
                }
            };

            let message = completion.message;
            if message.tool_calls.is_empty() {
                let final_message = Message {
                    content: Some(message.text().trim().to_string()),
                    ..message
                };
                self.messages.push(final_message.clone());
                return Ok(final_message);
            }

            self.messages.push(message.clone());

            let mut failed = false;
            for call in &message.tool_calls {
                match self.dispatch(call) {
                    Ok(result) => self.messages.push(Message::tool_result(&call.id, result)),
                    Err(error) => {
                        failed = true;
                        self.messages
                            .push(Message::tool_result(&call.id, tool_failure(&call.function.name, error)));
                    }
                }
            }

            if !failed {
                completed_rounds += 1;
            }
        }

        self.forced_answer()
    }

    fn forced_answer(&mut self) -> Result<Message, ToolError> {
        let completion = self.model.complete(&self.request(None)).map_err(|error| {
            ToolError::Generation(format!(
                "Tool call limit reached and the final answer could not be generated: {error}"
            ))
        })?;

        let message = completion.message;
        if message.text().trim().is_empty() {
            return Err(ToolError::Generation(
                "Tool call limit reached and the model produced no final response.".to_string(),
            ));
        }
        let final_message = Message {
            content: Some(message.text().trim().to_string()),
            ..message
        };
        self.messages.push(final_message.clone());
        Ok(final_message)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tool_calls_serialise_in_the_openai_wire_shape() {
        let message = Message {
            role: "assistant".into(),
            content: None,
            tool_calls: vec![ToolCall {
                id: "c1".into(),
                kind: "function".into(),
                function: FunctionCall {
                    name: "echo_text".into(),
                    arguments: "{}".into(),
                },
            }],
            tool_call_id: None,
        };
        let wire = serde_json::to_value(&message).unwrap();

        assert_eq!(wire["tool_calls"][0]["type"], "function");
        assert_eq!(wire["tool_calls"][0]["function"]["name"], "echo_text");
        assert!(wire.get("content").is_none(), "absent content is omitted");
    }
}

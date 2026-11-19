-- The smallest complete plugin: a worked example of the contract, and the one
-- `tests/test_plugins.py` and `minus tools` exercise. Copy this directory to
-- start a new one.
return {
  name = "echo_text",
  description = "Echo text back, for checking that plugins are loaded. "
             .. "Returns the text and its length.",
  category = "",
  args = {
    text = {
      type = "string",
      description = "Text to echo back.",
      required = true,
    },
    loud = {
      type = "boolean",
      description = "Upper-case the echoed text.",
      default = false,
    },
  },
  run = function(args)
    local text = args.text
    if args.loud then
      text = string.upper(text)
    end
    log("echo_text called with " .. tostring(#text) .. " characters")
    return { echoed = text, length = #text }
  end,
}

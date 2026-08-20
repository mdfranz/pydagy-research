# Google Antigravity Web Search Capabilities: Python SDK vs. Antigravity CLI (`agy`)

## 1. Repository Overview: `google-antigravity/antigravity-sdk-python`

The [`antigravity-sdk-python`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python) repository contains the official Python SDK (`google-antigravity`) for leasing, configuring, and orchestrating Google Antigravity agents backed by Gemini and Gemini Enterprise (Vertex AI).

### Core Three-Tier Architecture
1. **Layer 1 (`High-Level / Agent`)**:
   - The [`Agent`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/agent.py#L34-L150) async context manager handles binary discovery, localharness lifecycle, tool wiring, safety policy enforcement, and streaming.
2. **Layer 2 (`Session / Conversation`)**:
   - [`Conversation`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/conversation/conversation.py), [`HookRunner`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/hooks/hook_runner.py), [`ToolRunner`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/tools/tool_runner.py), and [`TriggerRunner`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/triggers/trigger_runner.py) manage state history, lifecycle interception, background event triggers, and custom tools.
3. **Layer 3 (`Transport / Connections`)**:
   - [`LocalConnection`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/connections/local/local_connection.py) and [`LocalConnectionStrategy`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/connections/local/local_connection_strategy.py) communicate via protobuf ([`localharness.proto`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/proto/localharness.proto)) with the platform-native compiled `localharness` runtime binary.

---

## 2. Built-in Web Search & Retrieval Capabilities

The SDK exposes two built-in tools for live web interaction:

### A. `search_web` ([`BuiltinTools.SEARCH_WEB`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/types.py#L286))
* **Functionality**: Performs a live Google Search query to ground the model with real-time web information.
* **Input Schema** ([`ActionSearchWeb`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/proto/localharness.proto#L363-L367)):
  * `query: str` (required) — The search keywords or phrasing.
  * `domain: str` (optional) — Restricts or prioritizes search results to a specific domain.
* **Output Schema** ([`SearchWebResult`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/connections/local/types.py#L112-L119)):
  * `summary: str` — Synthesized Google Search results with extracted snippets and source citations.
* **Classification**: Included in [`BuiltinTools.nondestructive()`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/types.py#L307-L326) (and `all_tools()`), excluded from `read_only()`.

### B. `read_url_content` ([`BuiltinTools.READ_URL_CONTENT`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/types.py#L287))
* **Functionality**: Performs HTTP fetching and parses web pages directly to Markdown/text (without JavaScript execution).
* **Input Schema** ([`ActionReadUrlContent`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/proto/localharness.proto#L368-L373)):
  * `url: str` (required) — The target URL to read.
* **Output Schema** ([`ReadUrlContentResult`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/connections/local/types.py#L121-L130)):
  * `title: str` — Extracted page title.
  * `summary: str` — Text / markdown representation.
  * `content_path: str` — Local cached disk path for large web documents (agent can inspect full content using `view_file`).
* **Classification**: Included in [`BuiltinTools.read_only()`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/types.py#L291-L304).

### SDK Usage Example
From [`examples/getting_started/web_tools.py`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/examples/getting_started/web_tools.py):

```python
import asyncio
from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig, types

async def main():
    config = LocalAgentConfig(
        capabilities=CapabilitiesConfig(
            enabled_tools=[
                types.BuiltinTools.SEARCH_WEB,
                types.BuiltinTools.READ_URL_CONTENT,
                types.BuiltinTools.VIEW_FILE,
            ]
        ),
    )

    async with Agent(config) as agent:
        prompt = "What is the latest release date of Python 3.13? Provide sources."
        response = await agent.chat(prompt)
        print(await response.text())

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 3. Detailed Comparison: Python SDK vs. Antigravity CLI (`agy`)

| Feature | **Antigravity CLI (`agy`)** | **Antigravity Python SDK (`google-antigravity`)** |
| :--- | :--- | :--- |
| **Primary Use Case** | Interactive developer terminal UI (TUI) for hands-on coding, debugging, and terminal chat. | Headless pipelines, batch processing, custom automation scripts, multi-agent frameworks, and backend services. |
| **Underlying Engine** | Powered by the compiled `localharness` runtime binary and Google Search backend. | Connects to the exact same compiled runtime binary and protobuf action schema via IPC. |
| **Configuration & Activation** | Tools are active by default in interactive sessions; global preferences set in `~/.gemini/antigravity-cli/settings.json`. | Explicitly configured per agent via [`CapabilitiesConfig`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/types.py#L363) (`enabled_tools` / `disabled_tools`). |
| **Lifecycle & Interception** | Interactive user prompts in the terminal for tool approval. | Programmatic interception using Python hooks ([`@pre_tool_call`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/hooks/hooks.py), [`@post_tool_call`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/hooks/hooks.py)) and declarative policies ([`policy.deny()`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/hooks/policy.py), [`policy.ask_user()`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/hooks/policy.py)). |
| **Subagent Delegation** | Spawns background subagents interactively via slash commands (`/goal`, `/plan`) or model delegation. | Programmatically configure specialized subagents (e.g. a `lead_researcher` restricted solely to `SEARCH_WEB` and `READ_URL_CONTENT`). |
| **Observability & Output** | Renders live TUI widgets, spinners, collapsible tool badges, and streaming text. | Emits strongly typed objects ([`SearchWebResult`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/connections/local/types.py#L112), [`ReadUrlContentResult`](file:///Users/mdfranz/tmp/repos/antigravity-sdk-python/google/antigravity/connections/local/types.py#L121)), streams reasoning thoughts, and exports OpenTelemetry traces. |
| **Authentication Modes** | Interactive terminal login flow (`gcloud` / user credentials). | Application Default Credentials (ADC), Vertex AI Express Mode (`VertexEndpoint`), and explicit API keys. |

---

## 4. Key Takeaways

1. **Shared Foundation**: Both the CLI and SDK utilize the identical underlying search action protocol (`ActionSearchWeb` and `ActionReadUrlContent`) backed by the compiled `localharness` runtime and Google Search.
2. **Operational Distinction**: The CLI provides a ready-to-use interactive TUI for developer workflows, while the SDK exposes fine-grained programmatic control over tool scoping, policy gates, subagent trees, and telemetry pipelines.

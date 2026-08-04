from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from typing import Any, Literal

from jsonschema import validate as validate_json_schema
from pydantic import BaseModel, ConfigDict, Field

from backend.agent_nodes import AgentNode, PythonCodeNode
from backend.documents import DocumentService
from backend.ollama import OllamaError
from backend.registry import BaseNode, NodeExecutionContext, PortDefinition, ToolParameter, ToolSpec
from backend.retrieval import RetrievalService
from backend.conditions import evaluate_condition
from backend.schemas import ConditionRule, ModelReference, WorkflowDefinition, WorkflowPort
from backend.storage import StorageError
from backend.utils import dumps_json, truncate_text


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return dumps_json(value)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _model_reference(config: Any) -> ModelReference | None:
    reference = getattr(config, "model_reference", None)
    if reference is not None:
        return reference
    profile_id = getattr(config, "provider_profile_id", None)
    model = getattr(config, "model", None) or getattr(config, "llm_model", None)
    return ModelReference(provider_profile_id=profile_id, model=model) if profile_id or model else None


def _render_context(items: list[dict[str, Any]]) -> str:
    return RetrievalService.render_context(items)


_SLUG_LABEL_PREFIX = re.compile(r"^[A-Za-z][A-Za-z ]{0,23}:\s*")


def _slugify(value: Any, fallback: str = "") -> str:
    """Turns a model-written name into one safe, readable folder segment.

    Small models like to answer with decoration ("**Name:** Sparse Attention"), so the
    first meaningful line is stripped of markdown, quotes and any short "label:" prefix
    before it becomes a lowercase hyphenated slug.
    """
    text = ""
    for line in _as_text(value).splitlines():
        candidate = line.strip().strip("#*`_-").strip().strip("\"'").strip()
        if candidate:
            text = candidate
            break
    text = _SLUG_LABEL_PREFIX.sub("", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)[:60].strip("-")
    return slug or fallback


def _note_text(value: Any) -> str:
    """Pulls the readable body out of a subflow result, which may be text or a payload."""
    if isinstance(value, dict):
        for key in ("content", "text", "result", "markdown", "value"):
            if value.get(key) is not None:
                return _as_text(value[key])
    return _as_text(value)


def _group_items(items: list[Any], group_size: int, separator: str) -> list[Any]:
    """Batches items so one subflow run can cover several chunks.

    Text items are joined so the subflow still sees a single readable passage; anything
    else is handed over as a list, and a group size of one passes items through untouched.
    """
    if group_size <= 1:
        return list(items)
    groups: list[Any] = []
    for start in range(0, len(items), group_size):
        batch = items[start : start + group_size]
        if len(batch) == 1:
            groups.append(batch[0])
        elif all(isinstance(item, str) for item in batch):
            groups.append(separator.join(batch))
        else:
            groups.append(batch)
    return groups


def _collapse_subflow(result: Any) -> Any:
    if isinstance(result, dict) and "result" in result:
        return result["result"]
    return result


def coerce_port_value(value: Any, kind: str, label: str) -> Any:
    """Checks a run-supplied value against a declared port kind, coercing where unambiguous."""
    if value is None or kind == "any":
        return value
    if kind == "text":
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return _as_text(value)
        raise ValueError(f"{label} expects text but received {type(value).__name__}")
    if kind == "number":
        if isinstance(value, bool):
            raise ValueError(f"{label} expects a number but received a boolean")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value) if "." in value or "e" in value.lower() else int(value)
            except ValueError as exc:
                raise ValueError(f"{label} expects a number but received {value!r}") from exc
        raise ValueError(f"{label} expects a number but received {type(value).__name__}")
    if kind == "list":
        if isinstance(value, list):
            return value
        raise ValueError(f"{label} expects a list but received {type(value).__name__}")
    if kind == "json":
        if isinstance(value, (dict, list)):
            return value
        raise ValueError(f"{label} expects a JSON object or array but received {type(value).__name__}")
    return value


class TextInputConfig(BaseModel):
    input_key: str | None = None
    value: Any = None


PortKind = Literal["any", "text", "number", "json", "list"]


class WorkflowInputConfig(BaseModel):
    key: str = Field(default="input", min_length=1)
    kind: PortKind = "any"
    label: str = ""
    description: str = ""
    required: bool = True
    default: Any = None


class WorkflowOutputConfig(BaseModel):
    key: str = Field(default="result", min_length=1)
    label: str = ""
    description: str = ""


class PromptTemplateConfig(BaseModel):
    template: str


class PromptBuilderConfig(BaseModel):
    output_format: Literal["text", "markdown", "json"] = "markdown"
    require_citations: bool = True


class OllamaGenerateConfig(BaseModel):
    model: str | None = None
    provider_profile_id: str | None = None
    model_reference: ModelReference | None = None
    mode: Literal["generate", "chat"] = "generate"
    stream: bool = True
    temperature: float | None = None
    system_prompt: str | None = None
    format: dict[str, Any] | str | None = None


class JsonParseConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    json_schema: dict[str, Any] | None = Field(default=None, alias="schema")
    retries: int = Field(default=0, ge=0, le=3)


class PdfIngestConfig(BaseModel):
    enhance_with_llm: bool | None = None
    model: str | None = None
    provider_profile_id: str | None = None
    model_reference: ModelReference | None = None
    llm_model: str | None = None
    triage_model: str | None = None


class EnhancePageConfig(BaseModel):
    model: str | None = None
    provider_profile_id: str | None = None
    model_reference: ModelReference | None = None
    llm_model: str | None = None
    triage_model: str | None = None


class SelectDocumentConfig(BaseModel):
    include_chunks: bool = True


class FileConfig(BaseModel):
    path: str = ""


class WriteNoteFolderConfig(BaseModel):
    #: Optional parent folder inside the workspace, e.g. "papers". Empty writes at the top level.
    base_dir: str = ""
    #: Folder name to use when nothing is wired into the ``folder`` port.
    folder: str = ""
    #: File name (without suffix) for the top-level summary.
    summary_name: str = "summary"
    #: File name prefix for each mapped note, numbered in order.
    item_prefix: str = "section"
    #: Heading written above each mapped note.
    item_heading: str = "Section"
    #: Markdown skipped by the summariser is dropped instead of written as an empty note.
    skip_marker: str = "SKIP"
    #: Clears the folder's existing Markdown first, so a rerun does not leave stale notes.
    replace_existing: bool = True


class IndexChunksConfig(BaseModel):
    model: str | None = None
    provider_profile_id: str | None = None
    model_reference: ModelReference | None = None


class RetrieveConfig(BaseModel):
    document_id: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)
    model: str | None = None
    provider_profile_id: str | None = None
    model_reference: ModelReference | None = None


class FullContextConfig(BaseModel):
    document_id: str | None = None
    max_chars: int | None = None


class MapSubflowConfig(BaseModel):
    subflow: dict[str, Any]
    item_key: str = "item"
    index_key: str = "index"
    max_items: int = Field(default=512, ge=1)
    #: What to do when the list is longer than ``max_items``. Truncating with a warning
    #: keeps a large paper producing a partial summary instead of failing outright.
    on_overflow: Literal["truncate", "error"] = "truncate"
    #: Number of items handed to each subflow run. Batching keeps a 124-chunk document
    #: from becoming 124 sequential model calls.
    group_size: int = Field(default=1, ge=1, le=64)
    #: How many subflow runs may be in flight at once.
    concurrency: int = Field(default=4, ge=1, le=16)
    group_separator: str = "\n\n"


class ReduceCombineConfig(BaseModel):
    mode: Literal["join_text", "json_merge", "list"] = "join_text"
    join_with: str = "\n\n"


class RepeatSubflowConfig(BaseModel):
    subflow: dict[str, Any]
    max_iterations: int = Field(default=3, ge=1)
    value_key: str = "seed"
    stop_when_field: str | None = "stop"
    carry_field: str | None = None


class MergeConfig(BaseModel):
    pass


class IfElseConfig(BaseModel):
    condition: ConditionRule


class ChunkBudgetConfig(BaseModel):
    max_chars: int = Field(default=4000, ge=1)


class FinalOutputConfig(BaseModel):
    artifact_name: str | None = None
    format: Literal["json", "md", "txt"] = "json"
    output_key: str = Field(default="result", min_length=1)


class TextInputNode(BaseNode):
    type_name = "text_input"
    label = "Text/Input"
    description = "Injects a static or run-provided value into the workflow."
    category = "inputs"
    outputs = [PortDefinition("value", "any"), PortDefinition("text", "text")]
    config_model = TextInputConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: TextInputConfig) -> dict[str, Any]:
        value = context.run_inputs.get(config.input_key) if config.input_key else config.value
        return {"value": value, "text": _as_text(value)}


class WorkflowInputNode(BaseNode):
    type_name = "workflow_input"
    label = "Workflow Input"
    description = "Declares a named, typed input on the workflow's public interface."
    category = "inputs"
    tags = ["interface", "parameter"]
    outputs = [PortDefinition("value", "any"), PortDefinition("text", "text")]
    config_model = WorkflowInputConfig
    interface_role = "input"
    interface_value_port = "value"

    def interface_port(self, node_id: str, config: Any) -> WorkflowPort | None:
        if not isinstance(config, WorkflowInputConfig):
            config = WorkflowInputConfig.model_validate(config)
        return WorkflowPort(
            key=config.key,
            kind=config.kind,
            label=config.label or config.key,
            description=config.description,
            required=config.required and config.default is None,
            default=config.default,
            node_id=node_id,
        )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: WorkflowInputConfig) -> dict[str, Any]:
        label = f"Workflow input '{config.key}'"
        if config.key in context.run_inputs:
            value = context.run_inputs[config.key]
        elif config.default is not None:
            value = config.default
        elif config.required:
            raise ValueError(f"{label} is required but was not supplied")
        else:
            value = None
        value = coerce_port_value(value, config.kind, label)
        return {"value": value, "text": _as_text(value)}


class PromptTemplateNode(BaseNode):
    type_name = "prompt_template"
    label = "Prompt Template"
    description = "Formats a prompt from explicit variable bindings."
    category = "llm"
    inputs = [PortDefinition("variables", "json", required=False)]
    outputs = [PortDefinition("prompt", "text")]
    config_model = PromptTemplateConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: PromptTemplateConfig) -> dict[str, Any]:
        variables = inputs.get("variables")
        if not isinstance(variables, dict):
            variables = {key: value for key, value in inputs.items() if key != "variables"}
        prompt = config.template.format_map({key: _as_text(value) for key, value in variables.items()})
        return {"prompt": prompt}


class PromptBuilderNode(BaseNode):
    type_name = "prompt_builder"
    label = "Prompt Builder"
    description = "Builds a grounded prompt from goals, context, and output constraints."
    category = "llm"
    inputs = [
        PortDefinition("goal", "text", required=False),
        PortDefinition("context", "text", required=False),
        PortDefinition("constraints", "text", required=False),
        PortDefinition("output_contract", "text", required=False),
    ]
    outputs = [PortDefinition("prompt", "text")]
    config_model = PromptBuilderConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: PromptBuilderConfig) -> dict[str, Any]:
        prompt = (
            "You are helping with a local research workflow.\n"
            f"Goal:\n{_as_text(inputs.get('goal')) or 'Summarize the supplied material.'}\n\n"
            f"Context:\n{_as_text(inputs.get('context'))}\n\n"
            f"Constraints:\n{_as_text(inputs.get('constraints')) or 'Be concise and grounded in the provided evidence.'}\n\n"
            f"Output contract ({config.output_format}):\n{_as_text(inputs.get('output_contract')) or 'Return the requested answer.'}"
        )
        if config.require_citations:
            prompt += "\n\nCite the supporting page or chunk labels for every factual claim."
        return {"prompt": prompt}


class OllamaGenerateNode(BaseNode):
    type_name = "ollama_generate"
    label = "Model Generate / Chat"
    description = "Calls the selected chat model with optional token streaming."
    category = "llm"
    inputs = [PortDefinition("prompt", "text", required=False), PortDefinition("messages", "json", required=False)]
    outputs = [PortDefinition("text", "text"), PortDefinition("raw", "json")]
    config_model = OllamaGenerateConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: OllamaGenerateConfig) -> dict[str, Any]:
        resolved = context.services.model_runtime.resolve(
            "chat", node_reference=_model_reference(config), workflow_defaults=context.workflow_model_defaults
        )

        async def on_token(token: str) -> None:
            context.check_cancelled()
            await context.emit("token", {"node_path": context.node_path, "token": token})

        if config.mode == "chat":
            messages = inputs.get("messages")
            if not isinstance(messages, list):
                messages = [{"role": "user", "content": _as_text(inputs.get("prompt"))}]
            response = await context.services.model_runtime.generate(
                resolved,
                "",
                messages=messages,
                temperature=config.temperature,
                format_=config.format,
                on_token=on_token if config.stream else None,
            )
            return {"text": response.get("message", {}).get("content", ""), "raw": response}
        response = await context.services.model_runtime.generate(
            resolved,
            _as_text(inputs.get("prompt")),
            system=config.system_prompt,
            temperature=config.temperature,
            format_=config.format,
            on_token=on_token if config.stream else None,
        )
        return {"text": response.get("response", ""), "raw": response}


class OllamaEmbedNode(BaseNode):
    type_name = "ollama_embed"
    label = "Model Embeddings"
    description = "Embeds text or chunk collections with the selected provider."
    category = "llm"
    inputs = [PortDefinition("text", "text", required=False), PortDefinition("texts", "json", required=False)]
    outputs = [PortDefinition("embeddings", "json")]
    config_model = IndexChunksConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: IndexChunksConfig) -> dict[str, Any]:
        resolved = context.services.model_runtime.resolve(
            "embedding", node_reference=_model_reference(config), workflow_defaults=context.workflow_model_defaults
        )
        texts = inputs.get("texts")
        if texts is None:
            texts = [_as_text(inputs.get("text"))]
        elif isinstance(texts, str):
            texts = [texts]
        embeddings = await context.services.model_runtime.embed(resolved, texts)
        return {"embeddings": embeddings}


class PlainTextNode(BaseNode):
    type_name = "plain_text"
    label = "Plain Text"
    description = "Coerces values into plain text."
    category = "parsing"
    inputs = [PortDefinition("value", "any")]
    outputs = [PortDefinition("text", "text")]

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        return {"text": _as_text(inputs.get("value"))}


class MarkdownNode(BaseNode):
    type_name = "markdown"
    label = "Markdown"
    description = "Formats values as Markdown text."
    category = "parsing"
    inputs = [PortDefinition("value", "any")]
    outputs = [PortDefinition("markdown", "text")]

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        value = inputs.get("value")
        if isinstance(value, list):
            body = "\n".join(f"- {_as_text(item)}" for item in value)
        else:
            body = _as_text(value)
        return {"markdown": body}


class JsonParseNode(BaseNode):
    type_name = "json_parse"
    label = "JSON Parser"
    description = "Parses and validates structured JSON output."
    category = "parsing"
    inputs = [PortDefinition("text", "text", required=False), PortDefinition("value", "any", required=False)]
    outputs = [PortDefinition("value", "json")]
    config_model = JsonParseConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: JsonParseConfig) -> dict[str, Any]:
        raw = inputs.get("value") if inputs.get("value") is not None else inputs.get("text")
        if isinstance(raw, str):
            last_error: Exception | None = None
            for _ in range(config.retries + 1):
                try:
                    parsed = json.loads(raw)
                    if config.json_schema:
                        validate_json_schema(parsed, config.json_schema)
                    return {"value": parsed}
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
            raise ValueError(f"JSON parsing failed: {last_error}")
        if config.json_schema and raw is not None:
            validate_json_schema(raw, config.json_schema)
        return {"value": raw}


class PdfIngestNode(BaseNode):
    type_name = "pdf_ingest"
    label = "PDF Ingest"
    description = "OCRs pages, triages OCR quality with a small vision LLM, rewrites only the poor pages, extracts figures, and stores artifacts."
    category = "documents"
    inputs = [PortDefinition("document_id", "text")]
    outputs = [
        PortDefinition("document", "json"),
        PortDefinition("chunks", "json"),
        PortDefinition("artifact_ids", "json"),
        PortDefinition("text", "text"),
    ]
    config_model = PdfIngestConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: PdfIngestConfig) -> dict[str, Any]:
        async def report_progress(payload: dict[str, Any]) -> None:
            context.check_cancelled()
            await context.emit("progress", {"node_path": context.node_path, **payload})

        result = await context.services.documents.ingest_document(
            _as_text(inputs["document_id"]),
            enhance_with_llm=config.enhance_with_llm,
            llm_model=config.llm_model,
            model_reference=_model_reference(config),
            workflow_defaults=context.workflow_model_defaults,
            triage_model=config.triage_model,
            progress=report_progress,
        )
        details = context.services.documents.get_document_details(result["document_id"])
        if not details:
            raise ValueError("Ingested document not found")
        document, _, _ = details
        return {"document": {"id": document.id, "title": document.title, "status": document.status}, **result}


class EnhanceDocumentPageNode(BaseNode):
    type_name = "enhance_document_page"
    label = "Enhance OCR Page"
    description = "Forces LLM Markdown reconstruction for one already-ingested OCR page, regardless of its triage rating."
    category = "documents"
    inputs = [
        PortDefinition("document_id", "text"),
        PortDefinition("page_number", "number"),
    ]
    outputs = [PortDefinition("document", "json"), PortDefinition("page", "json")]
    config_model = EnhancePageConfig

    async def execute(
        self,
        context: NodeExecutionContext,
        inputs: dict[str, Any],
        config: EnhancePageConfig,
    ) -> dict[str, Any]:
        try:
            page_number = int(inputs["page_number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("page_number must be a whole number") from exc
        async def report_progress(payload: dict[str, Any]) -> None:
            context.check_cancelled()
            await context.emit("progress", {"node_path": context.node_path, **payload})

        result = await context.services.documents.enhance_document_page(
            _as_text(inputs["document_id"]),
            page_number,
            llm_model=config.llm_model,
            model_reference=_model_reference(config),
            workflow_defaults=context.workflow_model_defaults,
            triage_model=config.triage_model,
            progress=report_progress,
        )
        return {
            "document": {
                "id": result["document_id"],
                "page_count": result["page_count"],
            },
            "page": result["page"],
        }


class SelectDocumentNode(BaseNode):
    type_name = "select_document"
    label = "Select Document"
    description = "Loads a document, chunk list, and concatenated text."
    category = "documents"
    inputs = [PortDefinition("document_id", "text")]
    outputs = [PortDefinition("document", "json"), PortDefinition("chunks", "json"), PortDefinition("text", "text")]
    config_model = SelectDocumentConfig
    tool_spec = ToolSpec(
        name="load_document",
        description="Loads a stored paper and returns its full text with citations.",
        parameters=(
            ToolParameter(port="document_id", description="The id of the document to load."),
        ),
        result_port="text",
    )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: SelectDocumentConfig) -> dict[str, Any]:
        details = context.services.documents.get_document_details(_as_text(inputs["document_id"]))
        if not details:
            raise ValueError("Document not found")
        document, _, chunks = details
        payload = [
            {
                "chunk_id": chunk.id,
                "chunk_index": chunk.chunk_index,
                "section_title": chunk.section_title,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "citation": chunk.citation,
                "text": chunk.text,
                "metadata": chunk.metadata_json or {},
            }
            for chunk in chunks
        ]
        return {
            "document": {"id": document.id, "title": document.title, "status": document.status},
            "chunks": payload if config.include_chunks else [],
            "text": _render_context(payload),
        }


class DocumentSplitterNode(BaseNode):
    type_name = "document_splitter"
    label = "Document/Section Splitter"
    description = "Splits text into bounded chunks with synthetic provenance."
    category = "documents"
    inputs = [PortDefinition("text", "text")]
    outputs = [PortDefinition("chunks", "json")]

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        text = _as_text(inputs.get("text"))
        words = text.split()
        chunk_size = max(100, context.services.settings.max_chunk_chars // 8)
        chunks = []
        for index in range(0, len(words), chunk_size):
            snippet = " ".join(words[index : index + chunk_size])
            chunks.append(
                {
                    "chunk_index": len(chunks),
                    "section_title": "Generated Section",
                    "page_start": 1,
                    "page_end": 1,
                    "citation": f"chunk-{len(chunks)}",
                    "text": snippet,
                    "metadata": {"source": "document_splitter"},
                }
            )
        return {"chunks": chunks}


class ReadWorkspaceNode(BaseNode):
    type_name = "read_workspace"
    label = "Read Text/Markdown/JSON"
    description = "Reads a safe UTF-8 text, markdown, or JSON file from the workspace."
    category = "files"
    inputs = [PortDefinition("path", "text", required=False)]
    outputs = [PortDefinition("value", "any"), PortDefinition("text", "text")]
    config_model = FileConfig
    tool_spec = ToolSpec(
        name="read_workspace_file",
        description="Reads a text, markdown or JSON file from the workspace folder.",
        parameters=(
            ToolParameter(port="path", description="Path relative to the workspace folder."),
        ),
        result_port="text",
    )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: FileConfig) -> dict[str, Any]:
        path = _as_text(inputs.get("path") or config.path)
        media_type, content = context.services.storage.read_workspace_file(path)
        return {"value": content, "text": _as_text(content), "media_type": media_type}


class WriteWorkspaceNode(BaseNode):
    type_name = "write_workspace"
    label = "Write Text/Markdown/JSON"
    description = "Writes a safe UTF-8 text, markdown, or JSON file inside the workspace."
    category = "files"
    inputs = [PortDefinition("content", "any"), PortDefinition("path", "text", required=False)]
    outputs = [PortDefinition("path", "text")]
    config_model = FileConfig
    tool_spec = ToolSpec(
        name="write_workspace_file",
        description="Writes a text, markdown or JSON file into the workspace folder.",
        parameters=(
            ToolParameter(port="content", description="What to write."),
            ToolParameter(port="path", description="Path relative to the workspace folder."),
        ),
        result_port="path",
    )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: FileConfig) -> dict[str, Any]:
        path = _as_text(inputs.get("path") or config.path)
        context.services.storage.write_workspace_file(path, inputs.get("content"))
        return {"path": path}


class WriteNoteFolderNode(BaseNode):
    type_name = "write_note_folder"
    label = "Write Note Folder"
    description = (
        "Writes a summary and its section notes as Markdown files in one workspace folder, "
        "so they show up together in Notes."
    )
    category = "files"
    tags = ["notes", "markdown", "workspace"]
    inputs = [
        PortDefinition("folder", "text", required=False, description="Folder name, usually a short model-written paper name."),
        PortDefinition("summary", "any", required=False, description="Top-level summary, written as one file."),
        PortDefinition("items", "json", required=False, description="Section notes, one Markdown file each."),
        PortDefinition("title", "any", required=False, description="Paper title or document payload, used for headings."),
    ]
    outputs = [
        PortDefinition("folder", "text", description="Workspace-relative folder the notes were written to."),
        PortDefinition("paths", "json", description="Every file written, in order."),
        PortDefinition("count", "number", description="How many files were written."),
    ]
    config_model = WriteNoteFolderConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: WriteNoteFolderConfig) -> dict[str, Any]:
        title_value = inputs.get("title")
        title = _as_text(title_value.get("title") or "") if isinstance(title_value, dict) else _as_text(title_value)
        title = title.strip()

        fallback = _slugify(title, f"paper-{context.run_id[:8]}")
        folder = _slugify(inputs.get("folder") or config.folder, fallback)
        base = "/".join(part for part in (_slugify(config.base_dir), folder) if part)

        storage = context.services.storage
        if config.replace_existing:
            try:
                storage.clear_workspace_folder_markdown(base)
            except StorageError:
                pass

        paths: list[str] = []
        summary = _note_text(inputs.get("summary")).strip()
        if summary:
            name = _slugify(config.summary_name, "summary")
            heading = f"# {title}\n\n" if title and not summary.lstrip().startswith("# ") else ""
            paths.append(storage.write_workspace_file(f"{base}/{name}.md", f"{heading}{summary}\n").relative_path)

        prefix = _slugify(config.item_prefix, "section")
        marker = config.skip_marker.strip().casefold()
        written = 0
        for item in _as_list(inputs.get("items")):
            body = _note_text(item).strip()
            if not body or (marker and body.casefold() == marker):
                continue
            written += 1
            heading = f"# {config.item_heading} {written}".rstrip()
            if title:
                heading = f"{heading} — {title}"
            path = f"{base}/{prefix}-{written:02d}.md"
            paths.append(storage.write_workspace_file(path, f"{heading}\n\n{body}\n").relative_path)

        await context.emit(
            "info",
            {"node_path": context.node_path, "message": f"Wrote {len(paths)} note(s) to {base or 'the workspace root'}."},
        )
        return {"folder": base, "paths": paths, "count": len(paths)}


class IndexChunksNode(BaseNode):
    type_name = "index_chunks"
    label = "Index Chunks"
    description = "Embeds and persists document chunks for vector retrieval."
    category = "retrieval"
    inputs = [PortDefinition("document_id", "text", required=False), PortDefinition("chunks", "json", required=False)]
    outputs = [PortDefinition("indexed_count", "number")]
    config_model = IndexChunksConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: IndexChunksConfig) -> dict[str, Any]:
        resolved = context.services.model_runtime.resolve(
            "embedding", node_reference=_model_reference(config), workflow_defaults=context.workflow_model_defaults
        )
        document_id = _as_text(inputs.get("document_id")) if inputs.get("document_id") is not None else None
        if document_id:
            chunks = context.services.retrieval.fetch_document_chunks(document_id)
            chunk_ids = [chunk.id for chunk in chunks]
            texts = [chunk.text for chunk in chunks]
        else:
            payload = _as_list(inputs.get("chunks"))
            if not payload:
                raise ValueError("No chunks supplied to index")
            document_id = payload[0].get("document_id")
            if not document_id:
                raise ValueError("Chunk payloads must include document_id")
            chunks = context.services.retrieval.fetch_document_chunks(document_id)
            chunk_ids = [chunk.id for chunk in chunks]
            texts = [chunk.text for chunk in chunks]
        embeddings = await context.services.model_runtime.embed(resolved, texts)
        context.services.retrieval.set_embeddings(chunk_ids, embeddings)
        return {"indexed_count": len(chunk_ids)}


class VectorRetrieveNode(BaseNode):
    type_name = "vector_retrieve"
    label = "Vector Retrieve"
    description = "Runs embedded semantic retrieval over persisted document chunks."
    category = "retrieval"
    inputs = [PortDefinition("query", "text"), PortDefinition("document_id", "text", required=False)]
    outputs = [PortDefinition("results", "json"), PortDefinition("context_text", "text")]
    config_model = RetrieveConfig
    tool_spec = ToolSpec(
        name="search_paper",
        description="Finds the passages most relevant to a question, by meaning rather than wording.",
        parameters=(
            ToolParameter(port="query", description="What you are looking for, in plain language."),
            ToolParameter(
                port="document_id",
                description="Restrict the search to one document.",
                required=False,
            ),
        ),
        result_port="context_text",
    )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: RetrieveConfig) -> dict[str, Any]:
        resolved = context.services.model_runtime.resolve(
            "embedding", node_reference=_model_reference(config), workflow_defaults=context.workflow_model_defaults
        )
        document_id = _as_text(inputs.get("document_id") or config.document_id) or None
        embedding = (await context.services.model_runtime.embed(resolved, _as_text(inputs.get("query"))))[0]
        results = context.services.retrieval.vector_search(embedding, document_id=document_id, top_k=config.top_k)
        if not results and not context.services.retrieval.has_embeddings(document_id):
            await context.emit(
                "warning",
                {
                    "node_path": context.node_path,
                    "message": (
                        "This paper has not been indexed yet, so there is nothing to search. Run the Paper "
                        "ingestion workflow on it first, or add an Index chunks step before this one."
                    ),
                },
            )
        return {"results": results, "context_text": _render_context(results)}


class KeywordRetrieveNode(BaseNode):
    type_name = "keyword_retrieve"
    label = "Keyword/Grep Retrieve"
    description = "Performs simple keyword retrieval over persisted chunks."
    category = "retrieval"
    inputs = [PortDefinition("query", "text"), PortDefinition("document_id", "text", required=False)]
    outputs = [PortDefinition("results", "json"), PortDefinition("context_text", "text")]
    config_model = RetrieveConfig
    tool_spec = ToolSpec(
        name="search_paper_keywords",
        description="Finds passages containing specific words or phrases. Use for names, numbers and exact terms.",
        parameters=(
            ToolParameter(port="query", description="The words or phrase to look for."),
            ToolParameter(
                port="document_id",
                description="Restrict the search to one document.",
                required=False,
            ),
        ),
        result_port="context_text",
    )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: RetrieveConfig) -> dict[str, Any]:
        document_id = _as_text(inputs.get("document_id") or config.document_id) or None
        results = context.services.retrieval.keyword_search(_as_text(inputs.get("query")), document_id=document_id, top_k=config.top_k)
        return {"results": results, "context_text": _render_context(results)}


class FullContextNode(BaseNode):
    type_name = "full_context"
    label = "Full Document Context"
    description = "Loads ordered document chunks until a context budget is reached."
    category = "retrieval"
    inputs = [PortDefinition("document_id", "text", required=False)]
    outputs = [PortDefinition("results", "json"), PortDefinition("context_text", "text")]
    config_model = FullContextConfig
    tool_spec = ToolSpec(
        name="read_whole_paper",
        description="Reads a document from the beginning up to a length budget. Use when a search is too narrow.",
        parameters=(
            ToolParameter(port="document_id", description="The id of the document to read."),
        ),
        result_port="context_text",
    )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: FullContextConfig) -> dict[str, Any]:
        document_id = _as_text(inputs.get("document_id") or config.document_id)
        if not document_id:
            raise ValueError("document_id is required")
        results = context.services.retrieval.full_context(document_id, max_chars=config.max_chars)
        return {"results": results, "context_text": _render_context(results)}


class ContextMergeNode(BaseNode):
    type_name = "context_merge"
    label = "Context Merge/Deduplicate"
    description = "Deduplicates and merges retrieval result sets into one grounded context."
    category = "retrieval"
    inputs = [
        PortDefinition("primary", "json", required=False),
        PortDefinition("secondary", "json", required=False),
        PortDefinition("extra", "json", required=False),
    ]
    outputs = [PortDefinition("results", "json"), PortDefinition("context_text", "text")]

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        merged = context.services.retrieval.merge_contexts(
            _as_list(inputs.get("primary")),
            _as_list(inputs.get("secondary")),
            _as_list(inputs.get("extra")),
        )
        return {"results": merged, "context_text": _render_context(merged)}


class CitationFormatterNode(BaseNode):
    type_name = "citation_formatter"
    label = "Citation/Source Formatter"
    description = "Formats citations from retrieval results or chunks."
    category = "outputs"
    inputs = [PortDefinition("results", "json")]
    outputs = [PortDefinition("text", "text")]
    tool_spec = ToolSpec(
        name="format_citations",
        description="Turns retrieval results into a tidy source list.",
        parameters=(
            ToolParameter(port="results", json_type="string", description="The retrieval results to format."),
        ),
        result_port="text",
    )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        results = _as_list(inputs.get("results"))
        text = "\n".join(f"- {item.get('citation', 'source')}" for item in results)
        return {"text": text}


class MapSubflowNode(BaseNode):
    type_name = "map_subflow"
    label = "Map Subflow"
    description = "Executes a reusable subflow over each item in a bounded list."
    category = "control"
    inputs = [PortDefinition("items", "json")]
    outputs = [
        PortDefinition("results", "json"),
        PortDefinition("count", "number", required=False, description="How many subflow runs completed."),
    ]
    config_model = MapSubflowConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: MapSubflowConfig) -> dict[str, Any]:
        items = _as_list(inputs.get("items"))
        if len(items) > config.max_items:
            if config.on_overflow == "error":
                raise ValueError(
                    f"This step received {len(items)} items but is limited to {config.max_items}. "
                    "Raise the limit, group more items per run, or set overflow to truncate."
                )
            await context.emit(
                "warning",
                {
                    "node_path": context.node_path,
                    "message": (
                        f"Only the first {config.max_items} of {len(items)} items were processed. "
                        "Raise the limit or increase the group size to cover the whole document."
                    ),
                },
            )
            items = items[: config.max_items]

        groups = _group_items(items, config.group_size, config.group_separator)
        workflow = WorkflowDefinition.model_validate(config.subflow)
        results: list[Any] = [None] * len(groups)
        semaphore = asyncio.Semaphore(config.concurrency)

        async def run_group(index: int, payload: Any) -> None:
            async with semaphore:
                context.check_cancelled()
                result = await context.execute_subflow(
                    workflow,
                    {config.item_key: payload, config.index_key: index},
                    f"[{index}]",
                )
                results[index] = _collapse_subflow(result)
                await context.emit(
                    "progress",
                    {"node_path": context.node_path, "completed": index + 1, "total": len(groups)},
                )

        await asyncio.gather(*(run_group(index, payload) for index, payload in enumerate(groups)))
        return {"results": results, "count": len(results)}


class ReduceCombineNode(BaseNode):
    type_name = "reduce_combine"
    label = "Reduce/Combine"
    description = "Combines mapped results into text, merged JSON, or a list."
    category = "control"
    inputs = [PortDefinition("items", "json")]
    outputs = [PortDefinition("value", "any"), PortDefinition("text", "text")]
    config_model = ReduceCombineConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: ReduceCombineConfig) -> dict[str, Any]:
        items = _as_list(inputs.get("items"))
        if config.mode == "json_merge":
            merged: dict[str, Any] = {}
            for item in items:
                if isinstance(item, dict):
                    merged.update(item)
            return {"value": merged, "text": _as_text(merged)}
        if config.mode == "list":
            return {"value": items, "text": _as_text(items)}
        text = config.join_with.join(_as_text(item) for item in items)
        return {"value": text, "text": text}


class RepeatSubflowNode(BaseNode):
    type_name = "repeat_subflow"
    label = "Repeat Subflow"
    description = "Repeats a reusable subflow with a hard iteration cap and optional stop signal."
    category = "control"
    inputs = [PortDefinition("seed", "any")]
    outputs = [PortDefinition("value", "any"), PortDefinition("history", "json"), PortDefinition("iterations", "number")]
    config_model = RepeatSubflowConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: RepeatSubflowConfig) -> dict[str, Any]:
        workflow = WorkflowDefinition.model_validate(config.subflow)
        current = inputs.get("seed")
        history: list[Any] = []
        for iteration in range(config.max_iterations):
            context.check_cancelled()
            result = await context.execute_subflow(workflow, {config.value_key: current, "iteration": iteration}, f"(repeat-{iteration})")
            collapsed = _collapse_subflow(result)
            history.append(collapsed)
            if isinstance(collapsed, dict):
                if config.stop_when_field and collapsed.get(config.stop_when_field):
                    current = collapsed.get(config.carry_field) if config.carry_field else collapsed
                    return {"value": current, "history": history, "iterations": iteration + 1}
                current = collapsed.get(config.carry_field) if config.carry_field else collapsed
            else:
                current = collapsed
        return {"value": current, "history": history, "iterations": config.max_iterations}


class MergeNode(BaseNode):
    type_name = "merge"
    label = "Merge"
    description = "Merges two values safely without arbitrary execution."
    category = "control"
    inputs = [PortDefinition("left", "any", required=False), PortDefinition("right", "any", required=False)]
    outputs = [PortDefinition("value", "any")]
    config_model = MergeConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: MergeConfig) -> dict[str, Any]:
        left = inputs.get("left")
        right = inputs.get("right")
        if isinstance(left, dict) and isinstance(right, dict):
            return {"value": {**left, **right}}
        if isinstance(left, list) and isinstance(right, list):
            return {"value": left + right}
        return {"value": {"left": left, "right": right}}


class IfElseNode(BaseNode):
    type_name = "if_else"
    label = "If / Else"
    description = "Routes a value to exactly one branch using a safe visual condition."
    category = "control"
    tags = ["branch", "condition"]
    inputs = [PortDefinition("value", "any", required=False)]
    outputs = [
        PortDefinition("matched", "any", description="Whether the condition matched."),
        PortDefinition("true", "any", required=False, description="Value when the condition matches."),
        PortDefinition("false", "any", required=False, description="Value when the condition does not match."),
    ]
    config_model = IfElseConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: IfElseConfig) -> dict[str, Any]:
        matched = evaluate_condition(config.condition, workflow=context.run_inputs, inputs=inputs)
        return {"matched": matched, ("true" if matched else "false"): inputs.get("value")}


class ChunkBudgetNode(BaseNode):
    type_name = "chunk_budget"
    label = "Chunk/Token Budget"
    description = "Trims text or retrieval results to a bounded character budget."
    category = "control"
    inputs = [PortDefinition("value", "any")]
    outputs = [PortDefinition("value", "any"), PortDefinition("text", "text")]
    config_model = ChunkBudgetConfig

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: ChunkBudgetConfig) -> dict[str, Any]:
        value = inputs.get("value")
        if isinstance(value, list):
            budget = config.max_chars
            kept: list[dict[str, Any]] = []
            used = 0
            for item in value:
                text = _as_text(item.get("text") if isinstance(item, dict) else item)
                if kept and used + len(text) > budget:
                    break
                kept.append(item)
                used += len(text)
            return {"value": kept, "text": truncate_text(_render_context(kept), config.max_chars)}
        text = truncate_text(_as_text(value), config.max_chars)
        return {"value": text, "text": text}


class FinalOutputNode(BaseNode):
    type_name = "final_output"
    label = "Final Output"
    description = "Marks workflow output and optionally stores it as a run artifact."
    category = "outputs"
    inputs = [PortDefinition("content", "any", required=False), PortDefinition("citations", "any", required=False)]
    outputs = [PortDefinition("result", "any"), PortDefinition("artifact", "json", required=False)]
    config_model = FinalOutputConfig
    interface_role = "output"
    interface_value_port = "result"

    def interface_port(self, node_id: str, config: Any) -> WorkflowPort | None:
        if not isinstance(config, FinalOutputConfig):
            config = FinalOutputConfig.model_validate(config)
        return WorkflowPort(key=config.output_key, kind="any", label=config.output_key, node_id=node_id)

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: FinalOutputConfig) -> dict[str, Any]:
        result: Any
        if inputs.get("citations") is not None:
            result = {"content": inputs.get("content"), "citations": inputs.get("citations")}
        else:
            result = inputs.get("content")
        artifact_payload = None
        if config.artifact_name:
            if config.format == "json":
                relative_path = f"runs/{context.run_id}/{config.artifact_name}.json"
                stored = context.services.storage.write_json(context.services.settings.artifacts_dir, relative_path, result)
                media_type = "application/json"
            elif config.format == "md":
                relative_path = f"runs/{context.run_id}/{config.artifact_name}.md"
                stored = context.services.storage.write_text(context.services.settings.artifacts_dir, relative_path, _as_text(result))
                media_type = "text/markdown"
            else:
                relative_path = f"runs/{context.run_id}/{config.artifact_name}.txt"
                stored = context.services.storage.write_text(context.services.settings.artifacts_dir, relative_path, _as_text(result))
                media_type = "text/plain"
            artifact = context.services.documents.create_artifact_record(
                owner_type="run",
                kind="run_output",
                run_id=context.run_id,
                relative_path=relative_path,
                media_type=media_type,
                stored=stored,
            )
            artifact_payload = {"id": artifact.id, "relative_path": artifact.relative_path, "media_type": artifact.media_type}
        return {"result": result, "artifact": artifact_payload}


class WorkflowOutputNode(BaseNode):
    type_name = "workflow_output"
    label = "Workflow Output"
    description = "Publishes a named value on the workflow's public interface."
    category = "outputs"
    tags = ["interface", "return"]
    inputs = [PortDefinition("value", "any")]
    outputs: list[PortDefinition] = []
    config_model = WorkflowOutputConfig
    interface_role = "output"
    interface_value_port = "value"

    def interface_port(self, node_id: str, config: Any) -> WorkflowPort | None:
        if not isinstance(config, WorkflowOutputConfig):
            config = WorkflowOutputConfig.model_validate(config)
        return WorkflowPort(
            key=config.key,
            kind="any",
            label=config.label or config.key,
            description=config.description,
            node_id=node_id,
        )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: WorkflowOutputConfig) -> dict[str, Any]:
        return {"value": inputs.get("value")}


BUILTIN_NODES: list[BaseNode] = [
    AgentNode(),
    PythonCodeNode(),
    TextInputNode(),
    WorkflowInputNode(),
    PromptTemplateNode(),
    PromptBuilderNode(),
    OllamaGenerateNode(),
    OllamaEmbedNode(),
    PlainTextNode(),
    MarkdownNode(),
    JsonParseNode(),
    PdfIngestNode(),
    EnhanceDocumentPageNode(),
    SelectDocumentNode(),
    DocumentSplitterNode(),
    ReadWorkspaceNode(),
    WriteWorkspaceNode(),
    WriteNoteFolderNode(),
    IndexChunksNode(),
    VectorRetrieveNode(),
    KeywordRetrieveNode(),
    FullContextNode(),
    ContextMergeNode(),
    CitationFormatterNode(),
    MapSubflowNode(),
    ReduceCombineNode(),
    RepeatSubflowNode(),
    MergeNode(),
    IfElseNode(),
    ChunkBudgetNode(),
    FinalOutputNode(),
    WorkflowOutputNode(),
]

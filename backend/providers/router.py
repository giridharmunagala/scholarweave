from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from backend.api.dependencies import services
from backend.providers.schemas import (
    ProviderCreate,
    ProviderModelsResponse,
    ProviderResponse,
    ProviderUpdate,
    ProviderVerifyRequest,
    ProviderVerifyResponse,
    TranscriptionResponse,
    BuiltInSpeechStatus,
)
from backend.providers.errors import ProviderRuntimeError
from backend.providers.types import ModelReference
from backend.providers.service import ProviderService

router = APIRouter(prefix="/providers", tags=["providers"])
MAX_AUDIO_BYTES = 25 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


def provider_service(container=Depends(services)) -> ProviderService:
    return container.providers


async def _read_audio_upload(file: UploadFile) -> bytes:
    content = bytearray()
    try:
        while chunk := await file.read(UPLOAD_CHUNK_BYTES):
            content.extend(chunk)
            if len(content) > MAX_AUDIO_BYTES:
                raise ProviderRuntimeError(
                    "The recorded audio exceeds the 25 MB limit."
                )
    finally:
        await file.close()
    if not content:
        raise ProviderRuntimeError("The recorded audio is empty.")
    return bytes(content)


@router.get("/speech/builtin/status", response_model=BuiltInSpeechStatus)
def built_in_speech_status(container=Depends(services)) -> BuiltInSpeechStatus:
    return container.builtin_speech.status()


@router.post("/speech/builtin/install", response_model=BuiltInSpeechStatus)
async def install_built_in_speech(container=Depends(services)) -> BuiltInSpeechStatus:
    return await container.builtin_speech.install()


@router.delete("/speech/builtin", response_model=BuiltInSpeechStatus)
async def uninstall_built_in_speech(container=Depends(services)) -> BuiltInSpeechStatus:
    return await container.builtin_speech.uninstall()


@router.post("/speech/builtin/start", response_model=BuiltInSpeechStatus)
async def start_built_in_speech(container=Depends(services)) -> BuiltInSpeechStatus:
    return await container.builtin_speech.start()


@router.websocket("/speech/builtin/stream")
async def stream_with_built_in_speech(
    websocket: WebSocket,
    container=Depends(services),
) -> None:
    await websocket.accept()
    try:
        session = await container.builtin_speech.create_session()
        await websocket.send_json({"type": "ready"})
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            content = message.get("bytes")
            if content is not None:
                text = await session.accept_pcm(content)
                if not await _send_speech_message(
                    websocket,
                    {"type": "partial", "text": text},
                ):
                    break
            elif message.get("text") == "finish":
                text = await session.finish()
                if await _send_speech_message(
                    websocket,
                    {"type": "final", "text": text},
                ):
                    await websocket.close()
                break
    except WebSocketDisconnect:
        pass
    except ProviderRuntimeError as exc:
        if await _send_speech_message(
            websocket,
            {"type": "error", "message": str(exc)},
        ):
            await websocket.close(code=1011)


async def _send_speech_message(
    websocket: WebSocket,
    message: dict[str, str],
) -> bool:
    try:
        await websocket.send_json(message)
        return True
    except (WebSocketDisconnect, RuntimeError):
        return False


@router.get("", response_model=list[ProviderResponse])
def list_providers(
    include_archived: bool = Query(default=False),
    service: ProviderService = Depends(provider_service),
) -> list[ProviderResponse]:
    return service.list(include_archived=include_archived)


@router.post("", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
def create_provider(
    payload: ProviderCreate,
    service: ProviderService = Depends(provider_service),
) -> ProviderResponse:
    return service.create(payload)


@router.get("/{profile_id}", response_model=ProviderResponse)
def get_provider(
    profile_id: str,
    service: ProviderService = Depends(provider_service),
) -> ProviderResponse:
    return service.get(profile_id)


@router.put("/{profile_id}", response_model=ProviderResponse)
def update_provider(
    profile_id: str,
    payload: ProviderUpdate,
    service: ProviderService = Depends(provider_service),
) -> ProviderResponse:
    return service.update(profile_id, payload)


@router.delete("/{profile_id}", response_model=ProviderResponse)
def archive_provider(
    profile_id: str,
    service: ProviderService = Depends(provider_service),
) -> ProviderResponse:
    return service.archive(profile_id)


@router.get("/{profile_id}/models", response_model=ProviderModelsResponse)
async def discover_models(
    profile_id: str,
    service: ProviderService = Depends(provider_service),
) -> ProviderModelsResponse:
    return await service.discover(profile_id)


@router.post("/{profile_id}/verify", response_model=ProviderVerifyResponse)
async def verify_provider(
    profile_id: str,
    payload: ProviderVerifyRequest,
    service: ProviderService = Depends(provider_service),
) -> ProviderVerifyResponse:
    return await service.verify(profile_id, model_name=payload.model)


@router.post("/speech/transcriptions", response_model=TranscriptionResponse)
async def transcribe_speech(
    file: UploadFile = File(...),
    provider_profile_id: str = Form(...),
    model: str = Form(...),
    service: ProviderService = Depends(provider_service),
) -> TranscriptionResponse:
    content = await _read_audio_upload(file)
    text = await service.transcribe(
        ModelReference(provider_profile_id=provider_profile_id, model=model),
        filename=file.filename or "recording.webm",
        content=content,
        content_type=file.content_type or "application/octet-stream",
    )
    return TranscriptionResponse(text=text)

"""Provide functionality to STT."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from aiohttp import web
from aiohttp.hdrs import istr
from aiohttp.web_exceptions import (
    HTTPBadRequest,
    HTTPNotFound,
    HTTPUnsupportedMediaType,
)
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import language as language_util

from .const import (
    DOMAIN,
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechResultState,
)
from .legacy import (
    Provider,
    SpeechMetadata,
    SpeechResult,
    async_get_provider,
    async_setup_legacy,
)

__all__ = [
    "async_get_provider",
    "AudioBitRates",
    "AudioChannels",
    "AudioCodecs",
    "AudioFormats",
    "AudioSampleRates",
    "DOMAIN",
    "Provider",
    "SpeechMetadata",
    "SpeechResult",
    "SpeechResultState",
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up STT."""
    websocket_api.async_register_command(hass, websocket_list_engines)

    platform_setups = async_setup_legacy(hass, config)

    if platform_setups:
        await asyncio.wait([asyncio.create_task(setup) for setup in platform_setups])

    hass.http.register_view(SpeechToTextView(hass.data[DOMAIN]))
    return True


class SpeechToTextView(HomeAssistantView):
    """STT view to generate a text from audio stream."""

    requires_auth = True
    url = "/api/stt/{provider}"
    name = "api:stt:provider"

    def __init__(self, providers: dict[str, Provider]) -> None:
        """Initialize a tts view."""
        self.providers = providers

    async def post(self, request: web.Request, provider: str) -> web.Response:
        """Convert Speech (audio) to text."""
        if provider not in self.providers:
            raise HTTPNotFound()
        stt_provider: Provider = self.providers[provider]

        # Get metadata
        try:
            metadata = _metadata_from_header(request)
        except ValueError as err:
            raise HTTPBadRequest(text=str(err)) from err

        # Check format
        if not stt_provider.check_metadata(metadata):
            raise HTTPUnsupportedMediaType()

        # Process audio stream
        result = await stt_provider.async_process_audio_stream(
            metadata, request.content
        )

        # Return result
        return self.json(asdict(result))

    async def get(self, request: web.Request, provider: str) -> web.Response:
        """Return provider specific audio information."""
        if provider not in self.providers:
            raise HTTPNotFound()
        stt_provider: Provider = self.providers[provider]

        return self.json(
            {
                "languages": stt_provider.supported_languages,
                "formats": stt_provider.supported_formats,
                "codecs": stt_provider.supported_codecs,
                "sample_rates": stt_provider.supported_sample_rates,
                "bit_rates": stt_provider.supported_bit_rates,
                "channels": stt_provider.supported_channels,
            }
        )


def _metadata_from_header(request: web.Request) -> SpeechMetadata:
    """Extract STT metadata from header.

    X-Speech-Content:
        format=wav; codec=pcm; sample_rate=16000; bit_rate=16; channel=1; language=de_de
    """
    try:
        data = request.headers[istr("X-Speech-Content")].split(";")
    except KeyError as err:
        raise ValueError("Missing X-Speech-Content header") from err

    fields = (
        "language",
        "format",
        "codec",
        "bit_rate",
        "sample_rate",
        "channel",
    )

    # Convert Header data
    args: dict[str, Any] = {}
    for entry in data:
        key, _, value = entry.strip().partition("=")
        if key not in fields:
            raise ValueError(f"Invalid field {key}")
        args[key] = value

    for field in fields:
        if field not in args:
            raise ValueError(f"Missing {field} in X-Speech-Content header")

    try:
        return SpeechMetadata(
            language=args["language"],
            format=args["format"],
            codec=args["codec"],
            bit_rate=args["bit_rate"],
            sample_rate=args["sample_rate"],
            channel=args["channel"],
        )
    except TypeError as err:
        raise ValueError(f"Wrong format of X-Speech-Content: {err}") from err


@websocket_api.websocket_command(
    {
        "type": "stt/engine/list",
        vol.Optional("language"): str,
    }
)
@callback
def websocket_list_engines(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """List speech to text engines and, optionally, if they support a given language."""
    legacy_providers: dict[str, Provider] = hass.data[DOMAIN]

    language = msg["language"]
    providers = {
        "providers": [
            {
                "engine_id": engine_id,
                "language_supported": bool(
                    language_util.matches(language, provider.supported_languages)
                ),
            }
            for engine_id, provider in legacy_providers.items()
        ]
    }

    connection.send_message(websocket_api.result_message(msg["id"], providers))

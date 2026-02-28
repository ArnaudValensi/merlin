"""Tests for transcribe.py — audio transcription backends."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, mock_open

import pytest

import transcribe


class TestConstants:
    def test_model_config_constants(self):
        assert transcribe.MODEL_SIZE == "medium"
        assert transcribe.COMPUTE_TYPE == "int8"
        assert transcribe.LANGUAGE == "fr"


class TestLocalBackend:
    @patch("transcribe._get_model")
    def test_returns_joined_segments(self, mock_get_model):
        seg1 = MagicMock()
        seg1.text = " hello "
        seg2 = MagicMock()
        seg2.text = " world "
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([seg1, seg2], MagicMock())
        mock_get_model.return_value = mock_model

        result = transcribe._transcribe_local("/tmp/test.ogg", "fr")
        assert result == "hello world"

    @patch("transcribe._get_model")
    def test_calls_model_with_language(self, mock_get_model):
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([], MagicMock())
        mock_get_model.return_value = mock_model

        transcribe._transcribe_local("/tmp/test.ogg", "en")
        mock_model.transcribe.assert_called_once_with("/tmp/test.ogg", language="en")

    @patch("transcribe._get_model")
    def test_empty_segments_returns_empty_string(self, mock_get_model):
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([], MagicMock())
        mock_get_model.return_value = mock_model

        result = transcribe._transcribe_local("/tmp/test.ogg", "fr")
        assert result == ""


class TestOpenAIBackend:
    @patch("httpx.post")
    def test_calls_openai_api(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"text": "transcribed text"}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        with patch("builtins.open", mock_open(read_data=b"audio data")):
            result = transcribe._transcribe_openai("/tmp/test.ogg", "en", "sk-test-key")

        assert result == "transcribed text"
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.args[0] == "https://api.openai.com/v1/audio/transcriptions"
        assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer sk-test-key"
        assert call_kwargs.kwargs["data"]["model"] == "whisper-1"
        assert call_kwargs.kwargs["data"]["language"] == "en"

    @patch("httpx.post")
    def test_raises_on_api_error(self, mock_post):
        import httpx
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401 Unauthorized", request=MagicMock(), response=MagicMock()
        )
        mock_post.return_value = mock_response

        with patch("builtins.open", mock_open(read_data=b"audio data")):
            with pytest.raises(httpx.HTTPStatusError):
                transcribe._transcribe_openai("/tmp/test.ogg", "en", "sk-bad-key")


class TestSaaSBackend:
    def test_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            transcribe._transcribe_saas("/tmp/test.ogg", "en", "https://merlincloud.dev")


class TestBackendSelection:
    def setup_method(self):
        # Reset the backend logged flag between tests
        transcribe._backend_logged = False

    @patch("transcribe._transcribe_local", return_value="local result")
    @patch.dict("os.environ", {}, clear=True)
    def test_no_env_uses_local(self, mock_local):
        # Ensure neither key is set
        import os
        os.environ.pop("MERLIN_SAAS_API", None)
        os.environ.pop("OPENAI_API_KEY", None)
        result = transcribe.transcribe("/tmp/test.ogg")
        assert result == "local result"
        mock_local.assert_called_once_with("/tmp/test.ogg", "fr")

    @patch("transcribe._transcribe_openai", return_value="openai result")
    @patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True)
    def test_openai_key_uses_openai(self, mock_openai):
        result = transcribe.transcribe("/tmp/test.ogg")
        assert result == "openai result"
        mock_openai.assert_called_once_with("/tmp/test.ogg", "fr", "sk-test")

    @patch("transcribe._transcribe_saas")
    @patch.dict("os.environ", {"MERLIN_SAAS_API": "https://merlincloud.dev", "OPENAI_API_KEY": "sk-test"}, clear=True)
    def test_saas_takes_priority_over_openai(self, mock_saas):
        mock_saas.side_effect = NotImplementedError("not yet")
        with pytest.raises(NotImplementedError):
            transcribe.transcribe("/tmp/test.ogg")
        mock_saas.assert_called_once_with("/tmp/test.ogg", "fr", "https://merlincloud.dev")

    @patch("transcribe._transcribe_local", return_value="text")
    @patch.dict("os.environ", {}, clear=True)
    def test_custom_language_passed_through(self, mock_local):
        import os
        os.environ.pop("MERLIN_SAAS_API", None)
        os.environ.pop("OPENAI_API_KEY", None)
        transcribe.transcribe("/tmp/test.ogg", language="de")
        mock_local.assert_called_once_with("/tmp/test.ogg", "de")

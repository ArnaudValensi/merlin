"""Tests for transcribe.py — audio transcription via faster-whisper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import transcribe


class TestTranscribe:
    @patch("transcribe._get_model")
    def test_returns_joined_segments(self, mock_get_model):
        seg1 = MagicMock()
        seg1.text = " hello "
        seg2 = MagicMock()
        seg2.text = " world "
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([seg1, seg2], MagicMock())
        mock_get_model.return_value = mock_model

        result = transcribe.transcribe("/tmp/test.ogg")
        assert result == "hello world"

    @patch("transcribe._get_model")
    def test_calls_model_with_english(self, mock_get_model):
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([], MagicMock())
        mock_get_model.return_value = mock_model

        transcribe.transcribe("/tmp/test.ogg")
        mock_model.transcribe.assert_called_once_with("/tmp/test.ogg", language="fr")

    @patch("transcribe._get_model")
    def test_empty_segments_returns_empty_string(self, mock_get_model):
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([], MagicMock())
        mock_get_model.return_value = mock_model

        result = transcribe.transcribe("/tmp/test.ogg")
        assert result == ""

    def test_model_config_constants(self):
        assert transcribe.MODEL_SIZE == "medium"
        assert transcribe.COMPUTE_TYPE == "int8"
        assert transcribe.LANGUAGE == "fr"

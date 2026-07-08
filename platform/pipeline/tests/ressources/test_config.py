"""Unit tests targeting platform settings parsing logic utility functions."""

from unittest.mock import MagicMock, patch

from galadril_pipeline.resources.config import load_vision_config


@patch("galadril_vision.common.config.VisionConfig.from_yaml")
def test_load_vision_config_default_path(mock_from_yaml: MagicMock) -> None:
    """Validates that the configuration function parses default bootstrap files safely."""
    mock_cfg = MagicMock()
    mock_from_yaml.return_value = mock_cfg

    result = load_vision_config()

    assert result is mock_cfg
    mock_from_yaml.assert_called_once_with("bootstrap.yaml")


@patch("galadril_vision.common.config.VisionConfig.from_yaml")
def test_load_vision_config_custom_path(mock_from_yaml: MagicMock) -> None:
    """Validates that custom configuration file pathways propagate accurately downstream."""
    mock_cfg = MagicMock()
    mock_from_yaml.return_value = mock_cfg

    result = load_vision_config("custom_pipeline_layout.yaml")

    assert result is mock_cfg
    mock_from_yaml.assert_called_once_with("custom_pipeline_layout.yaml")
